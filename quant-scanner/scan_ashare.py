# -*- coding: utf-8 -*-
"""
A股盘前扫描 —— 新架构入口脚本。

这是这次重构系列的第一个可以真正在 GitHub Actions 里跑的完整脚本。
跟原来 scan_ashare.py 比，核心差异：

1. 选股结果通过 tool call 结构化提交（core/ai_picks.py + core/prompting.py），
   不再从自由文本 HTML 里用正则硬解析。
2. 止损存百分比、不存绝对价格（core/models.py 的 Pick.stop_loss_pct），
   plaza止损位再也不会出现"用盘前参考价算、跟真实开盘价对不上"的问题。
3. 冻结名单判断用 core/storage.py 的 frozen_min_score，不再有"Score
   缺失时该怎么处理"这类边界情况——因为 Pick 在构造时就做了范围校验，
   不可能有 NaN Score 流进 trade_history。
4. pending 文件（scan→review 交接）用 core/storage.py 的
   save_pending_picks，包含日期字段本身，不需要review阶段再从文件名
   正则解析日期。
5. 任何阶段的异常都会触发失败通知邮件（core/notify.py），不会只是
   安静地印在 GitHub Actions 日志里等你自己发现。

环境变量（跟原脚本保持一致，方便直接复用现有的 GitHub Secrets 配置）：
  TUSHARE_TOKEN, CLAWSOCKET_API_KEY, CLAWSOCKET_BASE_URL,
  EMAIL_ACCOUNT, EMAIL_PASSWORD, TARGET_EMAILS
"""
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.models import Market, Status
from core.storage import (
    load_positions, save_pending_picks, frozen_min_score, find_active,
)
from core.indicators import compute_technical_snapshot, suggest_stop_loss_pct
from core.tushare_source import fetch_stock_pool, fetch_macro_context
from core.prompting import build_macro_context_text, build_pool_summary_text, build_scan_prompt, call_ai_for_picks
from core.notify import send_email, notify_failure

TARGET_MODEL = "claude-opus-4-8"
TRADE_HISTORY_PATH = "trade_history.csv"
MAX_PICKS = 5


def _check_beijing_market_hours():
    tz = datetime.timezone(datetime.timedelta(hours=8))
    now = datetime.datetime.now(tz)
    if now.weekday() >= 5:
        print("周末休市，退出扫描。")
        sys.exit(0)
    return now


def _validate_env():
    required = ["TUSHARE_TOKEN", "CLAWSOCKET_API_KEY", "CLAWSOCKET_BASE_URL"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        print(f"致命错误：缺少环境变量 {missing}，请检查 GitHub Actions Secrets 配置。")
        sys.exit(1)


def build_pending_picks_filename(entry_date_str: str) -> str:
    return f"pending_picks_a_share_{entry_date_str.replace('-', '')}.json"


def run_scan():
    now = _check_beijing_market_hours()
    entry_date_str = now.strftime("%Y-%m-%d")
    print(f"启动A股盘前扫描（新架构） | 日期: {entry_date_str} | 引擎: {TARGET_MODEL}")

    import tushare as ts
    ts.set_token(os.environ["TUSHARE_TOKEN"])
    pro = ts.pro_api()

    import anthropic
    client = anthropic.Anthropic(
        api_key=os.environ["CLAWSOCKET_API_KEY"],
        base_url=os.environ["CLAWSOCKET_BASE_URL"],
    )

    import yfinance as yf

    # ---- 1. 读取现有持仓，用于"已持有的不重复选"和冻结名单 ----
    existing_positions = load_positions(TRADE_HISTORY_PATH)
    frozen = frozen_min_score(existing_positions)
    active_tickers = {p.ticker for p in existing_positions if p.status == Status.ACTIVE}
    print(f"当前持仓 {len(active_tickers)} 只 | 冻结名单 {len(frozen)} 只")

    # ---- 2. 拉取候选池 + 计算技术指标 ----
    pool = fetch_stock_pool(pro)
    pool = [s for s in pool if s.ticker not in active_tickers]
    print(f"候选池 {len(pool)} 支（已排除当前持仓）")

    technical_by_ticker = {}
    for stock in pool:
        try:
            technical_by_ticker[stock.ticker] = compute_technical_snapshot(stock.bars)
        except ValueError as e:
            print(f"⚠️ {stock.ticker} 技术指标计算跳过: {e}")

    # ---- 3. 宏观数据 ----
    macro_ctx = fetch_macro_context(yf)
    macro_text = build_macro_context_text(macro_ctx)

    # ---- 4. 构造 prompt 并调用 AI ----
    pool_summary = build_pool_summary_text(pool, technical_by_ticker)
    frozen_text = "\n".join(f"{ticker}: 需评分>={score:.0f}" for (mkt, ticker), score in frozen.items())
    prompt = build_scan_prompt(pool_summary, macro_text, frozen_text, Market.A_SHARE, MAX_PICKS)

    result = call_ai_for_picks(client, TARGET_MODEL, prompt, Market.A_SHARE)
    print(f"AI 返回 {len(result.picks)} 条候选")

    # ---- 5. 过滤：冻结名单 + 用ATR校正止损（如果AI给的止损明显不合理） ----
    final_picks = []
    for pick in result.picks:
        key = (Market.A_SHARE.value, pick.ticker)
        if key in frozen and pick.score < frozen[key]:
            print(f"⏭️ {pick.ticker} 未达冻结解锁分数线({frozen[key]:.0f})，跳过")
            continue
        # 如果AI没有给技术指标（比如漏填），从我们自己算好的数据里补上，
        # 保证止损的ATR依据不会因为AI遗漏而丢失
        if pick.technical.atr_pct is None and pick.ticker in technical_by_ticker:
            pick.technical = technical_by_ticker[pick.ticker]
        final_picks.append(pick)

    if len(final_picks) > MAX_PICKS:
        final_picks = sorted(final_picks, key=lambda p: p.score, reverse=True)[:MAX_PICKS]

    print(f"最终入选 {len(final_picks)} 支：{[p.ticker for p in final_picks]}")

    # ---- 6. 板块集中度检查（警示，不做硬性拦截）----
    concentration_warning = None
    if len(final_picks) >= 2:
        industry_counts = {}
        for p in final_picks:
            industry_counts.setdefault(p.industry, []).append(f"{p.name}({p.ticker})")
        max_industry, max_list = max(industry_counts.items(), key=lambda kv: len(kv[1]))
        pct = len(max_list) / len(final_picks) * 100
        if len(max_list) >= 3 or pct >= 60:
            concentration_warning = f"今日 {len(final_picks)} 支推荐中有 {len(max_list)} 支（{pct:.0f}%）集中在【{max_industry}】行业：{', '.join(max_list)}"
            print(f"⚠️ 板块集中度提示：{concentration_warning}")

    # ---- 7. 保存 pending picks，供 review 阶段补充真实价格 ----
    if final_picks:
        pending_path = build_pending_picks_filename(entry_date_str)
        save_pending_picks(pending_path, final_picks, entry_date_str)
        print(f"✅ 已保存 {len(final_picks)} 条待确认标的至 {pending_path}（不含价格）")
    else:
        print("⚠️ 本次没有入选标的，不生成pending文件。")

    # ---- 8. 发送邮件 ----
    email_html = _build_email_html(result.narrative, final_picks, concentration_warning, entry_date_str)
    send_email(f"🎯 A股盘前扫描 {entry_date_str}", email_html)


def _build_email_html(narrative: str, picks, concentration_warning, entry_date_str: str) -> str:
    picks_html = ""
    for p in picks:
        picks_html += f"""
        <div style="background:#fff;border:1px solid #e0e0e0;border-radius:8px;padding:15px 20px;margin-bottom:12px;">
            <b>{p.name} ({p.ticker})</b> | {p.industry} | 评分 {p.score:.0f}/100
            <br>持有期建议: {p.hold_period_min_days}-{p.hold_period_max_days}天 |
            止损: {p.stop_loss_pct:.1f}%（相对真实开盘价，具体价格在盘后确认）
            <br><span style="color:#666;">{p.reasoning_summary}</span>
        </div>
        """

    concentration_html = ""
    if concentration_warning:
        concentration_html = f"""
        <div style="background:#fff3e0;border-left:5px solid #f57c00;padding:15px 20px;border-radius:8px;margin-bottom:20px;">
            <b>⚠️ 板块集中度提示：</b>{concentration_warning}
        </div>
        """

    return f"""
    <!DOCTYPE html><html><head><meta charset="utf-8"></head>
    <body style="font-family:sans-serif;background:#f4f6f9;padding:20px;">
    <div style="max-width:800px;margin:0 auto;">
        <h1>🎯 A股盘前扫描 {entry_date_str}</h1>
        <div style="background:#eaf4ff;border-left:6px solid #1976d2;padding:20px;border-radius:8px;margin-bottom:20px;">
            {narrative or "（本次无额外宏观主线说明）"}
        </div>
        {concentration_html}
        <h3>今日候选（{len(picks)}支）</h3>
        {picks_html or "<p>本次无入选标的</p>"}
        <p style="text-align:center;color:#999;font-size:12px;margin-top:30px;">
            开盘价/收盘价将在盘后 review 阶段用真实行情数据补充确认。本邮件为系统信号，不构成投资建议。
        </p>
    </div>
    </body></html>
    """


if __name__ == "__main__":
    _validate_env()
    try:
        run_scan()
    except Exception as e:
        import traceback
        traceback.print_exc()
        notify_failure("A股盘前扫描", e)
        sys.exit(1)

# -*- coding: utf-8 -*-
"""
Prompt 构造 + AI 调用。

关键设计决定：narrative（给人看的分析文字）和 picks（机器要用的结构化
数据）在同一次 API 调用里产生，但走两个完全独立的通道——narrative 是
普通 text 内容块，picks 是 tool_use 内容块，用 extract_tool_use_block
精确按类型提取。这意味着不管 narrative 部分 AI 写成什么样（要不要有
前言、要不要先"想"一下再写），都不可能污染 picks 的解析——因为
picks根本不是从 narrative 的文本里挖出来的，是 AI 通过工具调用单独
提交的、由 API 强制校验过 JSON schema 的数据。

这就是"邮件混入AI原始思考过程"这个bug不会在新架构里重新出现的根本
原因：不是"多加了一层过滤"，是"结构化数据和自由文本从一开始就走
两条完全不相交的路径"。
"""
import json
from typing import List, Optional

from .models import Market
from .ai_picks import PICK_TOOL_SCHEMA, extract_tool_use_block, parse_picks_from_tool_use, PickParseError
from .tushare_source import PoolStock


def build_macro_context_text(macro_ctx: dict) -> str:
    """把 fetch_macro_context() 返回的结构化数据组织成给AI看的文本块，
    含板块相关性提示和VIX风控规则——这部分逻辑之前是直接嵌在数据抓取
    函数里的，这里拆开：抓数据和"怎么把数据讲给AI听"是两件事，各自
    更容易单独测试和调整。"""
    lines = []
    for key, entry in macro_ctx["values"].items():
        if entry is None:
            continue
        if key == "VIX":
            lines.append(f"- {entry['desc']} ({entry['ticker']}): {entry['value']:.2f} (当日变动: {entry['pct_chg']:+.2f}%)")
        elif key == "10Y_US_Bond":
            lines.append(f"- {entry['desc']} ({entry['ticker']}): {entry['value']:.3f}% (当日变动: {entry['pct_chg']:+.2f}%)")
        else:
            lines.append(f"- {entry['desc']} ({entry['ticker']}): ${entry['value']:.2f} (当日变动: {entry['pct_chg']:+.2f}%)")

    guidance = ("\n【使用提示】以上大宗商品数据对不同行业相关性差异很大：原油/WTI/布伦特"
                "主要影响石油化工、煤炭开采、航空运输等上下游行业，对其他行业相关性很低，"
                "请结合每支标的自己的所属行业判断，不要不分行业地把油价波动同等代入评分。")

    vix_regime = macro_ctx.get("vix_regime")
    vix_value = macro_ctx.get("vix_value")
    if vix_regime == "extreme":
        guidance += (f"\n【VIX风控提示】当前VIX={vix_value:.1f}，处于极度恐慌区间（>=30）。"
                     f"请大幅提高评分门槛，优先考虑防御性板块，减少激进追涨型推荐。")
    elif vix_regime == "elevated":
        guidance += f"\n【VIX风控提示】当前VIX={vix_value:.1f}，处于偏高波动区间（>=25），请相应提高评分门槛。"

    return "\n".join(lines) + guidance


def build_pool_summary_text(pool: List[PoolStock], technical_by_ticker: dict, max_items: int = 60) -> str:
    """把候选池 + 已经算好的技术指标组织成给AI看的文本。技术指标是
    程序预先算好传进来的（indicators.compute_technical_snapshot），
    AI只需要在打分时参考、并且在tool call里原样带回，不需要自己
    估算指标数值——这样可以直接用程序算出来的准确值去核对AI有没有
    乱编技术面数据。"""
    lines = []
    sorted_pool = sorted(pool, key=lambda s: s.latest_amount, reverse=True)[:max_items]
    for stock in sorted_pool:
        tech = technical_by_ticker.get(stock.ticker)
        if tech is None:
            continue
        lines.append(
            f"{stock.ticker} {stock.name} [{stock.industry}] | "
            f"最新涨跌幅{stock.latest_pct_chg:+.2f}% 成交额{stock.latest_amount/1e8:.2f}亿 | "
            f"RSI={tech.rsi} 乖离率={tech.bias_pct}% ATR%={tech.atr_pct} "
            f"MACD金叉={tech.macd_golden_cross} 周线共振={tech.weekly_resonance} "
            f"KDJ回升={tech.kdj_j_rising} 量能放大={tech.volume_surge}(量比{tech.volume_ratio})"
        )
    return "\n".join(lines)


def build_scan_prompt(pool_summary: str, macro_text: str, frozen_tickers_text: str,
                       market: Market, max_picks: int = 5) -> str:
    market_label = "A股" if market == Market.A_SHARE else "美股"
    return f"""你是一位专业的{market_label}量化策略研究员。基于以下候选股票池的技术面数据和宏观环境，
选出最多 {max_picks} 支值得关注的标的。

【候选股票池】（已预先计算好技术指标，请直接参考，不要自己估算）：
{pool_summary}

【宏观与大宗商品环境】：
{macro_text}

【已被冻结、暂不能选的标的】（历史止损/强清过，需要评分明显更高才能重新入选）：
{frozen_tickers_text or "无"}

【输出要求】：
1. 你必须调用 submit_stock_picks 工具提交结构化的选股结果，这是唯一的数据提交渠道。
2. technical 字段请直接使用候选池里给出的对应数值原样带回，不要自己重新估算。
3. stop_loss_pct 必须是负数（相对入场价的跌幅百分比），建议参考对应标的的 ATR% 来定合理的止损距离
   （波动大的标的止损应该更宽，波动小的应该更紧），不要不分波动大小统一给一个固定数字。
4. reasoning_summary 用1-2句话说明选择理由即可，这个字段不会被程序解析，可以自由书写。
5. 如果某支标的的宏观逻辑推理缺乏真实新闻验证（纯逻辑推演），评分应该相应打折，不要给出
   跟有新闻验证支撑的标的同等的高分。
6. 在调用工具之前，你也可以先用一小段文字（3-5句话）简述今天整体的宏观驱动主线，这段文字
   仅供人工阅读参考，不需要包含任何后面工具调用里已经有的结构化数据。
"""


def call_ai_for_picks(client, model: str, prompt: str, market: Market,
                       max_tokens: int = 4000) -> "ScanResult":
    """
    真正调用 Anthropic API，强制走 submit_stock_picks 工具。

    tool_choice 显式指定工具名（不是让模型自己决定用不用工具）——
    这保证了只要 API 调用成功返回，就一定有 tool_use 块可以提取，
    不会出现"AI这次心情不好，决定只回文字不调用工具"的情况。
    """
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        tools=[PICK_TOOL_SCHEMA],
        tool_choice={"type": "tool", "name": "submit_stock_picks"},
        messages=[{"role": "user", "content": prompt}],
    )

    narrative_parts = [b.text for b in response.content if getattr(b, "type", None) == "text"]
    narrative = "\n".join(narrative_parts).strip()

    tool_input = extract_tool_use_block(response.content, tool_name="submit_stock_picks")
    picks = parse_picks_from_tool_use(tool_input, market)

    return ScanResult(picks=picks, narrative=narrative)


class ScanResult:
    def __init__(self, picks, narrative):
        self.picks = picks
        self.narrative = narrative

# -*- coding: utf-8 -*-
"""
A股数据源：tushare 股票池 + yfinance 宏观数据。

宏观数据这部分是直接复用这次对话里已经验证过、从 stooq.com 切换到
yfinance 之后测试通过的逻辑（^TNX/VIX/黄金/白银/铜/WTI/布伦特），
没有重新发明。

股票池这部分是新写的，但公开的 tushare API 调用方式（stock_basic +
daily）跟原 scan_ashare.py 一致，只是输出目标从"塞进一个巨大的dict"
改成"喂给 indicators.compute_technical_snapshot 需要的 OHLCV 格式"。
"""
import datetime
from dataclasses import dataclass
from typing import List, Optional

import pandas as pd

from .indicators import OHLCV


@dataclass
class PoolStock:
    """一支候选股票，携带足够构造 Pick 所需的原始信息（除了AI要填的
    score/tag/reasoning这些主观判断字段）。"""
    ticker: str          # tushare格式，如 600276.SH
    name: str
    industry: str
    bars: OHLCV           # 最近N日行情，喂给indicators模块算技术指标
    latest_amount: float  # 最新成交额，用于流动性过滤/展示
    latest_pct_chg: float  # 最新涨跌幅(%)，盘前动量参考


def fetch_stock_pool(pro, lookback_days: int = 90, min_amount: float = 30_000_000,
                      max_tickers: int = 300) -> List[PoolStock]:
    """
    构建A股候选池：市值/流动性初筛 + 拉取最近行情。

    pro: 已经 ts.set_token() 过的 tushare pro_api 客户端（调用方传入，
    这个函数不管 token 怎么设置——token 生命周期管理是调用方的责任，
    不是数据获取逻辑该关心的事）。

    这里明确要求至少90天数据（覆盖周线共振需要的12周+日线指标的
    预热期），数据不足的股票会被跳过，而不是硬凑数据算出一个不可靠
    的指标值。
    """
    basic = pro.stock_basic(exchange='', list_status='L', fields='ts_code,name,industry')
    industry_map = dict(zip(basic['ts_code'], basic.get('industry', ['未知'] * len(basic))))
    name_map = dict(zip(basic['ts_code'], basic.get('name', basic['ts_code'])))

    end_date = datetime.datetime.now().strftime('%Y%m%d')
    start_date = (datetime.datetime.now() - datetime.timedelta(days=lookback_days)).strftime('%Y%m%d')

    # 用最近一个交易日的全市场快照做初筛（流动性），再对入选的逐个拉历史K线
    latest_snapshot = None
    for offset in range(7):
        try_date = (datetime.datetime.now() - datetime.timedelta(days=offset)).strftime('%Y%m%d')
        try:
            df = pro.daily(trade_date=try_date)
            if df is not None and not df.empty:
                latest_snapshot = df
                break
        except Exception:
            continue

    if latest_snapshot is None:
        raise RuntimeError("连续7天都拉不到任何全市场快照数据，tushare接口可能异常")

    latest_snapshot = latest_snapshot.copy()
    latest_snapshot['amount'] = latest_snapshot['amount'] * 1000  # tushare的amount单位是千元
    qualified = latest_snapshot[latest_snapshot['amount'] >= min_amount]
    qualified = qualified.sort_values('amount', ascending=False).head(max_tickers)

    pool: List[PoolStock] = []
    for _, row in qualified.iterrows():
        ts_code = row['ts_code']
        try:
            hist = pro.daily(ts_code=ts_code, start_date=start_date, end_date=end_date)
            if hist is None or len(hist) < 30:
                continue
            hist = hist.sort_values('trade_date')
            bars = OHLCV(
                close=hist['close'].astype(float).tolist(),
                high=hist['high'].astype(float).tolist(),
                low=hist['low'].astype(float).tolist(),
                volume=hist['vol'].astype(float).tolist(),
            )
            pool.append(PoolStock(
                ticker=ts_code,
                name=name_map.get(ts_code, ts_code),
                industry=industry_map.get(ts_code, '未知'),
                bars=bars,
                latest_amount=float(row['amount']),
                latest_pct_chg=float(row.get('pct_chg', 0.0)),
            ))
        except Exception as e:
            print(f"⚠️ 跳过 {ts_code}：拉取历史行情失败 {e}")
            continue

    return pool


def fetch_macro_context(yf_module) -> dict:
    """
    抓取国际宏观/大宗商品数据 + 相关性使用提示 + VIX风控规则文本。

    yf_module: 传入 yfinance 模块本身（而不是在这里 import），方便测试
    时用假的yfinance替换掉，不需要真的联网。

    返回 dict 而不是拼好的字符串——调用方（prompt构造那一层）决定
    怎么把这些数据组织进最终的prompt文本，这个函数只负责"拿到干净的
    结构化宏观数据"，两件事分开，各自更容易单独测试。
    """
    macro_tickers = {
        "10Y_US_Bond": ("^TNX", "美国10年期国债收益率"),
        "VIX": ("^VIX", "美股恐慌指数VIX"),
        "Gold": ("GC=F", "COMEX黄金期货"),
        "Silver": ("SI=F", "COMEX白银期货"),
        "Copper": ("HG=F", "COMEX铜期货"),
        "WTI_Oil": ("CL=F", "WTI原油期货"),
        "Brent_Oil": ("BZ=F", "布伦特原油期货"),
    }

    values = {}
    for key, (ticker, desc) in macro_tickers.items():
        try:
            df = yf_module.download(ticker, period="5d", progress=False)
            if df is None or df.empty:
                values[key] = None
                continue
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            close_val = float(df['Close'].iloc[-1])
            prev_close = float(df['Close'].iloc[-2])
            pct_chg = round((close_val - prev_close) / prev_close * 100, 2)
            values[key] = {"desc": desc, "ticker": ticker, "value": close_val, "pct_chg": pct_chg}
        except Exception as e:
            print(f"⚠️ 宏观因子 {desc}({ticker}) 抓取受阻: {e}")
            values[key] = None

    vix_entry = values.get("VIX")
    vix_value = vix_entry["value"] if vix_entry else None

    vix_regime = None
    if vix_value is not None:
        if vix_value >= 30:
            vix_regime = "extreme"
        elif vix_value >= 25:
            vix_regime = "elevated"
        else:
            vix_regime = "normal"

    return {"values": values, "vix_value": vix_value, "vix_regime": vix_regime}

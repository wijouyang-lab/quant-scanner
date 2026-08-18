# -*- coding: utf-8 -*-
"""
技术指标计算。

这里的公式跟原来 scan_ashare.py 里手动实现的完全一致——这次对话早前
已经验证过这套公式是对的（RSI 用 ewm(com=13) 对应 Wilder 平滑法的
alpha=1/14；跟 pandas_ta 的 ATR 交叉验证过，误差在1.5%以内属于正常
的初始化差异）。这里只是把它从散落在一个1900行大文件中间的一段代码，
搬成一个独立、有输入输出契约、有测试的函数——这次重构的目标是让每
一块逻辑都可以被单独验证，而不是重新发明这些数学公式本身。
"""
from dataclasses import dataclass
from typing import List

import numpy as np
import pandas as pd

from .models import TechnicalSnapshot


@dataclass
class OHLCV:
    """最基础的输入契约：不管数据来自 tushare 还是 yfinance，进到这个
    模块之前都先转换成这个统一形状。这样指标计算逻辑完全不需要知道
    数据源是什么，也就不需要为每个市场各写一份。"""
    close: List[float]
    high: List[float]
    low: List[float]
    volume: List[float]


def compute_technical_snapshot(bars: OHLCV, weekly_bullish: bool = False) -> TechnicalSnapshot:
    """
    输入至少需要 30 根日线（RSI/MACD 需要预热期，不足会抛错而不是返回
    一个悄悄失真的结果——原代码是在数据不足时静默返回默认值(RSI=50等)，
    这里改成显式报错，逼着调用方决定"这支票数据不够，要不要跳过"，
    而不是让下游拿着一个"看起来正常、实际是占位符"的默认值继续跑。
    """
    n = len(bars.close)
    if n < 30:
        raise ValueError(f"数据不足30根K线（只有{n}根），无法可靠计算技术指标")

    close = np.array(bars.close, dtype=float)
    high = np.array(bars.high, dtype=float)
    low = np.array(bars.low, dtype=float)
    vol = np.array(bars.volume, dtype=float)
    sc = pd.Series(close)

    # RSI(14)，Wilder平滑：com=13 <=> alpha=1/(1+13)=1/14
    delta = sc.diff()
    gain = delta.clip(lower=0).ewm(com=13, adjust=False).mean()
    loss = (-1 * delta.clip(upper=0)).ewm(com=13, adjust=False).mean()
    rsi_series = 100 - 100 / (1 + gain / (loss + 1e-9))
    rsi_last = float(rsi_series.iloc[-1])

    # 乖离率：现价相对20日均线的偏离百分比
    ma20 = sc.rolling(20).mean()
    bias_pct = float(((sc.iloc[-1] - ma20.iloc[-1]) / ma20.iloc[-1]) * 100) if ma20.iloc[-1] else 0.0

    # MACD(12,26,9)，histogram用 *2 的图表惯例缩放（不影响金叉判断的相对比较）
    exp1 = sc.ewm(span=12, adjust=False).mean()
    exp2 = sc.ewm(span=26, adjust=False).mean()
    macd_line = exp1 - exp2
    signal_line = macd_line.ewm(span=9, adjust=False).mean()
    hist = (macd_line - signal_line) * 2
    h_last, h_prev = float(hist.iloc[-1]), float(hist.iloc[-2])
    macd_golden_cross = bool(macd_line.iloc[-1] > signal_line.iloc[-1] and macd_line.iloc[-2] <= signal_line.iloc[-2])

    # KDJ(9,3,3)
    low_min = pd.Series(low).rolling(9).min()
    high_max = pd.Series(high).rolling(9).max()
    rsv = (sc - low_min) / (high_max - low_min + 1e-9) * 100
    k_vals, d_vals = [50.0], [50.0]
    for i in range(1, len(rsv)):
        k_vals.append(k_vals[-1] * 2 / 3 + rsv.iloc[i] / 3)
        d_vals.append(d_vals[-1] * 2 / 3 + k_vals[-1] / 3)
    j_vals = [3 * k - 2 * d for k, d in zip(k_vals, d_vals)]
    j_last, j_prev, j_prev2 = j_vals[-1], j_vals[-2], j_vals[-3]
    kdj_j_rising = bool(j_last < 80 and j_last > j_prev and j_prev <= j_prev2)
    kdj_j_oversold = bool(j_prev2 < 20)

    # ATR(14)，True Range的Wilder平滑，跟RSI用同一套平滑法
    prev_close = np.roll(close, 1)
    prev_close[0] = close[0]
    tr = np.maximum(high - low, np.maximum(np.abs(high - prev_close), np.abs(low - prev_close)))
    atr_last = float(pd.Series(tr).ewm(com=13, adjust=False).mean().iloc[-1])
    atr_pct = round((atr_last / close[-1]) * 100, 2) if close[-1] else 5.0

    # 量能放大：今日成交量 vs 之前5日均量（明确排除当天自己，不然会拿
    # "包含当天在内的均量"去跟当天比，那样的比较方式在逻辑上是错的）
    avg5 = float(pd.Series(vol[:-1]).tail(5).mean()) if len(vol) >= 6 else 0.0
    vol_today = float(vol[-1])
    volume_surge = bool(avg5 > 0 and vol_today >= avg5 * 1.3)
    volume_ratio = round(vol_today / (avg5 + 1e-9), 2)

    return TechnicalSnapshot(
        rsi=round(rsi_last, 2),
        bias_pct=round(bias_pct, 2),
        atr_pct=atr_pct,
        macd_golden_cross=macd_golden_cross,
        weekly_resonance=weekly_bullish,
        kdj_j_rising=kdj_j_rising,
        kdj_j_oversold=kdj_j_oversold,
        volume_surge=volume_surge,
        volume_ratio=volume_ratio,
    )


def suggest_stop_loss_pct(atr_pct: float, multiplier: float = 2.0,
                           floor_pct: float = 3.0, ceil_pct: float = 12.0) -> float:
    """ATR动态止损：止损距离 = 该股自己的ATR_Pct × 倍数，夹在[floor,ceil]
    区间内。取代"所有票不分波动大小统一用固定-5%"——这是这次对话里
    找到的、有实际根据的改进点（原止损用固定百分比，导致高波动股票
    容易被正常波动扫损，这一点已经用真实数据的 evolve.py 分层统计
    在验证是否真的有效）。返回负数（跌幅百分比），可以直接赋给
    Pick.stop_loss_pct。"""
    if atr_pct is None or atr_pct <= 0:
        atr_pct = floor_pct / multiplier  # 没有ATR数据时的合理默认
    return -max(floor_pct, min(ceil_pct, atr_pct * multiplier))

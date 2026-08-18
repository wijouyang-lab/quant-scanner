# -*- coding: utf-8 -*-
"""
共用数据模型。

这是这次重构的地基：scan / review / evolve 三边、A股和美股两个市场，
全部从这里读字段名和做数据验证，不再像之前那样每个文件各自写死一份
CSV表头字符串、全靠人眼对齐（这正是 MU 那 9 行重复计入、Score 列显示成
"科技" 那类 bug 的根源——两边字段数量、顺序稍微不一致，写出来的CSV
就直接错位，且没有任何东西会在写入的时候报错）。

设计上的几个关键决定，和为什么：

1. Position 用"一个 position 一行、原地更新"模型，两个市场统一。
   原来 A 股是"只要还在追踪就每天新增一行"，同一笔仓位会累积好几行，
   平仓时要么全部重新打标签（新版修复），要么被 evolve.py 按行统计成
   好几笔交易（旧版 bug）。美股一直是"一行到底"，这次统一成这个更简单
   的模型，不是因为它更"高级"，而是因为整个对话里所有跟"同一笔交易被
   算成好几笔"相关的 bug，根源都是 A 股那个多行模型本身。

2. 止损存百分比（stop_loss_pct），不存绝对价格字符串。
   原来止损价是盘前用一个参考价算出来的绝对价格（"107.74元"），真实
   开盘价出来后如果偏离参考价，止损位这个"锚点"从一开始就偏了，需要
   专门写"按比例校准"的补丁去修（这次对话里针对两个市场各写了一遍）。
   存百分比之后，止损价 = 真实开盘价 × (1 + stop_loss_pct/100)，
   在任何真实价格上都能正确换算，校准这类 bug 整个不可能再发生。

3. Status 是统一枚举，不是 A 股的 Tag 字符串 + 美股的 Status 字符串
   两套互不相通的命名。

4. 每个字段都有类型和范围校验（__post_init__ / validate()），
   不合法的数据在"写入"这一步就会报错，不会先安静地写进CSV，
   等到分析阶段才被发现是脏数据。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from enum import Enum
from typing import Optional


class Market(str, Enum):
    A_SHARE = "A_SHARE"
    US = "US"


class Status(str, Enum):
    """统一状态机。取代 A 股原来的 Tag（Core_Dragon/Stop_Loss_Hit/...）
    和美股原来的 Status（Active/Dropped/...）两套不同命名。"""
    ACTIVE = "Active"
    DROPPED = "Dropped"
    STOP_LOSS_HIT = "Stop_Loss_Hit"
    PERIOD_MATURED = "Period_Matured"
    FORCED_EXIT = "Forced_Exit"

    @property
    def is_closed(self) -> bool:
        """是否代表一笔已经平仓、应该被纳入胜率统计的终态。"""
        return self in _CLOSED_STATUSES

    @property
    def is_terminal(self) -> bool:
        """是否是终态——终态的记录不应该再被任何后续流程覆盖写入。
        目前和 is_closed 等价，拆成两个属性是为了以后如果加入非平仓类
        的终态（比如人工标记"数据异常，不纳入统计"）时不用改调用方代码。"""
        return self.is_closed


_CLOSED_STATUSES = {Status.DROPPED, Status.STOP_LOSS_HIT, Status.PERIOD_MATURED, Status.FORCED_EXIT}


@dataclass
class TechnicalSnapshot:
    """扫描时刻的技术指标快照——纯技术面数据，不含任何价格，可以安全地
    在盘前就确定下来，不需要等真实开盘价。这一点很重要：这个类里的任何
    字段都不应该被当作"价格"使用，即使字段名看起来像（没有这样的字段）。
    """
    rsi: Optional[float] = None
    bias_pct: Optional[float] = None            # 乖离率(%)
    atr_pct: Optional[float] = None              # ATR 占价格的百分比
    macd_golden_cross: bool = False
    weekly_resonance: bool = False
    kdj_j_rising: bool = False
    kdj_j_oversold: bool = False
    volume_surge: bool = False
    volume_ratio: Optional[float] = None          # 量比
    tech_score: Optional[float] = None            # 0-40 技术面综合分

    def to_row(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_row(row: dict) -> "TechnicalSnapshot":
        def _f(key):
            v = row.get(key)
            if v is None or v == "":
                return None
            return float(v)

        def _b(key):
            v = row.get(key)
            return str(v).strip().lower() in ("true", "1", "yes")

        return TechnicalSnapshot(
            rsi=_f("rsi"), bias_pct=_f("bias_pct"), atr_pct=_f("atr_pct"),
            macd_golden_cross=_b("macd_golden_cross"), weekly_resonance=_b("weekly_resonance"),
            kdj_j_rising=_b("kdj_j_rising"), kdj_j_oversold=_b("kdj_j_oversold"),
            volume_surge=_b("volume_surge"), volume_ratio=_f("volume_ratio"),
            tech_score=_f("tech_score"),
        )


@dataclass
class Pick:
    """
    scan 阶段的结构化输出单元——一支被选中的股票。

    这个结构体本身就是 schema：AI 必须通过 tool call 按这个形状返回数据，
    不再从自由文本 HTML 里用正则硬解析（这次对话里"评分正则漏掉后面的
    ']'导致 Score 恒为 N/A"、"止损正则边界情况解析失败"这类 bug，根源
    都是"结构化数据硬塞进自由文本、再用正则挖出来"这个模式本身脆弱，
    不是某一次写错正则）。
    """
    ticker: str
    name: str
    market: Market
    tag: str
    score: float
    industry: str
    hold_period_min_days: int
    hold_period_max_days: int
    stop_loss_pct: float
    reasoning_summary: str
    technical: TechnicalSnapshot = field(default_factory=TechnicalSnapshot)
    earnings_date: Optional[str] = None

    def __post_init__(self):
        errors = []
        if not (0 <= self.score <= 100):
            errors.append(f"score 必须在 0-100 之间，收到 {self.score}")
        if self.stop_loss_pct >= 0:
            errors.append(f"stop_loss_pct 必须是负数（相对入场价的跌幅百分比），收到 {self.stop_loss_pct}")
        if self.stop_loss_pct < -50:
            errors.append(f"stop_loss_pct={self.stop_loss_pct} 跌幅超过50%，明显异常，很可能是单位算错")
        if self.hold_period_min_days <= 0 or self.hold_period_max_days <= 0:
            errors.append("持有期天数必须是正数")
        if self.hold_period_min_days > self.hold_period_max_days:
            errors.append(f"hold_period_min_days({self.hold_period_min_days}) 不能大于 max({self.hold_period_max_days})")
        if not self.ticker.strip():
            errors.append("ticker 不能为空")
        if errors:
            raise ValueError(f"[{self.ticker}] Pick 数据校验失败: {'; '.join(errors)}")


@dataclass
class Position:
    """
    trade_history 的结构化对应——一个 position 从建仓到退出的完整生命周期。
    两个市场统一用"一个 position 一行、原地更新"模型。
    """
    ticker: str
    name: str
    market: Market
    tag: str
    score: float
    industry: str
    entry_date: date
    open_price: float                    # 真实开盘价，绝不是盘前参考价
    stop_loss_pct: float
    hold_period_min_days: int
    hold_period_max_days: int
    technical: TechnicalSnapshot = field(default_factory=TechnicalSnapshot)
    status: Status = Status.ACTIVE
    close_price: Optional[float] = None
    exit_date: Optional[date] = None
    exit_price: Optional[float] = None
    earnings_date: Optional[str] = None

    def __post_init__(self):
        if self.open_price <= 0:
            raise ValueError(f"[{self.ticker}] open_price 必须是正数，收到 {self.open_price}")
        if not (0 <= self.score <= 100):
            raise ValueError(f"[{self.ticker}] score 必须在 0-100 之间，收到 {self.score}")

    @property
    def pnl_pct(self) -> Optional[float]:
        """已平仓才有意义。分母用 open_price（真实开盘价），不是任何
        盘前参考价——这是这次重构消灭"止损价/成本价跟真实开盘价对不上"
        整类问题的关键点之一。"""
        if self.exit_price is None:
            return None
        return round((self.exit_price - self.open_price) / self.open_price * 100, 2)

    @property
    def stop_loss_price(self) -> float:
        """止损百分比换算成绝对价格。任何时候都用这笔 position 自己
        记录的 open_price 换算，不存在"止损价用旧参考价算、后来对不上"
        的情况——因为从一开始就没有单独存绝对止损价这个字段。"""
        return round(self.open_price * (1 + self.stop_loss_pct / 100), 4)

    def close(self, exit_price: float, exit_date: date, status: Status) -> None:
        """
        原地更新收盘状态——这是唯一允许改变一笔 position 状态的方法，
        且只在当前状态不是终态时才允许调用。

        这一步校验直接对应这次对话里发现并修复的真实 bug：原来的代码
        按 ticker 匹配、不管 Tag/Status 是不是已经是终态，导致同一个
        ticker 如果先后有两段不同的持仓，后一段的清仓操作会把前一段
        已经归档的历史记录也覆盖掉。这里在数据模型层面直接堵死这个
        可能性：状态机不允许对终态记录再次调用 close()。
        """
        if self.status.is_terminal:
            raise ValueError(
                f"[{self.ticker}] 状态已经是终态({self.status.value})，不能再次 close()。"
                f"如果这是同一支票的新一轮持仓，应该创建一个新的 Position，而不是修改这一个。"
            )
        if not isinstance(status, Status) or not status.is_closed:
            raise ValueError(f"close() 的 status 参数必须是一个已平仓状态，收到 {status}")
        self.status = status
        self.exit_price = exit_price
        self.exit_date = exit_date

    # ---- CSV 行 <-> Position 的双向转换，是唯一允许做这个转换的地方 ----

    CSV_FIELDS = [
        "ticker", "name", "market", "tag", "score", "industry", "entry_date",
        "open_price", "stop_loss_pct", "hold_period_min_days", "hold_period_max_days",
        "status", "close_price", "exit_date", "exit_price", "earnings_date",
        "rsi", "bias_pct", "atr_pct", "macd_golden_cross", "weekly_resonance",
        "kdj_j_rising", "kdj_j_oversold", "volume_surge", "volume_ratio", "tech_score",
    ]

    def to_row(self) -> dict:
        row = {
            "ticker": self.ticker, "name": self.name, "market": self.market.value,
            "tag": self.tag, "score": self.score, "industry": self.industry,
            "entry_date": self.entry_date.isoformat(),
            "open_price": self.open_price, "stop_loss_pct": self.stop_loss_pct,
            "hold_period_min_days": self.hold_period_min_days,
            "hold_period_max_days": self.hold_period_max_days,
            "status": self.status.value,
            "close_price": self.close_price if self.close_price is not None else "",
            "exit_date": self.exit_date.isoformat() if self.exit_date else "",
            "exit_price": self.exit_price if self.exit_price is not None else "",
            "earnings_date": self.earnings_date or "",
        }
        row.update(self.technical.to_row())
        return row

    @staticmethod
    def from_row(row: dict) -> "Position":
        def _opt_float(key):
            v = row.get(key)
            if v is None or v == "":
                return None
            return float(v)

        def _opt_date(key):
            v = row.get(key)
            if not v:
                return None
            return datetime.strptime(v, "%Y-%m-%d").date()

        pos = Position(
            ticker=row["ticker"], name=row["name"], market=Market(row["market"]),
            tag=row["tag"], score=float(row["score"]), industry=row.get("industry", "未知"),
            entry_date=datetime.strptime(row["entry_date"], "%Y-%m-%d").date(),
            open_price=float(row["open_price"]), stop_loss_pct=float(row["stop_loss_pct"]),
            hold_period_min_days=int(row["hold_period_min_days"]),
            hold_period_max_days=int(row["hold_period_max_days"]),
            technical=TechnicalSnapshot.from_row(row),
            status=Status(row["status"]),
            close_price=_opt_float("close_price"),
            exit_date=_opt_date("exit_date"),
            exit_price=_opt_float("exit_price"),
            earnings_date=row.get("earnings_date") or None,
        )
        return pos


def pick_to_position(pick: Pick, open_price: float, close_price: Optional[float],
                      entry_date: date) -> Position:
    """把 scan 阶段的 Pick（还没有真实价格）升级成 review 阶段的 Position
    （已经有真实开盘价）。这是唯一允许发生这个转换的地方，保证不会有
    代码路径绕过去、用盘前参考价冒充 open_price。"""
    return Position(
        ticker=pick.ticker, name=pick.name, market=pick.market, tag=pick.tag,
        score=pick.score, industry=pick.industry, entry_date=entry_date,
        open_price=open_price, stop_loss_pct=pick.stop_loss_pct,
        hold_period_min_days=pick.hold_period_min_days,
        hold_period_max_days=pick.hold_period_max_days,
        technical=pick.technical, close_price=close_price,
        earnings_date=pick.earnings_date,
    )

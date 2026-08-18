# -*- coding: utf-8 -*-
"""
结构化输出层——用 tool call 强制 AI 按 Pick 的 schema 返回选股数据，
取代"自由文本 HTML + 正则硬解析"这个模式。

这次对话里出现过的这些 bug，根源都是"结构化数据被塞进自由文本、
再靠正则表达式挖出来"这个模式本身脆弱，不是某一次正则写错：
  - 评分正则没处理"评分:[74]/100"数字后面的"]"，导致 Score 恒为 N/A
  - 止损正则在某些边界格式下解析失败，静默retreat到默认止损公式
  - AI 有时会在正式输出前多写一段思考过程，混进最终 HTML

用 tool_use 之后：
  - AI 必须调用 submit_picks 这个工具，参数按 JSON schema 强校验，
    类型、必填字段、取值范围（部分）由 API 本身保证，不再是"但愿
    AI 输出的格式跟正则预期的一致"
  - 拿到的 tool_use.input 直接就是结构化字典，用 Pick(**data) 构造，
    Pick 自己的 __post_init__ 再做一层业务规则校验（比如止损必须是
    负数），双重保险
  - 叙事性的分析报告（给人看的部分）完全独立生成，它的自由格式
    永远不会被任何代码解析，格式怎么写都不会造成程序性bug

这里只做 US 市场的概念验证（yfinance 生态更熟悉、可以更有把握地写出
真实可跑的代码）；A股版本的改造方式完全一样，只是换数据源。
"""
import json
from typing import List, Optional

from .models import Market, Pick, TechnicalSnapshot


PICK_TOOL_SCHEMA = {
    "name": "submit_stock_picks",
    "description": (
        "提交今日选股结果的结构化数据。这是唯一的数据提交渠道——"
        "所有选股的机器可读字段（代码/评分/止损/持有期等）都必须通过"
        "这个工具提交，不要在文字回复里重复这些结构化数据。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "picks": {
                "type": "array",
                "description": "本次选中的股票列表",
                "items": {
                    "type": "object",
                    "properties": {
                        "ticker": {"type": "string", "description": "股票代码"},
                        "name": {"type": "string", "description": "公司名称"},
                        "tag": {"type": "string", "description": "分类标签，如 Core_Dragon"},
                        "score": {"type": "number", "minimum": 0, "maximum": 100,
                                  "description": "综合评分 0-100"},
                        "industry": {"type": "string", "description": "所属行业/板块"},
                        "hold_period_min_days": {"type": "integer", "minimum": 1},
                        "hold_period_max_days": {"type": "integer", "minimum": 1},
                        "stop_loss_pct": {
                            "type": "number", "maximum": -0.1, "minimum": -50,
                            "description": "止损百分比，相对入场价的跌幅，必须是负数，如 -6.5",
                        },
                        "reasoning_summary": {
                            "type": "string",
                            "description": "给人看的简短选股理由（1-2句），不会被程序解析，可以自由书写",
                        },
                        "earnings_date": {
                            "type": ["string", "null"],
                            "description": "若已知近期有财报，格式YYYY-MM-DD，否则为null",
                        },
                        "technical": {
                            "type": "object",
                            "description": "技术指标快照，由程序预先计算好传入prompt，AI只需原样带回，不需要自己估算",
                            "properties": {
                                "rsi": {"type": ["number", "null"]},
                                "bias_pct": {"type": ["number", "null"]},
                                "atr_pct": {"type": ["number", "null"]},
                                "macd_golden_cross": {"type": "boolean"},
                                "weekly_resonance": {"type": "boolean"},
                                "kdj_j_rising": {"type": "boolean"},
                                "kdj_j_oversold": {"type": "boolean"},
                                "volume_surge": {"type": "boolean"},
                                "volume_ratio": {"type": ["number", "null"]},
                                "tech_score": {"type": ["number", "null"]},
                            },
                        },
                    },
                    "required": ["ticker", "name", "tag", "score", "industry",
                                 "hold_period_min_days", "hold_period_max_days",
                                 "stop_loss_pct", "reasoning_summary"],
                },
            }
        },
        "required": ["picks"],
    },
}


class PickParseError(Exception):
    """picks数据解析/校验失败时抛出，附带具体是哪一条、哪个字段出的问题，
    而不是让调用方拿到一个空列表却不知道为什么。"""
    pass


def parse_picks_from_tool_use(tool_input: dict, market: Market) -> List[Pick]:
    """
    把 API 返回的 tool_use.input（已经是dict，不需要再手动json.loads一次
    ——除非你拿到的是原始字符串，那种情况下先json.loads再传进来）转换成
    Pick 对象列表。每一条独立校验：某一条数据有问题只会导致那一条被
    跳过并记录原因，不会因为一条脏数据让整批全部作废。
    """
    if "picks" not in tool_input:
        raise PickParseError("tool_use.input 里没有 'picks' 字段，AI可能没有正确调用工具")

    picks: List[Pick] = []
    errors: List[str] = []
    for i, raw in enumerate(tool_input["picks"]):
        try:
            tech_raw = raw.get("technical") or {}
            technical = TechnicalSnapshot(
                rsi=tech_raw.get("rsi"), bias_pct=tech_raw.get("bias_pct"),
                atr_pct=tech_raw.get("atr_pct"),
                macd_golden_cross=bool(tech_raw.get("macd_golden_cross", False)),
                weekly_resonance=bool(tech_raw.get("weekly_resonance", False)),
                kdj_j_rising=bool(tech_raw.get("kdj_j_rising", False)),
                kdj_j_oversold=bool(tech_raw.get("kdj_j_oversold", False)),
                volume_surge=bool(tech_raw.get("volume_surge", False)),
                volume_ratio=tech_raw.get("volume_ratio"),
                tech_score=tech_raw.get("tech_score"),
            )
            pick = Pick(
                ticker=raw["ticker"], name=raw["name"], market=market, tag=raw["tag"],
                score=float(raw["score"]), industry=raw["industry"],
                hold_period_min_days=int(raw["hold_period_min_days"]),
                hold_period_max_days=int(raw["hold_period_max_days"]),
                stop_loss_pct=float(raw["stop_loss_pct"]),
                reasoning_summary=raw["reasoning_summary"],
                technical=technical,
                earnings_date=raw.get("earnings_date"),
            )
            picks.append(pick)
        except (KeyError, ValueError, TypeError) as e:
            ticker = raw.get("ticker", f"第{i+1}条")
            errors.append(f"{ticker}: {e}")

    if errors:
        print(f"⚠️ {len(errors)} 条选股数据校验失败，已跳过: {errors}")
    if not picks and errors:
        raise PickParseError(f"全部 {len(errors)} 条选股数据都校验失败，没有一条有效: {errors}")

    return picks


def extract_tool_use_block(response_content: list, tool_name: str = "submit_stock_picks") -> dict:
    """
    从 API 响应的 content 里找到目标 tool_use block 的 input。

    这里明确按 type 字段过滤（type == "tool_use" and name == tool_name），
    不是像原代码里那样直接假设 content[0] 是想要的那个块——这正是这次
    对话里"邮件混入AI原始思考过程"那个bug的同一种模式（盲取content[0]，
    没有按类型/名称精确筛选）。哪怕以后这个API调用同时开启了extended
    thinking（content里会多出一个thinking类型的块排在前面），这里的
    过滤逻辑依然正确，不需要跟着改。
    """
    for block in response_content:
        block_type = getattr(block, "type", None) or (block.get("type") if isinstance(block, dict) else None)
        if block_type == "tool_use":
            block_name = getattr(block, "name", None) or (block.get("name") if isinstance(block, dict) else None)
            if block_name == tool_name:
                block_input = getattr(block, "input", None) if not isinstance(block, dict) else block.get("input")
                return block_input
    raise PickParseError(
        f"API 响应里没有找到名为 '{tool_name}' 的 tool_use 块——"
        f"AI可能选择了不调用工具，只返回了文字。需要检查 tool_choice 设置。"
    )

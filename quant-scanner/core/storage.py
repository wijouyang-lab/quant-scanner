# -*- coding: utf-8 -*-
"""
统一的 CSV 读写层。

用 Python 内置的 csv 模块做真正的读写（而不是像之前四个 review.py/scan.py
里那样手动 f"{a},{b},{c}" 拼接字符串），这消灭了一整类"字段里如果碰巧
带逗号或换行，行就错位"的潜在风险——这次对话里检查过当前数据没有触发
这个问题，但也没有真正修掉，只是运气好当前数据里没有逗号。

迁移机制：只有一种情况需要处理——磁盘上的 CSV 表头字段集合是
Position.CSV_FIELDS 的真子集（老版本 schema，缺几个新加的列）。这时候
用 csv.DictReader 读、缺失字段自动变成 None，Position.from_row 本身就
处理了这种情况（见 models.py 的测试），不需要像之前那样每加一个新字段
就要在四个文件里分别手写一份"给老数据补空列"的迁移代码。
"""
import csv
import json
import os
from dataclasses import asdict
from typing import List, Optional

from .models import Position, Status, Pick, Market, TechnicalSnapshot


def load_positions(path: str) -> List[Position]:
    """读取全部 Position。单行解析失败不会导致整个文件读取失败——
    跳过那一行、打印警告、继续处理其余的行，这是刻意的设计：一条脏
    数据不应该让整个下游流程（比如 evolve.py 的胜率统计）连带失败。
    """
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return []
    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    positions: List[Position] = []
    skipped = 0
    for row in rows:
        try:
            positions.append(Position.from_row(row))
        except Exception as e:
            skipped += 1
            print(f"⚠️ 跳过一行无法解析的记录 [{row.get('ticker', '?')}]: {e}")
    if skipped:
        print(f"⚠️ 共跳过 {skipped} 行无法解析的记录（不影响其余记录的读取）")
    return positions


def save_positions(path: str, positions: List[Position]) -> None:
    """整体重写——用于状态更新后的整体保存。用临时文件+原子替换，
    避免程序中途崩溃导致 trade_history 文件本身损坏成半写状态。"""
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=Position.CSV_FIELDS)
        writer.writeheader()
        for pos in positions:
            writer.writerow(pos.to_row())
    os.replace(tmp_path, path)


def append_position(path: str, position: Position) -> None:
    """追加一笔新 position（scan → review 补充成交价之后的写入路径）。"""
    need_header = not os.path.exists(path) or os.path.getsize(path) == 0
    with open(path, "a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=Position.CSV_FIELDS)
        if need_header:
            writer.writeheader()
        writer.writerow(position.to_row())


def find_active(positions: List[Position], ticker: str):
    """
    查找某个 ticker 当前是否有一笔 Active 状态的 position。scan 阶段用
    这个来判断"这支票是不是已经持有了"，避免重复建仓。

    这次对话里 MU 被重复选中 9 次，根源之一就是这类判断在旧代码里因为
    Status 经常是脏数据（NaN，源于另一个 schema 错位 bug）而失效。这里
    如果发现同一个 ticker 有 2 笔以上同时 Active，直接报错而不是安静
    选第一个——说明别的地方在建仓前没有正确检查已有持仓，这种情况应该
    在这里就被看见，而不是被优雅地掩盖掉直到造成更大的数据混乱。
    """
    matches = [p for p in positions if p.ticker == ticker and p.status == Status.ACTIVE]
    if len(matches) > 1:
        raise RuntimeError(
            f"[{ticker}] 发现 {len(matches)} 笔同时处于 Active 状态的 position，"
            f"这不应该发生——说明建仓前没有正确检查是否已持有同一支票。"
        )
    return matches[0] if matches else None


def frozen_min_score(positions: List[Position]) -> dict:
    """
    计算每个曾经止损/强清过的 ticker，需要达到多少分才能重新入选。

    取代原来"历史 Score 缺失时锁死为 inf、永久拉黑"那个虽然是刻意的
    保守设计、但现在已经用不上的逻辑——因为 Pick 在构造时就做了
    0-100 范围校验和空值拒绝（见 models.py），不可能再出现 NaN Score
    流进 trade_history 这种情况了，所以也就不再需要那层"数据不完整时
    要不要保守拉黑"的额外判断。
    """
    REQUALIFY_MARGIN = 10.0
    result = {}
    for p in positions:
        if p.status in (Status.STOP_LOSS_HIT, Status.FORCED_EXIT):
            key = (p.market.value, p.ticker)
            threshold = p.score + REQUALIFY_MARGIN
            if key not in result or threshold > result[key]:
                result[key] = threshold
    return result


# ---------------------------------------------------------------
# Pending picks（scan → review 交接文件）
#
# 用 JSON 而不是 CSV：Pick 带一个嵌套的 TechnicalSnapshot，摊平成 CSV
# 列没有实际好处，反而要多写一层"嵌套字段怎么摊平/怎么还原"的代码。
# 这个文件只在 scan.py 和 review.py 之间短暂存在（review 处理完就
# 改名成 .processed），不是长期保存的账本，不需要 CSV 那种可以直接
# 用 Excel 打开看的便利性。
# ---------------------------------------------------------------

def save_pending_picks(path: str, picks: List[Pick], entry_date: str) -> None:
    """entry_date: 'YYYY-MM-DD'，随文件一起存，review阶段不需要自己再
    从文件名解析日期（原来的实现是从文件名 ashare_stocks_pending_20260813.csv
    里正则抠日期，这里直接结构化存进内容本身，更不容易出错）。"""
    data = {
        "entry_date": entry_date,
        "picks": [
            {**asdict(p), "market": p.market.value}  # Enum 不能直接json序列化，转成值
            for p in picks
        ],
    }
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def load_pending_picks(path: str):
    """返回 (entry_date: str, picks: List[Pick])。单条数据解析失败会被
    跳过并记录，不会让整个文件读取失败。"""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    entry_date = data["entry_date"]
    picks: List[Pick] = []
    for raw in data.get("picks", []):
        try:
            tech_raw = raw.get("technical") or {}
            technical = TechnicalSnapshot(**tech_raw)
            pick = Pick(
                ticker=raw["ticker"], name=raw["name"], market=Market(raw["market"]),
                tag=raw["tag"], score=raw["score"], industry=raw["industry"],
                hold_period_min_days=raw["hold_period_min_days"],
                hold_period_max_days=raw["hold_period_max_days"],
                stop_loss_pct=raw["stop_loss_pct"],
                reasoning_summary=raw["reasoning_summary"],
                technical=technical,
                earnings_date=raw.get("earnings_date"),
            )
            picks.append(pick)
        except Exception as e:
            print(f"⚠️ 跳过一条无法解析的pending pick [{raw.get('ticker','?')}]: {e}")

    return entry_date, picks


def list_pending_files(directory: str, market: Market) -> List[str]:
    """列出所有还没处理过的pending文件（不含.processed后缀）。明确扫描
    目录、按前缀+后缀过滤，而不是只拼出"今天"这一个文件名去检查存不
    存在——那种写法导致任何一天处理失败，那天的文件就永远不会被重试，
    这是这次对话里修过的核心bug模式，新架构从一开始就不会有这个问题。"""
    prefix = f"pending_picks_{market.value.lower()}_"
    if not os.path.isdir(directory):
        return []
    matches = [
        os.path.join(directory, f) for f in os.listdir(directory)
        if f.startswith(prefix) and f.endswith(".json") and not f.endswith(".processed.json")
    ]
    return sorted(matches)

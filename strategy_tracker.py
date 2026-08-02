"""期權策略到期損益與歷史績效的純計算工具。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


Outcome = Literal["WIN", "LOSS"]
StrategyType = Literal["credit", "debit"]


@dataclass
class StrategyOutcome:
    """一組期權策略在到期結算後的實際結果。"""

    outcome: Outcome
    realized_pnl: float
    max_loss_hit: bool


def score_outcome(
    legs: list[dict],
    net_premium: float,
    strategy_type: StrategyType,
    settlement_spot: float,
    max_loss: float,
) -> StrategyOutcome:
    """依每隻腳的到期內在價值計算一組策略的實現損益。

    到期時選擇權只剩內在價值：BUY 腳持有人可取得該價值，SELL 腳則有
    履約交付義務、必須付出該價值。Credit 策略先收到權利金，Debit 策略
    先付出權利金，因此兩者都以各腳淨內在價值加減最初權利金得到最終損益。
    金額維持每一組／每一口單位，不乘以合約的 100 股乘數。
    """
    bought_intrinsic = 0.0
    sold_intrinsic = 0.0

    for leg in legs:
        strike = leg["strike_price"]
        if leg["option_type"] == "CALL":
            intrinsic_value = max(settlement_spot - strike, 0.0)
        else:
            intrinsic_value = max(strike - settlement_spot, 0.0)

        # BUY 腳取得內在價值；SELL 腳承擔相同價值的履約支出。
        if leg["action"] == "BUY":
            bought_intrinsic += intrinsic_value
        else:
            sold_intrinsic += intrinsic_value

    net_intrinsic = bought_intrinsic - sold_intrinsic
    if strategy_type == "credit":
        # Credit 策略的收入起點是收到的權利金，再扣除到期淨履約支出。
        realized_pnl = net_premium + net_intrinsic
    else:
        # Debit 策略必須先回收付出的權利金，超過成本的內在價值才是獲利。
        realized_pnl = net_intrinsic - net_premium

    outcome: Outcome = "WIN" if realized_pnl > 0 else "LOSS"
    # 結算值落在最大虧損界線 0.01 以內即視為觸頂，吸收常見的小數誤差。
    max_loss_hit = realized_pnl <= (-max_loss + 0.01)

    return StrategyOutcome(
        outcome=outcome,
        realized_pnl=realized_pnl,
        max_loss_hit=max_loss_hit,
    )


def summarize_track_record(records: list[dict]) -> dict:
    """彙總整體及各策略的結算勝率與累積損益。"""
    total_count = len(records)
    win_count = sum(record["outcome"] == "WIN" for record in records)
    total_pnl = sum(record["realized_pnl"] for record in records)

    # 空紀錄的勝率定義為 0，避免沒有分母時產生除以零。
    win_rate_pct = round(win_count / total_count * 100, 1) if total_count else 0.0

    strategy_totals: dict[str, dict[str, int | float]] = {}
    for record in records:
        strategy_name = record["strategy_name"]
        if strategy_name not in strategy_totals:
            strategy_totals[strategy_name] = {
                "count": 0,
                "win_count": 0,
                "total_pnl": 0.0,
            }

        group = strategy_totals[strategy_name]
        group["count"] += 1
        group["win_count"] += record["outcome"] == "WIN"
        group["total_pnl"] += record["realized_pnl"]

    by_strategy = {}
    for strategy_name, group in strategy_totals.items():
        count = group["count"]
        by_strategy[strategy_name] = {
            "count": count,
            "win_rate_pct": round(group["win_count"] / count * 100, 1),
            "total_pnl": group["total_pnl"],
        }

    return {
        "total_count": total_count,
        "win_count": win_count,
        "win_rate_pct": win_rate_pct,
        "total_pnl": total_pnl,
        "by_strategy": by_strategy,
    }

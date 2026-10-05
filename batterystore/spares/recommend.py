"""最低库存与补货建议。

建议每次都用当前全部更换记录即时计算，补录进来后自然重算，不存在"刷新建议"
的额外动作。最低库存取再订货点：采购到货周期内的期望消耗加安全库存；目标
库存按目标覆盖天数备货。建议采购量只补足到目标库存，并把在途采购单计入
头寸，避免同一波检修重复下单。
"""

from __future__ import annotations

import math

from ..config import SparesLimits
from .rate import DAY_SECONDS, RateEstimate

URGENCY_STOCKOUT = "stockout"
URGENCY_URGENT = "urgent"
URGENCY_REORDER = "reorder"
URGENCY_OK = "ok"


def _ceil_qty(value: float) -> float:
    """整件备件按整件订货，数量向上取整。"""

    return float(math.ceil(value - 1e-9))


def build_recommendation(
    estimate: RateEstimate,
    on_hand: float,
    open_incoming: float,
    limits: SparesLimits,
    *,
    part_id: str,
    scope: str,
    unit: str,
    lead_time_days: float | None = None,
) -> dict:
    """把速率估计、库存与在途量合成一条补货建议。"""

    lead_days = limits.lead_time_days if lead_time_days is None else float(lead_time_days)
    rate = estimate.selected_rate
    lead_demand = rate * lead_days
    safety_stock = limits.service_level_z * math.sqrt(max(lead_demand, 0.0))
    reorder_point = lead_demand + safety_stock
    target_stock = rate * limits.target_coverage_days + safety_stock
    position = on_hand + open_incoming
    raw_qty = target_stock - position
    suggest_qty = _ceil_qty(raw_qty) if raw_qty > 0.0 else 0.0
    coverage_days = (on_hand / rate) if rate > 0.0 else None

    if on_hand < 0.0:
        urgency = URGENCY_STOCKOUT
    elif on_hand <= lead_demand:
        urgency = URGENCY_URGENT
    elif position <= reorder_point:
        urgency = URGENCY_REORDER
    else:
        urgency = URGENCY_OK

    messages: list[str] = []
    if urgency == URGENCY_STOCKOUT:
        messages.append(f"已缺件 {-on_hand:g}{unit}，在补到货前相关检修只能停修等待")
    elif urgency == URGENCY_URGENT:
        messages.append("可用库存已低于到货周期内的期望消耗，需立即补货")
    elif urgency == URGENCY_REORDER:
        messages.append("库存已到再订货点，建议在本轮周期内下单")
    else:
        messages.append("库存高于再订货点，暂不需要补货")
    if open_incoming > 0.0:
        messages.append(f"已有在途采购 {open_incoming:g}{unit}，已计入补货头寸")

    return {
        "part_id": part_id,
        "scope": scope,
        "unit": unit,
        "basis": estimate.selected_basis,
        "rate_per_day": round(rate, 8),
        "window_rate_per_day": round(estimate.window_rate, 8),
        "cycle_rate_per_day": round(estimate.cycle_rate, 8),
        "season_active": estimate.season_active,
        "season_month": estimate.season_month,
        "low_confidence": estimate.low_confidence,
        "reasons": list(estimate.reasons),
        "lead_time_days": round(lead_days, 4),
        "lead_demand": round(lead_demand, 6),
        "safety_stock": round(safety_stock, 6),
        "min_stock": round(reorder_point, 6),
        "target_stock": round(_ceil_qty(target_stock), 6),
        "on_hand": round(on_hand, 6),
        "open_incoming": round(open_incoming, 6),
        "position": round(position, 6),
        "suggest_qty": suggest_qty,
        "coverage_days": round(coverage_days, 4) if coverage_days is not None else None,
        "urgency": urgency,
        "messages": messages,
    }


def expected_arrival_ts(issued_ts: float, lead_time_days: float) -> float:
    """按下发时间与到货周期算预计到货时间。"""

    return issued_ts + lead_time_days * DAY_SECONDS

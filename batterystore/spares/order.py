"""采购单与重算差异提示。

采购单一经下发即冻结：补录更换事件后建议会重算，但任何在途或已收货采购单的
数量都不允许被悄悄改写。重算结果与采购单只做对照，产出差异提示，由人工决定
是否追单或提前催货。
"""

from __future__ import annotations

DAY_SECONDS = 86_400.0
DIFF_EXCESS = "excess"
DIFF_SHORT = "short"
DIFF_MATCH = "match"


def compare_order(order: dict, current_suggest_qty: float, rate: float, now: float) -> dict:
    """把一条即时重算的建议与一张已下发采购单对照。

    比较口径是"当前建议采购量"与"该单尚未到货数量"：建议变大给出追单提示，
    建议变小给出可能积压提示，采购单本身不做任何修改。
    """

    remaining = float(order.get("remaining", max(0.0, float(order["qty"]) - float(order.get("received", 0.0)))))
    delta = round(current_suggest_qty - remaining, 6)
    if delta > 0.0:
        kind = DIFF_SHORT
        message = f"补录/重算后该单未交量还差 {delta:g} 才能补到目标库存，建议追加采购或催货"
    elif delta < 0.0:
        kind = DIFF_EXCESS
        message = f"重算后该单未交量比目标所需多 {-delta:g}，到货后可能形成积压，请评估改期或改拨其他站"
    else:
        kind = DIFF_MATCH
        message = "重算建议与该单未交量一致"
    expected_ts = float(order.get("expected_ts", order["issued_ts"]))
    overdue = bool(order.get("status") == "open" and now > expected_ts)
    return {
        "order_id": order["order_id"],
        "part_id": order["part_id"],
        "station": order["station"],
        "status": order.get("status", "open"),
        "ordered_qty": float(order["qty"]),
        "received_qty": float(order.get("received", 0.0)),
        "remaining_qty": round(remaining, 6),
        "current_suggest_qty": float(current_suggest_qty),
        "current_rate_per_day": round(rate, 8),
        "delta_qty": delta,
        "diff": kind,
        "issued_ts": order["issued_ts"],
        "expected_ts": round(expected_ts, 3),
        "overdue": overdue,
        "overdue_days": round((now - expected_ts) / DAY_SECONDS, 4) if overdue else 0.0,
        "frozen": True,
        "message": message,
    }

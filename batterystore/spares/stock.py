"""备件库存账。

库存不单独维护计数，而是由初始库存、更换消耗与到货收货三类流水推导：每发生
一次更换就立即扣减，仓库没货时允许出现负库存，负值即缺件停修的挂账数量；
到货后先补挂账再回补可用库存。这样"缺件停修"与"仓库压一堆"在同一份账上
都能直接看出来，补录历史更换也会如实反映到账上。
"""

from __future__ import annotations

from dataclasses import dataclass

DAY_SECONDS = 86_400.0


@dataclass(frozen=True)
class StockState:
    """某备件在某站的库存结果。"""

    on_hand: float
    shortage_qty: float
    consumed_total: float
    received_total: float
    stockout_days: float
    last_move_ts: float | None

    def as_dict(self) -> dict:
        return {
            "on_hand": round(self.on_hand, 6),
            "shortage_qty": round(self.shortage_qty, 6),
            "consumed_total": round(self.consumed_total, 6),
            "received_total": round(self.received_total, 6),
            "stockout_days": round(self.stockout_days, 4),
            "last_move_ts": round(self.last_move_ts, 3) if self.last_move_ts is not None else None,
            "in_stockout": self.on_hand < 0.0,
        }


def derive_stock(movements: list[tuple[float, float, str]], initial: float, now: float) -> StockState:
    """按时间顺序回放库存流水。

    每条流水是 ``(时间戳, 数量变动, 类型)``：消耗传负数、收货与初始传正数；
    类型只用于汇总消耗/收货小计。库存为负的区间按天累计成缺件时长。
    """

    balance = float(initial)
    consumed = 0.0
    received = 0.0
    stockout_days = 0.0
    last_ts: float | None = None
    ordered = sorted(movements, key=lambda item: item[0])
    for ts, delta, kind in ordered:
        if last_ts is not None and balance < 0.0:
            stockout_days += max(0.0, (ts - last_ts) / DAY_SECONDS)
        balance += delta
        if kind == "consume":
            consumed += -delta
        elif kind == "receive":
            received += delta
        last_ts = ts
    if last_ts is not None and balance < 0.0:
        stockout_days += max(0.0, (now - last_ts) / DAY_SECONDS)
    return StockState(
        on_hand=balance,
        shortage_qty=max(0.0, -balance),
        consumed_total=consumed,
        received_total=received,
        stockout_days=stockout_days,
        last_move_ts=last_ts,
    )

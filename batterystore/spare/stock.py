"""最低库存与补货策略。

消耗是成批、间歇的，按"平均日耗 × 提前期"一个点估最低库存必然在检修月缺货。
这里在提前期需求之外再加一档安全库存：把更换记录按天打成日耗桶，用日耗的
标准差刻画检修集中带来的波动，再按目标服务水平取正态分位放大到
``sqrt(提前期 + 复核期)``。

输出四个数：``min_stock`` 为最低库存（再订货点，低于它就该下单）、
``target_stock`` 为补到的目标水位、``suggested_qty`` 为结合当前在手、在途与
欠储后按订货批量取整的建议采购量、``cover_days`` 为现有可用库存还能撑多少天。
建议量为零即"不用补"，仓库压货时还会明确给出"高于目标水位"的状态。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .events import DAY_S, ReplacementEvent
from .parts import PartProfile
from .rate import RateEstimate

REORDER = "reorder"
SHORTAGE = "shortage"
OK = "ok"
OVERSTOCK = "overstock"


def normal_quantile(probability: float) -> float:
    """标准正态分位函数（Acklam 有理逼近），覆盖常用服务水平 0.5~0.999。"""

    if not 0.0 < probability < 1.0:
        raise ValueError("服务水平必须落在 (0, 1) 之间")
    if probability < 0.02425:  # 下尾
        q = math.sqrt(-2.0 * math.log(probability))
        return (
            (((((-7.784894002430293e-3 * q - 0.3223964580411365) * q - 2.400758277161838) * q
                - 2.549732539343734) * q + 4.374664141464968) * q + 2.938163982698783)
            / ((((0.007784695709041462 * q + 0.3224671290700398) * q + 2.445134137142996) * q
                + 3.754408661907416) * q + 1.0)
        )
    if probability <= 0.97575:  # 中部
        q = probability - 0.5
        r = q * q
        return (
            (((((-39.69683028665376 * r + 220.9460984245205) * r - 275.9285104469687) * r
                + 138.3577518672690) * r - 30.66479806614716) * r + 2.506628277459239) * q
        ) / (
            (((((-54.47609879822406 * r + 161.5858368580409) * r - 155.6989798598866) * r
                + 66.80131188771972) * r - 13.28068155288572) * r + 1.0)
        )
    q = math.sqrt(-2.0 * math.log(1.0 - probability))  # 上尾
    return -(
        (((((-7.784894002430293e-3 * q - 0.3223964580411365) * q - 2.400758277161838) * q
            - 2.549732539343734) * q + 4.374664141464968) * q + 2.938163982698783)
        / ((((0.007784695709041462 * q + 0.3224671290700398) * q + 2.445134137142996) * q
            + 3.754408661907416) * q + 1.0)
    )


def daily_demand_std(events: list[ReplacementEvent], now: float, span_days: float) -> float:
    """按天打桶计算日耗标准差（含零消耗的天），刻画成批更换的波动。"""

    bucket_count = max(1, math.ceil(span_days))
    buckets = [0] * bucket_count
    for event in events:
        offset = int((now - event.ts) // DAY_S)
        if 0 <= offset < bucket_count:
            buckets[offset] += event.qty
    mean = sum(buckets) / bucket_count
    variance = sum((value - mean) ** 2 for value in buckets) / bucket_count
    return math.sqrt(variance)


@dataclass(frozen=True)
class StockPolicy:
    """某备件在某口径集合上的库存策略结论。"""

    rate_per_day: float
    basis: str
    lead_demand: float
    safety_stock: float
    min_stock: int
    target_stock: int
    on_hand: int
    backlog: int
    in_transit: int
    available: int
    suggested_qty: int
    cover_days: float | None
    status: str
    reason: str

    def as_dict(self) -> dict:
        return {
            "rate_per_day": round(self.rate_per_day, 6),
            "basis": self.basis,
            "lead_demand": round(self.lead_demand, 3),
            "safety_stock": round(self.safety_stock, 3),
            "min_stock": self.min_stock,
            "target_stock": self.target_stock,
            "on_hand": self.on_hand,
            "backlog": self.backlog,
            "in_transit": self.in_transit,
            "available": self.available,
            "suggested_qty": self.suggested_qty,
            "cover_days": None if self.cover_days is None else round(self.cover_days, 2),
            "status": self.status,
            "reason": self.reason,
        }


def evaluate_policy(
    part: PartProfile,
    estimate: RateEstimate,
    events: list[ReplacementEvent],
    *,
    now: float,
    on_hand: int,
    backlog: int,
    in_transit: int,
    service_level: float,
    review_days: float,
) -> StockPolicy:
    """按速率估计与当前库存位置算最低库存与补货建议。"""

    rate = estimate.rate_per_day
    horizon = part.lead_days + max(0.0, review_days)
    lead_demand = rate * horizon
    span_days = estimate.window_days if estimate.basis == "window" else estimate.cycle_days
    sigma_day = daily_demand_std(events, now, span_days)
    safety = max(0.0, normal_quantile(service_level)) * sigma_day * math.sqrt(horizon)
    min_stock = math.ceil(lead_demand + safety)
    target_stock = max(min_stock, math.ceil(lead_demand + safety + rate * review_days))
    available = on_hand + in_transit
    shortage_gap = max(0, backlog - on_hand)
    net_position = on_hand + in_transit - backlog
    raw_gap = target_stock - net_position
    suggested = part.round_lot(raw_gap) if raw_gap > 0 else 0
    cover_days: float | None = None
    if rate > 0:
        cover_days = max(0.0, (on_hand - backlog) / rate)
    if backlog > on_hand:
        status = SHORTAGE
    elif available <= min_stock or suggested > 0:
        status = REORDER
    elif on_hand > target_stock:
        status = OVERSTOCK
    else:
        status = OK
    reason = estimate.reason
    if shortage_gap:
        reason = f"缺件待补 {shortage_gap} 件；{reason}"
    return StockPolicy(
        rate_per_day=rate,
        basis=estimate.basis,
        lead_demand=lead_demand,
        safety_stock=safety,
        min_stock=min_stock,
        target_stock=target_stock,
        on_hand=on_hand,
        backlog=backlog,
        in_transit=in_transit,
        available=available,
        suggested_qty=suggested,
        cover_days=cover_days,
        status=status,
        reason=reason,
    )

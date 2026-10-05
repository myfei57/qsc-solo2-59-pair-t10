"""消耗速率估计与估计口径切换。

两种口径并存，建议里同时给出，便于核对：

* ``window``：只看最近一个窗口（默认 90 天）的速率，对近期检修强度反应快，
  检修集中月份会明显抬高；
* ``cycle``：整个周期（默认 365 天）的平均速率，平滑掉季节峰，平时更稳。

默认按数据自动选：只有当最近窗口正处于检修季（窗口内存在月份消耗明显高于
周期月均，且倍数达到配置阈值）时采用窗口速率，避免检修月份按周期均值补货而
缺件停修；检修季结束（高峰月份滑出窗口、窗口强度回落到阈值以内）自动切回
周期均值，避免淡季仍按高峰备货压库存。备件目录也可以把口径强制钉成
``window`` 或 ``cycle``，自动结果只作为参考。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import SparesLimits
from ..spares.models import ReplacementEvent

DAY_SECONDS = 86_400.0
CYCLE_WINDOW_MONTHS = 12

BASIS_AUTO = "auto"
BASIS_WINDOW = "window"
BASIS_CYCLE = "cycle"
BASES = (BASIS_AUTO, BASIS_WINDOW, BASIS_CYCLE)


@dataclass(frozen=True)
class RateEstimate:
    """某备件在某统计范围上的速率估计。"""

    part_id: str
    scope: str
    now: float
    window_days: float
    cycle_days: float
    window_rate: float
    cycle_rate: float
    selected_basis: str
    selected_rate: float
    season_active: bool
    season_month: str | None
    season_ratio: float
    peak_ratio: float
    event_count_window: int
    event_count_cycle: int
    low_confidence: bool
    monthly: list[dict]
    reasons: list[str]

    def as_dict(self) -> dict:
        return {
            "part_id": self.part_id,
            "scope": self.scope,
            "now": round(self.now, 3),
            "window_days": self.window_days,
            "cycle_days": self.cycle_days,
            "window_rate_per_day": round(self.window_rate, 8),
            "cycle_rate_per_day": round(self.cycle_rate, 8),
            "selected_basis": self.selected_basis,
            "selected_rate_per_day": round(self.selected_rate, 8),
            "season_active": self.season_active,
            "season_month": self.season_month,
            "season_ratio_threshold": round(self.season_ratio, 4),
            "peak_ratio": round(self.peak_ratio, 4),
            "event_count_window": self.event_count_window,
            "event_count_cycle": self.event_count_cycle,
            "low_confidence": self.low_confidence,
            "monthly": [dict(item) for item in self.monthly],
            "reasons": list(self.reasons),
        }


def month_bucket(ts: float) -> str:
    """把时间戳归到 ``YYYY-MM`` 月桶。"""

    import time

    return time.strftime("%Y-%m", time.gmtime(ts))


def _quantities_in_range(events: list[ReplacementEvent], start: float, end: float) -> list[float]:
    return [event.qty for event in events if start < event.ts <= end]


def _monthly_series(events: list[ReplacementEvent], now: float, months: int) -> list[dict]:
    """按自然月汇总最近 ``months`` 个月（含当月）的消耗量，旧月在前。"""

    import calendar
    import time

    current = time.gmtime(now)
    buckets: list[dict] = []
    year, month = current.tm_year, current.tm_mon
    for offset in range(months - 1, -1, -1):
        shifted_year = year * 12 + (month - 1) - offset
        bucket_year, bucket_month = divmod(shifted_year, 12)
        bucket_month += 1
        buckets.append(
            {
                "month": f"{bucket_year:04d}-{bucket_month:02d}",
                "qty": 0.0,
                "events": 0,
                "days": calendar.monthrange(bucket_year, bucket_month)[1],
            }
        )
    index = {item["month"]: item for item in buckets}
    for event in events:
        item = index.get(month_bucket(event.ts))
        if item is not None:
            item["qty"] += event.qty
            item["events"] += 1
    for item in buckets:
        item["qty"] = round(item["qty"], 6)
    return buckets


def estimate_rate(
    part_id: str,
    events: list[ReplacementEvent],
    now: float,
    limits: SparesLimits,
    *,
    scope: str = "",
    override: str = BASIS_AUTO,
) -> RateEstimate:
    """按窗口与周期两种口径估计每日消耗速率，并给出自动选口结果。"""

    if override not in BASES:
        override = BASIS_AUTO
    window_start = now - limits.window_days * DAY_SECONDS
    cycle_start = now - limits.cycle_days * DAY_SECONDS
    window_qty = sum(_quantities_in_range(events, window_start, now))
    cycle_qty = sum(_quantities_in_range(events, cycle_start, now))
    window_rate = window_qty / limits.window_days
    cycle_rate = cycle_qty / limits.cycle_days
    window_events = _quantities_in_range(events, window_start, now)
    cycle_events = _quantities_in_range(events, cycle_start, now)

    monthly = _monthly_series(events, now, CYCLE_WINDOW_MONTHS)
    monthly_avg = cycle_qty / CYCLE_WINDOW_MONTHS
    peak_item = max(monthly, key=lambda item: item["qty"], default=None)
    peak_ratio = (peak_item["qty"] / monthly_avg) if peak_item is not None and monthly_avg > 0 else 0.0
    # 窗口高峰只能由窗口内的事件按月汇总，避免窗口外但同月的检修被算进来。
    window_monthly: dict[str, float] = {}
    for event in events:
        if window_start < event.ts <= now:
            bucket = month_bucket(event.ts)
            window_monthly[bucket] = window_monthly.get(bucket, 0.0) + event.qty
    window_peak_month = max(window_monthly, key=window_monthly.get, default=None)
    window_peak_qty = window_monthly.get(window_peak_month, 0.0) if window_peak_month else 0.0
    window_has_peak = bool(
        window_peak_month
        and monthly_avg > 0
        and window_peak_qty >= monthly_avg * limits.season_ratio
    )
    # 检修集中月份抬高近期速率：只有近期窗口仍在高峰里、且窗口速率显著高于
    # 周期速率（达到配置倍数）才按窗口备货；稀疏数据下单件更换不会被误判成
    # 检修季。高峰月份已经滑出窗口时，哪怕历史峰再高也切回周期均值。
    season_active = (
        window_has_peak and cycle_rate > 0.0 and window_rate >= cycle_rate * limits.season_ratio
    )

    reasons: list[str] = []
    if override in (BASIS_WINDOW, BASIS_CYCLE):
        selected_basis = override
        reasons.append(f"目录强制口径={override}")
    elif season_active:
        selected_basis = BASIS_WINDOW
        window_peak_ratio = (
            window_peak_qty / monthly_avg if monthly_avg > 0 and window_peak_month else 0.0
        )
        reasons.append(
            f"近窗口处于检修季：{window_peak_month} 月消耗为周期月均的 {window_peak_ratio:.2f} 倍，"
            "按窗口速率备货以防缺件停修"
        )
    else:
        selected_basis = BASIS_CYCLE
        if peak_ratio >= limits.season_ratio:
            reasons.append("检修高峰已滑出近窗口，切回周期均值，避免淡季按高峰压库存")
        else:
            reasons.append("近期无检修集中月份，按全周期平均速率估计")
    selected_rate = window_rate if selected_basis == BASIS_WINDOW else cycle_rate

    low_confidence = len(cycle_events) < limits.min_events
    if low_confidence:
        reasons.append(
            f"周期内仅 {len(cycle_events)} 条更换记录（少于 {limits.min_events} 条），速率可信度低，建议人工复核"
        )

    return RateEstimate(
        part_id=part_id,
        scope=scope,
        now=now,
        window_days=limits.window_days,
        cycle_days=limits.cycle_days,
        window_rate=window_rate,
        cycle_rate=cycle_rate,
        selected_basis=selected_basis,
        selected_rate=selected_rate,
        season_active=season_active,
        season_month=window_peak_month if season_active else None,
        season_ratio=limits.season_ratio,
        peak_ratio=peak_ratio,
        event_count_window=len(window_events),
        event_count_cycle=len(cycle_events),
        low_confidence=low_confidence,
        monthly=monthly,
        reasons=reasons,
    )

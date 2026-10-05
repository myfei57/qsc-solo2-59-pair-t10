"""消耗速率估计与口径切换。

两种口径都从更换记录出发，区别只在取数窗口：

* 窗口速率：最近 ``window_days`` 天的更换量除以窗口长度，检修集中的月份立刻
  抬升，检修过去后立刻回落；
* 周期速率：最近 ``cycle_days`` 天的更换量除以周期长度，把检修峰摊平到整个
  周期。备件档案建立不满一个周期时改用实际观测跨度，避免"只观测了一个月却按
  半年摊"把速率系统性低估。

切换规则（``switch_ratio``，默认 1.5）：两口径都在彼此的 ``ratio`` 倍以内时，
认为消耗平稳，用样本更多的周期均值；窗口速率高于周期均值 ``ratio`` 倍（检修
上量）或低于其 ``1/ratio``（检修刚过、周期均值还被抬高）时，切到窗口口径。
窗口内一条更换都没有时退回周期均值，防止把最低库存估成零。每条结果都带
``reason``，什么时候该切换由数据说了算，不由调用方临时指定；需要复盘时也
可以用 ``basis`` 强制指定口径。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import SpareLimits
from .events import DAY_S, ReplacementEvent

WINDOW = "window"
CYCLE = "cycle"


@dataclass(frozen=True)
class RateEstimate:
    """某备件（在某口径集合上）的速率估计。"""

    basis: str
    rate_per_day: float
    window_rate: float
    cycle_rate: float
    window_days: float
    cycle_days: float
    window_events: int
    cycle_events: int
    window_qty: int
    cycle_qty: int
    observed_days: float
    reason: str

    def as_dict(self) -> dict:
        return {
            "basis": self.basis,
            "rate_per_day": round(self.rate_per_day, 6),
            "window_rate": round(self.window_rate, 6),
            "cycle_rate": round(self.cycle_rate, 6),
            "window_days": self.window_days,
            "cycle_days": self.cycle_days,
            "window_events": self.window_events,
            "cycle_events": self.cycle_events,
            "window_qty": self.window_qty,
            "cycle_qty": self.cycle_qty,
            "observed_days": round(self.observed_days, 3),
            "reason": self.reason,
        }


def rate_stats(
    points: list[tuple[float, int]],
    now: float,
    window_days: float,
    cycle_days: float,
) -> dict:
    """按更换点计算两种口径的原始速率与样本计数。"""

    window_start = now - window_days * DAY_S
    cycle_start = now - cycle_days * DAY_S
    window_qty = 0
    cycle_qty = 0
    window_events = 0
    cycle_events = 0
    earliest = now
    for ts, qty in points:
        if ts < earliest:
            earliest = ts
        if ts > window_start:
            window_qty += qty
            window_events += 1
        if ts > cycle_start:
            cycle_qty += qty
            cycle_events += 1
    if points:
        observed_days = max(1.0, (now - earliest) / DAY_S)
    else:
        observed_days = 0.0
    effective_days = cycle_days if observed_days >= cycle_days else observed_days
    window_rate = window_qty / window_days
    cycle_rate = cycle_qty / effective_days if effective_days > 0 else 0.0
    return {
        "window_rate": window_rate,
        "cycle_rate": cycle_rate,
        "window_events": window_events,
        "cycle_events": cycle_events,
        "window_qty": window_qty,
        "cycle_qty": cycle_qty,
        "observed_days": observed_days,
    }


def choose_basis(window_rate: float, cycle_rate: float, window_events: int, switch_ratio: float) -> tuple[str, str]:
    """按两口径比值决定该用哪一种，返回口径与切换原因。"""

    if window_events == 0:
        if cycle_rate > 0:
            return CYCLE, "窗口内无更换，退回全周期均值，避免最低库存估零"
        return CYCLE, "周期内无更换记录，速率按零处理"
    if cycle_rate <= 0.0:
        return WINDOW, "周期均值为零而最近窗口已有更换，按窗口口径起估"
    ratio = window_rate / cycle_rate
    if ratio >= switch_ratio:
        return WINDOW, f"窗口速率为周期均值的 {ratio:.2f} 倍，检修上量切窗口口径"
    if ratio <= 1.0 / switch_ratio:
        return WINDOW, f"窗口速率仅为周期均值的 {ratio:.2f} 倍，检修已过切窗口口径"
    return CYCLE, f"两口径偏差 {ratio:.2f} 倍在 {switch_ratio:g} 倍带内，消耗平稳用周期均值"


def estimate_rate(
    events: list[ReplacementEvent],
    now: float,
    limits: SpareLimits,
    basis: str | None = None,
) -> RateEstimate:
    """从更换事件估计速率；``basis`` 为 ``window``/``cycle`` 时强制口径。"""

    points = [(event.ts, event.qty) for event in events]
    stats = rate_stats(points, now, limits.window_days, limits.cycle_days)
    auto_basis, reason = choose_basis(
        stats["window_rate"], stats["cycle_rate"], stats["window_events"], limits.switch_ratio
    )
    selected = basis if basis in (WINDOW, CYCLE) else auto_basis
    if basis in (WINDOW, CYCLE):
        reason = f"强制使用{'窗口' if basis == WINDOW else '全周期'}口径（自动口径为 {auto_basis}）"
    rate = stats["window_rate"] if selected == WINDOW else stats["cycle_rate"]
    return RateEstimate(
        basis=selected,
        rate_per_day=rate,
        window_rate=stats["window_rate"],
        cycle_rate=stats["cycle_rate"],
        window_days=limits.window_days,
        cycle_days=limits.cycle_days,
        window_events=stats["window_events"],
        cycle_events=stats["cycle_events"],
        window_qty=stats["window_qty"],
        cycle_qty=stats["cycle_qty"],
        observed_days=stats["observed_days"],
        reason=reason,
    )

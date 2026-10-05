"""备件子系统快照。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SpareSnapshot:
    """备件管理某一时刻的状态。"""

    parts: list
    stations: list
    events: int
    backfilled_events: int
    open_orders: int
    overdue_orders: int
    overview: list

    def as_dict(self) -> dict:
        return {
            "parts": [dict(item) for item in self.parts],
            "stations": list(self.stations),
            "events": self.events,
            "backfilled_events": self.backfilled_events,
            "open_orders": self.open_orders,
            "overdue_orders": self.overdue_orders,
            "overview": [dict(item) for item in self.overview],
        }

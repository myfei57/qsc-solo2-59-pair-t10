"""更换事件。

每条记录对应一次备件更换：``ts`` 是更换实际发生时间，``recorded_ts`` 是录入
系统的时间。两者相隔超过 ``lag_s`` 即判定为补录——补录事件参与消耗速率估计，
但不回改当前库存（库存只随实时更换与到货变动），这样补录历史之后建议可以
重算，账实却不会被悄悄改写。
"""

from __future__ import annotations

from dataclasses import dataclass

DAY_S = 86_400.0


@dataclass(frozen=True)
class ReplacementEvent:
    """一次备件更换记录。"""

    event_id: str
    part_id: str
    station: str
    ts: float
    qty: int
    recorded_ts: float
    backfilled: bool
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "part_id": self.part_id,
            "station": self.station,
            "ts": round(self.ts, 3),
            "qty": self.qty,
            "recorded_ts": round(self.recorded_ts, 3),
            "backfilled": self.backfilled,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "ReplacementEvent":
        return cls(
            event_id=str(payload["event_id"]),
            part_id=str(payload["part_id"]),
            station=str(payload["station"]),
            ts=float(payload["ts"]),
            qty=int(payload["qty"]),
            recorded_ts=float(payload["recorded_ts"]),
            backfilled=bool(payload["backfilled"]),
            note=str(payload.get("note", "")),
        )


def is_backfilled(ts: float, recorded_ts: float, lag_s: float = DAY_S) -> bool:
    """录入时间比发生时间晚超过容差（默认一天）就算补录。"""

    return float(recorded_ts) - float(ts) > float(lag_s)

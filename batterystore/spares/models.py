"""备件台账的数据结构。

备件目录、更换事件与采购单都是不可变记录：更换事件（含补录）只能追加不能
修改，采购单一经下发即冻结，到货只追加收货记录。台账内存状态由流水重放得到，
因此所有结构都带 ``as_dict`` / ``from_dict``，落盘与回放走同一份字段口径。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Part:
    """备件目录条目。"""

    part_id: str
    name: str
    unit: str
    basis: str = "auto"

    def as_dict(self) -> dict:
        return {"part_id": self.part_id, "name": self.name, "unit": self.unit, "basis": self.basis}

    @classmethod
    def from_dict(cls, raw: dict) -> "Part":
        return cls(
            part_id=str(raw["part_id"]),
            name=str(raw.get("name", raw["part_id"])),
            unit=str(raw.get("unit", "件")),
            basis=str(raw.get("basis", "auto")),
        )


@dataclass(frozen=True)
class ReplacementEvent:
    """一次备件更换（消耗）事件。

    ``ts`` 是更换实际发生的时间，``recorded_ts`` 是登记进系统的时间；两者相差
    超过宽限窗口即标记为补录。补录同样参与速率重算，但采购单不会被追溯改写。
    """

    event_id: str
    part_id: str
    station: str
    qty: float
    ts: float
    recorded_ts: float
    backfilled: bool
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "part_id": self.part_id,
            "station": self.station,
            "qty": self.qty,
            "ts": round(self.ts, 3),
            "recorded_ts": round(self.recorded_ts, 3),
            "backfilled": self.backfilled,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "ReplacementEvent":
        return cls(
            event_id=str(raw["event_id"]),
            part_id=str(raw["part_id"]),
            station=str(raw["station"]),
            qty=float(raw.get("qty", 1.0)),
            ts=float(raw["ts"]),
            recorded_ts=float(raw.get("recorded_ts", raw["ts"])),
            backfilled=bool(raw.get("backfilled", False)),
            note=str(raw.get("note", "")),
        )


@dataclass
class PurchaseOrder:
    """一张采购单。下发后数量与备件冻结，状态只随到货推进。"""

    order_id: str
    part_id: str
    station: str
    qty: float
    issued_ts: float
    lead_time_days: float
    expected_ts: float
    received: float = 0.0
    status: str = "open"
    receipts: list[dict] = field(default_factory=list)

    def remaining(self) -> float:
        """尚未到货的数量。"""

        return max(0.0, self.qty - self.received)

    def as_dict(self) -> dict:
        return {
            "order_id": self.order_id,
            "part_id": self.part_id,
            "station": self.station,
            "qty": self.qty,
            "issued_ts": round(self.issued_ts, 3),
            "lead_time_days": self.lead_time_days,
            "expected_ts": round(self.expected_ts, 3),
            "received": round(self.received, 6),
            "status": self.status,
            "receipts": [dict(item) for item in self.receipts],
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "PurchaseOrder":
        return cls(
            order_id=str(raw["order_id"]),
            part_id=str(raw["part_id"]),
            station=str(raw["station"]),
            qty=float(raw["qty"]),
            issued_ts=float(raw["issued_ts"]),
            lead_time_days=float(raw.get("lead_time_days", 0.0)),
            expected_ts=float(raw.get("expected_ts", raw["issued_ts"])),
            received=float(raw.get("received", 0.0)),
            status=str(raw.get("status", "open")),
            receipts=[dict(item) for item in raw.get("receipts", [])],
        )

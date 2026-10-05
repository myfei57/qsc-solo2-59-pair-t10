"""备件档案。

档案只放与单个备件绑定、不随时间变的补货参数：采购提前期（天下单到到货）、
起订量与订货批量。同一备件在所有站共用一份档案；各站库存与更换记录分开记。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PartProfile:
    """一个备件的补货参数。"""

    part_id: str
    name: str
    lead_days: float
    min_order_qty: int
    lot_size: int

    def as_dict(self) -> dict:
        return {
            "part_id": self.part_id,
            "name": self.name,
            "lead_days": round(self.lead_days, 3),
            "min_order_qty": self.min_order_qty,
            "lot_size": self.lot_size,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "PartProfile":
        return cls(
            part_id=str(payload["part_id"]),
            name=str(payload.get("name", payload["part_id"])),
            lead_days=float(payload["lead_days"]),
            min_order_qty=int(payload.get("min_order_qty", 1)),
            lot_size=int(payload.get("lot_size", 1)),
        )

    def round_lot(self, qty: int) -> int:
        """把建议采购量向上取整到订货批量，且不小于起订量。"""

        if qty <= 0:
            return 0
        lot = max(1, self.lot_size)
        rounded = ((int(qty) + lot - 1) // lot) * lot
        return max(rounded, max(1, self.min_order_qty))

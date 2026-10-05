"""采购单台账。

采购单一经下发即冻结：数量、供应商、预期到货时间都不允许再改。补录历史更换
记录后建议重算，若与已下发采购单出现偏差，只由差异提示暴露，台账本身一字不
改；要纠偏只能再下一张新单。到货按单登记，可分批，登记量不能超过下单量，
``status`` 随之在 ``open``（在途）、``partial``（部分到货）、``closed``
（齐套）之间流转，逾期未交完会标记 ``overdue``。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..errors import InventoryError
from .events import DAY_S

OPEN = "open"
PARTIAL = "partial"
CLOSED = "closed"
OVERDUE = "overdue"


@dataclass(frozen=True)
class PurchaseOrder:
    """一张已下发的采购单（不可变）。"""

    order_id: str
    part_id: str
    qty: int
    issued_ts: float
    expected_ts: float
    supplier: str
    received: int = 0
    receipts: list[dict] = field(default_factory=list)

    @property
    def in_transit(self) -> int:
        """尚未到货的数量。"""

        return max(0, self.qty - self.received)

    def status_at(self, now: float) -> str:
        """按到货进度与当前时间计算单据状态。"""

        if self.received >= self.qty:
            return CLOSED
        if now > self.expected_ts:
            return OVERDUE
        return PARTIAL if self.received > 0 else OPEN

    def as_dict(self, now: float) -> dict:
        return {
            "order_id": self.order_id,
            "part_id": self.part_id,
            "qty": self.qty,
            "issued_ts": round(self.issued_ts, 3),
            "expected_ts": round(self.expected_ts, 3),
            "supplier": self.supplier,
            "received": self.received,
            "in_transit": self.in_transit,
            "receipts": [dict(item) for item in self.receipts],
            "status": self.status_at(now),
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "PurchaseOrder":
        return cls(
            order_id=str(payload["order_id"]),
            part_id=str(payload["part_id"]),
            qty=int(payload["qty"]),
            issued_ts=float(payload["issued_ts"]),
            expected_ts=float(payload["expected_ts"]),
            supplier=str(payload.get("supplier", "")),
            received=int(payload.get("received", 0)),
            receipts=[dict(item) for item in payload.get("receipts", [])],
        )


class PurchaseLedger:
    """采购单台账：只追加新单与到货记录，从不改写旧单。"""

    def __init__(self, orders: list[PurchaseOrder] | None = None) -> None:
        self._orders: dict[str, PurchaseOrder] = {order.order_id: order for order in (orders or [])}

    def issue(
        self,
        order_id: str,
        part_id: str,
        qty: int,
        issued_ts: float,
        lead_days: float,
        supplier: str = "",
    ) -> PurchaseOrder:
        """下发一张新采购单，预期到货时间由提前期推出。"""

        if order_id in self._orders:
            raise InventoryError(f"采购单号重复: {order_id}", component="spare")
        if int(qty) <= 0:
            raise InventoryError("采购数量必须为正", component="spare")
        order = PurchaseOrder(
            order_id=order_id,
            part_id=part_id,
            qty=int(qty),
            issued_ts=float(issued_ts),
            expected_ts=float(issued_ts) + max(0.0, lead_days) * DAY_S,
            supplier=supplier,
        )
        self._orders[order_id] = order
        return order

    def receive(self, order_id: str, qty: int, ts: float, station: str, note: str = "") -> PurchaseOrder:
        """登记一次到货并冻结到单据上；到货量不能超过未交量。"""

        existing = self._orders.get(order_id)
        if existing is None:
            raise InventoryError(f"未知采购单: {order_id}", component="spare")
        if int(qty) <= 0:
            raise InventoryError("到货数量必须为正", component="spare")
        if existing.received + int(qty) > existing.qty:
            raise InventoryError(
                f"采购单 {order_id} 累计到货将超过下单量 {existing.qty}", component="spare"
            )
        receipt = {
            "ts": round(float(ts), 3),
            "qty": int(qty),
            "station": station,
            "note": note,
        }
        updated = PurchaseOrder(
            order_id=existing.order_id,
            part_id=existing.part_id,
            qty=existing.qty,
            issued_ts=existing.issued_ts,
            expected_ts=existing.expected_ts,
            supplier=existing.supplier,
            received=existing.received + int(qty),
            receipts=[*existing.receipts, receipt],
        )
        self._orders[order_id] = updated
        return updated

    def get(self, order_id: str) -> PurchaseOrder:
        """按单号取单。"""

        if order_id not in self._orders:
            raise InventoryError(f"未知采购单: {order_id}", component="spare")
        return self._orders[order_id]

    def for_part(self, part_id: str) -> list[PurchaseOrder]:
        """按备件返回全部采购单（按下发时间升序）。"""

        return sorted(
            (order for order in self._orders.values() if order.part_id == part_id),
            key=lambda order: order.issued_ts,
        )

    def in_transit_for(self, part_id: str, now: float) -> int:
        """某备件当前在途总量。"""

        return sum(order.in_transit for order in self.for_part(part_id) if order.status_at(now) != CLOSED)

    def all(self) -> list[PurchaseOrder]:
        """返回全部采购单（按下发时间升序）。"""

        return sorted(self._orders.values(), key=lambda order: order.issued_ts)

    def as_list(self) -> list[dict]:
        """序列化全部单据（不带状态，状态由读取方按当前时间算）。"""

        return [
            {
                "order_id": order.order_id,
                "part_id": order.part_id,
                "qty": order.qty,
                "issued_ts": order.issued_ts,
                "expected_ts": order.expected_ts,
                "supplier": order.supplier,
                "received": order.received,
                "receipts": [dict(item) for item in order.receipts],
            }
            for order in self.all()
        ]

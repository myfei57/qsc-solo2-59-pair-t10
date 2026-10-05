"""备件消耗与补货台账。

台账把四类事实接起来：备件目录、更换事件（含补录）、采购单与到货收货。状态
全部来自只追加的 JSONL 流水，重启后按流水重放；建议是每次即时计算的派生物，
不落盘，因此补录更换事件后建议自动按新速率重算，而已下发采购单是冻结记录，
只给出差异提示、绝不回写。同一备件在多站的消耗既可以按站分别估计，也可以
把各站事件合并后估计，两种视图共用同一套口径。
"""

from __future__ import annotations

import threading

from ..clock import Clock
from ..config import SparesLimits
from ..errors import SparesError
from ..ids import IdFactory
from ..store.journal import JsonlJournal
from .models import Part, PurchaseOrder, ReplacementEvent
from .order import compare_order
from .rate import BASES, DAY_SECONDS, estimate_rate
from .recommend import build_recommendation, expected_arrival_ts
from .stock import derive_stock

OP_PART = "part_register"
OP_REPLACE = "replacement"
OP_STOCK = "stock_initial"
OP_ORDER = "order_issue"
OP_RECEIVE = "order_receive"
OP_BASIS = "basis_override"

POOLED_STATION = "*"
COMPONENT = "spares"


class SparesLedger:
    """备件目录、消耗、库存与采购单的内存台账（带流水持久化）。"""

    def __init__(self, clock: Clock, limits: SparesLimits, journal: JsonlJournal, default_station: str) -> None:
        self._clock = clock
        self._limits = limits
        self._journal = journal
        self._default_station = default_station
        self._ids = IdFactory("spares")
        self._parts: dict[str, Part] = {}
        self._events: list[ReplacementEvent] = []
        self._orders: dict[str, PurchaseOrder] = {}
        self._initial_stock: dict[tuple[str, str], float] = {}
        self._seen_ops: set[str] = set()
        self._lock = threading.RLock()
        self._replay()

    @property
    def limits(self) -> SparesLimits:
        return self._limits

    def _append(self, op: dict) -> None:
        self._journal.append(op)
        self._apply(op)

    def _replay(self) -> None:
        for op in self._journal.read_all():
            if op.get("op_id") in self._seen_ops:
                continue
            self._apply(op)

    def _apply(self, op: dict) -> None:
        kind = op["kind"]
        op_id = str(op.get("op_id", ""))
        if op_id:
            self._seen_ops.add(op_id)
        if kind == OP_PART:
            part = Part.from_dict(op["part"])
            self._parts[part.part_id] = part
        elif kind == OP_REPLACE:
            event = ReplacementEvent.from_dict(op["event"])
            if event.event_id not in {item.event_id for item in self._events}:
                self._events.append(event)
        elif kind == OP_STOCK:
            self._initial_stock[(str(op["part_id"]), str(op["station"]))] = float(op["qty"])
        elif kind == OP_ORDER:
            order = PurchaseOrder.from_dict(op["order"])
            self._orders[order.order_id] = order
        elif kind == OP_RECEIVE:
            order = self._orders[str(op["order_id"])]
            receipt = {
                "ts": float(op["ts"]),
                "qty": float(op["qty"]),
                "station": str(op["station"]),
            }
            order.received += receipt["qty"]
            order.receipts.append(receipt)
            if order.received + 1e-9 >= order.qty:
                order.received = order.qty
                order.status = "closed"
        elif kind == OP_BASIS:
            part = self._parts[str(op["part_id"])]
            self._parts[part.part_id] = Part(part.part_id, part.name, part.unit, str(op["basis"]))

    def _require_part(self, part_id: str) -> Part:
        part = self._parts.get(part_id)
        if part is None:
            raise SparesError(f"未登记的备件: {part_id}", component=COMPONENT)
        return part

    def _station(self, station: str | None) -> str:
        return station or self._default_station

    def register_part(
        self, part_id: str, name: str | None = None, unit: str = "件", initial_stock: float = 0.0, station: str | None = None
    ) -> dict:
        """登记一个备件目录条目，可顺带录入某站初始库存。"""

        with self._lock:
            if not part_id:
                raise SparesError("备件编号不能为空", component=COMPONENT)
            if part_id in self._parts:
                raise SparesError(f"备件已登记: {part_id}", component=COMPONENT)
            target = self._station(station)
            op_id = self._ids.next()
            self._append(
                {
                    "op_id": op_id,
                    "kind": OP_PART,
                    "ts": self._clock.now(),
                    "part": Part(part_id, name or part_id, unit).as_dict(),
                }
            )
            if float(initial_stock) != 0.0:
                self._append(
                    {
                        "op_id": self._ids.next(),
                        "kind": OP_STOCK,
                        "ts": self._clock.now(),
                        "part_id": part_id,
                        "station": target,
                        "qty": float(initial_stock),
                    }
                )
            return {"part": self._parts[part_id].as_dict(), "initial_stock": float(initial_stock), "station": target}

    def set_initial_stock(self, part_id: str, qty: float, station: str | None = None) -> dict:
        """登记或调整某站某备件的期初库存（盘点基线）。"""

        with self._lock:
            self._require_part(part_id)
            target = self._station(station)
            self._append(
                {
                    "op_id": self._ids.next(),
                    "kind": OP_STOCK,
                    "ts": self._clock.now(),
                    "part_id": part_id,
                    "station": target,
                    "qty": float(qty),
                }
            )
            return {"part_id": part_id, "station": target, "initial_stock": float(qty)}

    def record_replacement(
        self,
        part_id: str,
        qty: float = 1.0,
        station: str | None = None,
        ts: float | None = None,
        backfilled: bool | None = None,
        note: str = "",
    ) -> dict:
        """登记一次更换消耗；事件时间早于登记时间超过宽限窗口即判为补录。"""

        with self._lock:
            self._require_part(part_id)
            if float(qty) <= 0.0:
                raise SparesError("更换数量必须为正数", component=COMPONENT)
            recorded_ts = self._clock.now()
            event_ts = float(ts) if ts is not None else recorded_ts
            if event_ts > recorded_ts:
                raise SparesError("更换时间不能晚于登记时间", component=COMPONENT)
            grace = self._limits.backfill_grace_days * DAY_SECONDS
            is_backfill = (recorded_ts - event_ts > grace) if backfilled is None else bool(backfilled)
            event = ReplacementEvent(
                event_id=self._ids.next(),
                part_id=part_id,
                station=self._station(station),
                qty=float(qty),
                ts=event_ts,
                recorded_ts=recorded_ts,
                backfilled=is_backfill,
                note=note,
            )
            self._append(
                {
                    "op_id": self._ids.next(),
                    "kind": OP_REPLACE,
                    "ts": recorded_ts,
                    "event": event.as_dict(),
                }
            )
            stock = self.stock(part_id, event.station)
            return {
                "event": event.as_dict(),
                "backfilled": is_backfill,
                "stock": stock,
                "recomputed": True,
            }

    def set_basis(self, part_id: str, basis: str) -> dict:
        """把某备件的估计口径钉成 window/cycle，或恢复 auto。"""

        with self._lock:
            self._require_part(part_id)
            if basis not in BASES:
                raise SparesError(f"口径必须是 {BASES} 之一", component=COMPONENT)
            self._append(
                {
                    "op_id": self._ids.next(),
                    "kind": OP_BASIS,
                    "ts": self._clock.now(),
                    "part_id": part_id,
                    "basis": basis,
                }
            )
            return {"part_id": part_id, "basis": basis}

    def _scope_events(self, part_id: str, station: str) -> tuple[str, list[ReplacementEvent]]:
        if station == POOLED_STATION:
            return POOLED_STATION, [event for event in self._events if event.part_id == part_id]
        return station, [
            event for event in self._events if event.part_id == part_id and event.station == station
        ]

    def _stock_for(self, part_id: str, station: str, now: float):
        _, events = self._scope_events(part_id, station)
        movements = [(event.ts, -event.qty, "consume") for event in events]
        if station == POOLED_STATION:
            initial = sum(
                value for (pid, sid), value in self._initial_stock.items() if pid == part_id
            )
            movements.extend(
                (receipt["ts"], receipt["qty"], "receive")
                for order in self._orders.values()
                if order.part_id == part_id
                for receipt in order.receipts
            )
        else:
            initial = self._initial_stock.get((part_id, station), 0.0)
        movements.extend(
            (receipt["ts"], receipt["qty"], "receive")
            for order in self._orders.values()
            if order.part_id == part_id
            for receipt in order.receipts
            if receipt["station"] == station
        )
        return derive_stock(movements, initial, now)

    def stock(self, part_id: str, station: str | None = None) -> dict:
        """返回某备件在某站（或合并）的当前库存。"""

        with self._lock:
            part = self._require_part(part_id)
            target = self._station(station)
            state = self._stock_for(part_id, target, self._clock.now())
            return {"part_id": part_id, "scope": target, "unit": part.unit, **state.as_dict()}

    def _open_incoming(self, part_id: str, station: str) -> float:
        total = 0.0
        for order in self._orders.values():
            if order.part_id != part_id or order.status != "open":
                continue
            if station == POOLED_STATION or order.station == station or order.station == POOLED_STATION:
                total += order.remaining()
        return total

    def rate_estimate(self, part_id: str, station: str | None = None):
        """按当前全部记录即时估计某备件在某站（或合并）的消耗速率。"""

        with self._lock:
            part = self._require_part(part_id)
            target = self._station(station)
            scope, events = self._scope_events(part_id, target)
            return estimate_rate(
                part_id,
                events,
                self._clock.now(),
                self._limits,
                scope=scope,
                override=part.basis,
            )

    def recommend(self, part_id: str, station: str | None = None, lead_time_days: float | None = None) -> dict:
        """即时计算最低库存与补货建议（补录后调用即为重算结果）。"""

        with self._lock:
            part = self._require_part(part_id)
            target = self._station(station)
            estimate = self.rate_estimate(part_id, target)
            stock_state = self._stock_for(part_id, target, self._clock.now())
            incoming = self._open_incoming(part_id, target)
            return build_recommendation(
                estimate,
                stock_state.on_hand,
                incoming,
                self._limits,
                part_id=part_id,
                scope=target,
                unit=part.unit,
                lead_time_days=lead_time_days,
            )

    def issue_order(
        self,
        part_id: str,
        qty: float | None = None,
        station: str | None = None,
        lead_time_days: float | None = None,
    ) -> dict:
        """按当前建议下发采购单并冻结；数量缺省取即时建议采购量。"""

        with self._lock:
            part = self._require_part(part_id)
            target = self._station(station)
            suggestion = self.recommend(part_id, target, lead_time_days)
            quantity = float(qty) if qty is not None else float(suggestion["suggest_qty"])
            if quantity <= 0.0:
                raise SparesError("采购数量必须为正数，当前无需补货", component=COMPONENT)
            lead_days = suggestion["lead_time_days"] if lead_time_days is None else float(lead_time_days)
            issued_ts = self._clock.now()
            order = PurchaseOrder(
                order_id=self._ids.next(),
                part_id=part_id,
                station=target,
                qty=quantity,
                issued_ts=issued_ts,
                lead_time_days=float(lead_days),
                expected_ts=expected_arrival_ts(issued_ts, float(lead_days)),
            )
            self._append(
                {
                    "op_id": self._ids.next(),
                    "kind": OP_ORDER,
                    "ts": issued_ts,
                    "order": order.as_dict(),
                }
            )
            return {
                "order": self._orders[order.order_id].as_dict(),
                "unit": part.unit,
                "basis_at_issue": suggestion["basis"],
                "rate_per_day_at_issue": suggestion["rate_per_day"],
                "suggest_qty_at_issue": float(suggestion["suggest_qty"]),
                "frozen": True,
            }

    def receive_order(self, order_id: str, qty: float | None = None, station: str | None = None) -> dict:
        """登记一笔到货收货：先补缺件挂账，剩余回补库存，超出订单数量拒绝。"""

        with self._lock:
            order = self._orders.get(order_id)
            if order is None:
                raise SparesError(f"未知采购单: {order_id}", component=COMPONENT)
            if order.status != "open":
                raise SparesError(f"采购单已关闭: {order_id}", component=COMPONENT)
            remaining = order.remaining()
            received_qty = remaining if qty is None else float(qty)
            if received_qty <= 0.0:
                raise SparesError("到货数量必须为正数", component=COMPONENT)
            if received_qty > remaining + 1e-9:
                raise SparesError(
                    f"到货数量 {received_qty:g} 超过未交数量 {remaining:g}", component=COMPONENT
                )
            target = self._station(station) if order.station == POOLED_STATION else order.station
            ts = self._clock.now()
            self._append(
                {
                    "op_id": self._ids.next(),
                    "kind": OP_RECEIVE,
                    "ts": ts,
                    "order_id": order_id,
                    "qty": received_qty,
                    "station": target,
                }
            )
            updated = self._orders[order_id]
            stock = self.stock(order.part_id, order.station)
            return {
                "order": updated.as_dict(),
                "received_now": received_qty,
                "station": target,
                "stock": stock,
            }

    def order_diff(self, order_id: str) -> dict:
        """用即时重算的建议与一张已下发采购单对照，只给差异提示，不改单。

        比较时先把该单未交数量从在途头寸里剔除，还原"假设这张单不存在"的
        建议采购量，再与该单未交数量对比；否则拿已含该单的增量去比该单总量，
        补录抬高需求时会把"需要追单"误报成"可能积压"。
        """

        with self._lock:
            order = self._orders.get(order_id)
            if order is None:
                raise SparesError(f"未知采购单: {order_id}", component=COMPONENT)
            part = self._require_part(order.part_id)
            estimate = self.rate_estimate(order.part_id, order.station)
            stock_state = self._stock_for(order.part_id, order.station, self._clock.now())
            incoming_all = self._open_incoming(order.part_id, order.station)
            incoming_excluding_order = max(0.0, incoming_all - order.remaining())
            suggestion_excl = build_recommendation(
                estimate,
                stock_state.on_hand,
                incoming_excluding_order,
                self._limits,
                part_id=order.part_id,
                scope=order.station,
                unit=part.unit,
            )
            suggestion = build_recommendation(
                estimate,
                stock_state.on_hand,
                incoming_all,
                self._limits,
                part_id=order.part_id,
                scope=order.station,
                unit=part.unit,
            )
            diff = compare_order(
                order.as_dict(),
                float(suggestion_excl["suggest_qty"]),
                float(suggestion["rate_per_day"]),
                self._clock.now(),
            )
            diff["suggest_qty_excluding_order"] = float(suggestion_excl["suggest_qty"])
            diff["additional_suggest_qty"] = float(suggestion["suggest_qty"])
            diff["recommendation"] = suggestion
            return diff

    def list_orders(self, part_id: str | None = None, include_closed: bool = True) -> list[dict]:
        """列出采购单，可按备件过滤，附带是否逾期。"""

        with self._lock:
            now = self._clock.now()
            orders = []
            for order in self._orders.values():
                if part_id is not None and order.part_id != part_id:
                    continue
                if not include_closed and order.status != "open":
                    continue
                item = order.as_dict()
                item["remaining"] = round(order.remaining(), 6)
                item["overdue"] = order.status == "open" and now > order.expected_ts
                orders.append(item)
            return sorted(orders, key=lambda item: item["issued_ts"])

    def arrivals(self) -> dict:
        """跟到货情况：在途、逾期、本期已到数量。"""

        with self._lock:
            now = self._clock.now()
            open_orders = [order for order in self._orders.values() if order.status == "open"]
            overdue = [order for order in open_orders if now > order.expected_ts]
            closed = [order for order in self._orders.values() if order.status == "closed"]
            return {
                "open_orders": len(open_orders),
                "open_qty": round(sum(order.remaining() for order in open_orders), 6),
                "overdue_orders": [order.order_id for order in sorted(overdue, key=lambda o: o.expected_ts)],
                "overdue_qty": round(sum(order.remaining() for order in overdue), 6),
                "closed_orders": len(closed),
                "received_total": round(sum(order.received for order in self._orders.values()), 6),
            }

    def part_overview(self, part_id: str) -> dict:
        """同一备件既给各站分开视图，也给合并视图。"""

        with self._lock:
            part = self._require_part(part_id)
            stations = sorted({event.station for event in self._events if event.part_id == part_id})
            stations.extend(
                sid
                for pid, sid in self._initial_stock
                if pid == part_id and sid not in stations
            )
            stations = sorted(set(stations) | {self._default_station})
            per_station = [self.recommend(part_id, station) for station in stations]
            pooled = self.recommend(part_id, POOLED_STATION)
            return {
                "part": part.as_dict(),
                "stations": stations,
                "per_station": per_station,
                "pooled": pooled,
            }

    def overview(self) -> dict:
        """全部备件的合并视图建议清单。"""

        with self._lock:
            return {
                "parts": [self.part_overview(part_id) for part_id in sorted(self._parts)],
                "arrivals": self.arrivals(),
            }

    def events(self, part_id: str | None = None, backfilled_only: bool = False) -> list[dict]:
        """查询更换事件，可只看补录。"""

        with self._lock:
            selected = [
                event.as_dict()
                for event in self._events
                if (part_id is None or event.part_id == part_id)
                and (not backfilled_only or event.backfilled)
            ]
            return sorted(selected, key=lambda item: item["ts"])

    def snapshot(self) -> dict:
        """返回备件台账完整快照。"""

        with self._lock:
            return {
                "limits": {
                    "window_days": self._limits.window_days,
                    "cycle_days": self._limits.cycle_days,
                    "lead_time_days": self._limits.lead_time_days,
                    "target_coverage_days": self._limits.target_coverage_days,
                    "min_events": self._limits.min_events,
                    "season_ratio": self._limits.season_ratio,
                    "backfill_grace_days": self._limits.backfill_grace_days,
                },
                "parts": [part.as_dict() for part_id, part in sorted(self._parts.items())],
                "events": [event.as_dict() for event in sorted(self._events, key=lambda e: e.ts)],
                "orders": self.list_orders(),
                "arrivals": self.arrivals(),
                "overview": self.overview(),
                "journal_count": self._journal.count(),
            }

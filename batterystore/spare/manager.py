"""备件管理门面。

:class:`SparePartsManager` 把更换记录、速率估计、库存策略与采购单台账串成一条
链，并负责落盘与审计事件。口径选择、最低库存与建议量都委托给
:mod:`batterystore.spare.rate` 与 :mod:`batterystore.spare.stock` 的纯函数，
台账的不可变约束由 :mod:`batterystore.spare.order` 保证。

库存按 (备件, 站) 分开记，同一备件可按站出建议，也可跨站合并出建议；采购单
按备件统一跟踪，到货指定入站站。实时更换会扣库存并在缺件时记欠储，补录更换
只进速率统计、不动库存。
"""

from __future__ import annotations

import threading
from typing import Callable

from ..clock import Clock
from ..config import SpareLimits
from ..errors import InventoryError
from ..ids import IdFactory
from ..store.jsonfile import JsonStore
from .events import DAY_S, ReplacementEvent, is_backfilled
from .order import CLOSED, PurchaseLedger, PurchaseOrder
from .parts import PartProfile
from .rate import CYCLE, WINDOW, estimate_rate
from .snapshot import SpareSnapshot
from .stock import evaluate_policy

CENTRAL = "central"
EVENTS_DOC = "spare_events"
STOCK_DOC = "spare_stock"
PARTS_DOC = "spare_parts"
ORDERS_DOC = "spare_orders"


class SparePartsManager:
    """备件消耗、库存与采购单台账。"""

    def __init__(
        self,
        limits: SpareLimits,
        clock: Clock,
        store: JsonStore,
        publish: Callable | None = None,
        backfill_lag_s: float = DAY_S,
    ) -> None:
        self._limits = limits
        self._clock = clock
        self._store = store
        self._publish_fn = publish
        self._lag_s = float(backfill_lag_s)
        self._parts: dict[str, PartProfile] = {}
        self._events: list[ReplacementEvent] = []
        self._stock: dict[str, dict[str, int]] = {}
        self._backlogs: dict[str, dict[str, int]] = {}
        self._ledger = PurchaseLedger()
        self._event_ids = IdFactory("evt")
        self._order_ids = IdFactory("po")
        self._lock = threading.RLock()
        self._restore()

    def _restore(self) -> None:
        raw_parts = self._store.read(PARTS_DOC)
        if isinstance(raw_parts, list):
            for item in raw_parts:
                profile = PartProfile.from_dict(item)
                self._parts[profile.part_id] = profile
        raw_events = self._store.read(EVENTS_DOC)
        if isinstance(raw_events, list):
            self._events = [ReplacementEvent.from_dict(item) for item in raw_events]
        raw_stock = self._store.read(STOCK_DOC)
        if isinstance(raw_stock, dict) and "on_hand" in raw_stock:
            on_hand = raw_stock.get("on_hand")
            backlog = raw_stock.get("backlog")
            if isinstance(on_hand, dict):
                for part_id, stations in on_hand.items():
                    if isinstance(stations, dict):
                        self._stock[str(part_id)] = {str(key): int(value) for key, value in stations.items()}
            if isinstance(backlog, dict):
                for part_id, stations in backlog.items():
                    if isinstance(stations, dict):
                        self._backlogs[str(part_id)] = {str(key): int(value) for key, value in stations.items()}
        elif isinstance(raw_stock, dict):
            # 兼容只存在手库存的旧格式
            for part_id, stations in raw_stock.items():
                if isinstance(stations, dict):
                    self._stock[part_id] = {str(key): int(value) for key, value in stations.items()}
        raw_orders = self._store.read(ORDERS_DOC)
        if isinstance(raw_orders, list):
            self._ledger = PurchaseLedger([PurchaseOrder.from_dict(item) for item in raw_orders])

    def _save_parts(self) -> None:
        self._store.write(PARTS_DOC, [profile.as_dict() for profile in self._parts.values()])

    def _save_events(self) -> None:
        self._store.write(EVENTS_DOC, [event.as_dict() for event in self._events])

    def _save_stock(self) -> None:
        self._store.write(
            STOCK_DOC,
            {
                "on_hand": {part_id: dict(stations) for part_id, stations in self._stock.items()},
                "backlog": {part_id: dict(stations) for part_id, stations in self._backlogs.items()},
            },
        )

    def _save_orders(self) -> None:
        self._store.write(ORDERS_DOC, self._ledger.as_list())

    def _publish(self, action: str, severity: str = "notice", **payload: object) -> None:
        if self._publish_fn is not None:
            self._publish_fn("spare", "spare", action, severity=severity, **payload)

    def _require_part(self, part_id: str) -> PartProfile:
        profile = self._parts.get(part_id)
        if profile is None:
            raise InventoryError(f"未知备件: {part_id}", component="spare")
        return profile

    def _events_of(self, part_id: str, station: str | None) -> list[ReplacementEvent]:
        return [
            event
            for event in self._events
            if event.part_id == part_id and (station is None or event.station == station)
        ]

    def _on_hand(self, part_id: str, station: str | None) -> int:
        stations = self._stock.get(part_id, {})
        if station is not None:
            return stations.get(station, 0)
        return sum(stations.values())

    def _backlog(self, part_id: str, station: str | None) -> int:
        stations = self._backlogs.get(part_id, {})
        if station is not None:
            return stations.get(station, 0)
        return sum(stations.values())

    def _stations_of(self, part_id: str) -> list[str]:
        stations = set(self._stock.get(part_id, {}))
        stations.update(self._backlogs.get(part_id, {}))
        stations.update(event.station for event in self._events if event.part_id == part_id)
        return sorted(stations)

    def register_part(
        self,
        part_id: str,
        name: str | None = None,
        lead_days: float | None = None,
        min_order_qty: int = 1,
        lot_size: int = 1,
        initial_stock: int = 0,
        station: str = CENTRAL,
    ) -> dict:
        """登记一个备件并可顺便设置某站初始库存；已登记的备件只补库存。"""

        with self._lock:
            now = self._clock.now()
            if not part_id:
                raise InventoryError("备件编号不能为空", component="spare")
            profile = self._parts.get(part_id)
            if profile is None:
                lead = float(self._limits.window_days) if lead_days is None else float(lead_days)
                if lead < 0:
                    raise InventoryError("采购提前期不能为负", component="spare")
                if min_order_qty < 1 or lot_size < 1:
                    raise InventoryError("起订量与订货批量必须为正", component="spare")
                profile = PartProfile(part_id, name or part_id, lead, int(min_order_qty), int(lot_size))
                self._parts[part_id] = profile
                self._save_parts()
            if initial_stock:
                self._stock.setdefault(part_id, {})
                self._stock[part_id][station] = self._stock[part_id].get(station, 0) + int(initial_stock)
                self._save_stock()
            self._publish("part_registered", part_id=part_id, station=station)
            return {"part": profile.as_dict(), "on_hand": self._on_hand(part_id, None), "now": now}

    def set_stock(self, part_id: str, qty: int, station: str = CENTRAL, note: str = "") -> dict:
        """盘点或初始化时直接设定某站在手库存（实时更换不经过这里）。"""

        with self._lock:
            self._require_part(part_id)
            if int(qty) < 0:
                raise InventoryError("库存不能为负", component="spare")
            previous = self._stock.get(part_id, {}).get(station, 0)
            self._stock.setdefault(part_id, {})[station] = int(qty)
            self._save_stock()
            self._publish(
                "stock_adjusted",
                part_id=part_id,
                station=station,
                previous=previous,
                qty=int(qty),
            )
            return {"part_id": part_id, "station": station, "previous": previous, "on_hand": int(qty), "note": note}

    def record_replacement(
        self,
        part_id: str,
        qty: int = 1,
        station: str = CENTRAL,
        ts: float | None = None,
        recorded_ts: float | None = None,
        note: str = "",
    ) -> dict:
        """登记一次备件更换。

        ``ts`` 是实际更换时间，缺省取当前时间；``recorded_ts`` 是录入时间，
        缺省也取当前时间。两者相差超过容差即按补录处理：补录只进速率统计，
        不扣当前库存；实时更换才扣库存，库存不足时记欠储，建议里会带缺货提示。
        """

        with self._lock:
            self._require_part(part_id)
            if int(qty) <= 0:
                raise InventoryError("更换数量必须为正", component="spare")
            happened = float(self._clock.now() if ts is None else ts)
            recorded = float(self._clock.now() if recorded_ts is None else recorded_ts)
            if recorded < happened - DAY_S:
                raise InventoryError("录入时间不能早于更换发生时间（容差一天）", component="spare")
            backfilled = is_backfilled(happened, recorded, self._lag_s)
            event = ReplacementEvent(
                event_id=self._event_ids.next(),
                part_id=part_id,
                station=station,
                ts=happened,
                qty=int(qty),
                recorded_ts=recorded,
                backfilled=backfilled,
                note=note,
            )
            self._events.append(event)
            self._save_events()
            on_hand_before = self._stock.get(part_id, {}).get(station, 0)
            backlog_before = self._backlogs.get(part_id, {}).get(station, 0)
            served = 0
            shortage = 0
            if not backfilled:
                served = min(int(qty), on_hand_before)
                shortage = int(qty) - served
                self._stock.setdefault(part_id, {})[station] = on_hand_before - served
                if shortage:
                    self._backlogs.setdefault(part_id, {})
                    self._backlogs[part_id][station] = backlog_before + shortage
                self._save_stock()
            self._publish(
                "replacement_recorded",
                severity="warning" if shortage else "notice",
                part_id=part_id,
                station=station,
                qty=int(qty),
                backfilled=backfilled,
                shortage=shortage,
            )
            return {
                "event": event.as_dict(),
                "served": served,
                "shortage": shortage,
                "on_hand": self._on_hand(part_id, station),
                "backlog": self._backlog(part_id, station),
            }

    def list_parts(self) -> list[dict]:
        """列出全部备件档案。"""

        with self._lock:
            return [profile.as_dict() for profile in self._parts.values()]

    def list_events(self, part_id: str | None = None, station: str | None = None, limit: int = 200) -> list[dict]:
        """按备件/站过滤更换记录，最新的在前。"""

        with self._lock:
            selected = [
                event
                for event in self._events
                if (part_id is None or event.part_id == part_id)
                and (station is None or event.station == station)
            ]
            return [event.as_dict() for event in reversed(selected[-max(1, int(limit)):])]

    def stock_view(self, part_id: str | None = None) -> dict:
        """返回各备件各站的在手与欠储库存。"""

        with self._lock:
            part_ids = [part_id] if part_id is not None else sorted(self._parts)
            view: dict[str, dict] = {}
            for pid in part_ids:
                self._require_part(pid)
                view[pid] = {
                    "stations": {
                        station: {
                            "on_hand": self._stock.get(pid, {}).get(station, 0),
                            "backlog": self._backlogs.get(pid, {}).get(station, 0),
                        }
                        for station in self._stations_of(pid)
                    },
                    "on_hand": self._on_hand(pid, None),
                    "backlog": self._backlog(pid, None),
                }
            return view

    def plan(
        self,
        part_id: str,
        station: str | None = None,
        basis: str | None = None,
        now: float | None = None,
    ) -> dict:
        """计算某备件在某站（或跨站合并）的速率、最低库存与补货建议。"""

        with self._lock:
            profile = self._require_part(part_id)
            moment = float(self._clock.now() if now is None else now)
            if basis not in (None, WINDOW, CYCLE):
                raise InventoryError(f"未知估计口径: {basis}", component="spare")
            events = self._events_of(part_id, station)
            estimate = estimate_rate(events, moment, self._limits, basis)
            in_transit = self._ledger.in_transit_for(part_id, moment)
            policy = evaluate_policy(
                profile,
                estimate,
                events,
                now=moment,
                on_hand=self._on_hand(part_id, station),
                backlog=self._backlog(part_id, station),
                in_transit=in_transit,
                service_level=self._limits.service_level,
                review_days=self._limits.review_days,
            )
            return {
                "part_id": part_id,
                "station": station,
                "stations": self._stations_of(part_id),
                "rate": estimate.as_dict(),
                "policy": policy.as_dict(),
                "events": len(events),
            }

    def overview(self, basis: str | None = None) -> list[dict]:
        """跨站合并的全备件补货总览（合并口径，用于下单决策）。"""

        with self._lock:
            return [self.plan(part_id, station=None, basis=basis) for part_id in sorted(self._parts)]

    def issue_order(
        self,
        part_id: str,
        qty: int | None = None,
        supplier: str = "",
        station: str = CENTRAL,
        now: float | None = None,
        note: str = "",
    ) -> dict:
        """下发采购单；数量缺省按当前合并建议量，单据下发后冻结。"""

        with self._lock:
            profile = self._require_part(part_id)
            moment = float(self._clock.now() if now is None else now)
            suggested = self.plan(part_id, station=None, now=moment)["policy"]["suggested_qty"]
            amount = suggested if qty is None else int(qty)
            if amount <= 0:
                raise InventoryError("采购数量必须为正，当前无需补货", component="spare")
            order = self._ledger.issue(
                self._order_ids.next(), part_id, amount, moment, profile.lead_days, supplier
            )
            self._save_orders()
            self._publish(
                "purchase_issued",
                part_id=part_id,
                order_id=order.order_id,
                qty=order.qty,
                suggested=suggested,
                station=station,
            )
            return {"order": order.as_dict(moment), "suggested_qty": suggested, "note": note}

    def receive_order(self, order_id: str, qty: int, station: str = CENTRAL, ts: float | None = None, note: str = "") -> dict:
        """登记采购单到货：先补该站欠储，余下入在手库存。"""

        with self._lock:
            moment = float(self._clock.now() if ts is None else ts)
            order = self._ledger.get(order_id)
            updated = self._ledger.receive(order_id, qty, moment, station, note)
            part_id = order.part_id
            backlog = self._backlogs.get(part_id, {}).get(station, 0)
            covered = min(int(qty), backlog)
            remaining = int(qty) - covered
            if covered:
                self._backlogs[part_id][station] = backlog - covered
            self._stock.setdefault(part_id, {})
            self._stock[part_id][station] = self._stock[part_id].get(station, 0) + remaining
            self._save_orders()
            self._save_stock()
            self._publish(
                "purchase_received",
                part_id=part_id,
                order_id=order_id,
                station=station,
                qty=int(qty),
                covered_backlog=covered,
            )
            return {
                "order": updated.as_dict(moment),
                "station": station,
                "covered_backlog": covered,
                "on_hand": self._on_hand(part_id, station),
                "backlog": self._backlog(part_id, station),
            }

    def list_orders(self, part_id: str | None = None, now: float | None = None) -> list[dict]:
        """列出采购单及当前状态（可按备件过滤）。"""

        with self._lock:
            moment = float(self._clock.now() if now is None else now)
            orders = self._ledger.all() if part_id is None else self._ledger.for_part(part_id)
            return [order.as_dict(moment) for order in orders]

    def order_diff(self, order_id: str, now: float | None = None) -> dict:
        """对比已下发采购单与当前重算建议，只报差异、不改单据。"""

        with self._lock:
            moment = float(self._clock.now() if now is None else now)
            order = self._ledger.get(order_id)
            plan = self.plan(order.part_id, station=None, now=moment)
            current = plan["policy"]["suggested_qty"]
            delta = current - order.qty
            if order.status_at(moment) == CLOSED:
                hint = "closed"
                message = "单据已齐套，差异仅供复盘，不再追加采购"
            elif delta > 0:
                hint = "short"
                message = f"重算建议比已下发数量多 {delta} 件，可另下新单补齐"
            elif delta < 0:
                hint = "surplus"
                message = f"重算建议比已下发数量少 {-delta} 件，原单不可改，关注到货后积压"
            else:
                hint = "match"
                message = "重算建议与已下发数量一致"
            return {
                "order_id": order.order_id,
                "part_id": order.part_id,
                "issued_qty": order.qty,
                "received": order.received,
                "in_transit": order.in_transit,
                "order_status": order.status_at(moment),
                "current_suggested_qty": current,
                "delta": delta,
                "hint": hint,
                "message": message,
                "basis": plan["rate"]["basis"],
                "rate_per_day": plan["rate"]["rate_per_day"],
                "min_stock": plan["policy"]["min_stock"],
            }

    def snapshot(self) -> SpareSnapshot:
        """返回备件子系统完整快照。"""

        with self._lock:
            now = self._clock.now()
            orders = self._ledger.all()
            stations = sorted({station for part_id in self._parts for station in self._stations_of(part_id)})
            backfilled = sum(1 for event in self._events if event.backfilled)
            overdue = sum(1 for order in orders if order.status_at(now) == "overdue")
            open_orders = sum(1 for order in orders if order.status_at(now) != CLOSED)
            return SpareSnapshot(
                parts=self.list_parts(),
                stations=stations,
                events=len(self._events),
                backfilled_events=backfilled,
                open_orders=open_orders,
                overdue_orders=overdue,
                overview=self.overview(),
            )

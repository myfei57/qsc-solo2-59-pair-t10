"""备件消耗估计、最低库存、补货建议与到货跟踪。

用例覆盖五条业务主线：

1. 两种速率口径（近窗口 / 全周期）与检修季的自动切换、切回；
2. 最低库存与补货建议随速率、库存、在途量变化；
3. 补录更换事件后建议重算，但已下发采购单冻结，只给差异提示；
4. 同一备件各站分开看与合并看；
5. 台账跨重启按流水重放。
"""

from __future__ import annotations

import pytest

from batterystore.errors import SparesError
from batterystore.spares.rate import DAY_SECONDS

PART = "fan-001"
STATION_A = "station1"
STATION_B = "station2"


def _seed_overhaul_history(platform, clock, part_id=PART, station=None, qty=10.0, days_ago=40.0):
    """在 ``days_ago`` 天前制造一次集中检修更换，平时只零散换 3 件。

    这些历史记录按数据迁移处理，显式声明非补录；业务规则里的补录另测。
    """

    platform.spares_register_part(part_id, name="散热风机", initial_stock=0.0, station=station)
    now = clock.now()
    for days in (200.0, 150.0, 100.0):
        platform.spares_record_replacement(
            part_id, qty=1.0, station=station, ts=now - days * DAY_SECONDS, backfilled=False
        )
    platform.spares_record_replacement(
        part_id, qty=qty, station=station, ts=now - days_ago * DAY_SECONDS, backfilled=False
    )


def test_two_rate_bases_differ_during_overhaul_month(platform, clock):
    _seed_overhaul_history(platform, clock)
    rate = platform.spares_rate(PART)
    assert rate["window_rate_per_day"] > rate["cycle_rate_per_day"]
    # 近窗口正处在集中检修月份：按窗口速率备货。
    assert rate["season_active"] is True
    assert rate["selected_basis"] == "window"
    assert rate["season_month"] is not None
    assert any("检修季" in reason for reason in rate["reasons"])


def test_basis_switches_back_to_cycle_after_overhaul_slides_out(platform, clock):
    _seed_overhaul_history(platform, clock, days_ago=40.0)
    assert platform.spares_rate(PART)["selected_basis"] == "window"
    # 高峰月份滑出 90 天窗口后，即使周期内仍有那个高峰，也切回周期均值。
    clock.advance(55 * DAY_SECONDS)
    rate = platform.spares_rate(PART)
    assert rate["selected_basis"] == "cycle"
    assert rate["season_active"] is False
    assert any("切回周期均值" in reason for reason in rate["reasons"])


def test_manual_basis_override_is_respected(platform, clock):
    _seed_overhaul_history(platform, clock)
    assert platform.spares_rate(PART)["selected_basis"] == "window"
    platform.spares_set_basis(PART, "cycle")
    rate = platform.spares_rate(PART)
    assert rate["selected_basis"] == "cycle"
    assert any("强制口径" in reason for reason in rate["reasons"])
    platform.spares_set_basis(PART, "window")
    assert platform.spares_rate(PART)["selected_basis"] == "window"
    platform.spares_set_basis(PART, "auto")
    assert platform.spares_rate(PART)["selected_basis"] == "window"


def test_recommendation_gives_min_stock_and_suggest_qty(platform, clock):
    _seed_overhaul_history(platform, clock)
    rec = platform.spares_recommend(PART)
    assert rec["basis"] == "window"
    assert rec["min_stock"] > 0.0
    assert rec["target_stock"] >= rec["min_stock"]
    # 期初无库存且已欠 13 件：建议量要补到目标库存并填平缺件挂账。
    assert rec["urgency"] == "stockout"
    assert rec["suggest_qty"] == pytest.approx(rec["target_stock"] - rec["position"], abs=1.0)
    assert rec["suggest_qty"] >= rec["target_stock"]
    platform.spares_set_stock(PART, 100.0)
    healthy = platform.spares_recommend(PART)
    assert healthy["urgency"] == "ok"
    assert healthy["suggest_qty"] == 0.0


def test_open_incoming_offsets_suggested_qty(platform, clock):
    _seed_overhaul_history(platform, clock)
    before = platform.spares_recommend(PART)
    issued = platform.spares_issue_order(PART, qty=5.0)
    order_id = issued["order"]["order_id"]
    assert issued["frozen"] is True
    after = platform.spares_recommend(PART)
    assert after["open_incoming"] == pytest.approx(5.0)
    assert after["suggest_qty"] == pytest.approx(max(0.0, before["suggest_qty"] - 5.0))
    assert platform.spares_orders(PART)["orders"][0]["order_id"] == order_id


def test_backfill_recomputes_advice_but_never_rewrites_issued_order(platform, clock):
    _seed_overhaul_history(platform, clock)
    issued = platform.spares_issue_order(PART, qty=6.0)
    order_id = issued["order"]["order_id"]
    frozen_qty = issued["order"]["qty"]
    frozen_rate = issued["rate_per_day_at_issue"]

    # 补录一笔 20 天前的大批量更换（超过 7 天宽限，自动判为补录）。
    backfilled = platform.spares_record_replacement(
        PART, qty=8.0, ts=clock.now() - 20.0 * DAY_SECONDS
    )
    assert backfilled["backfilled"] is True
    assert backfilled["recomputed"] is True
    events = platform.spares_events(PART, backfilled_only=True)["events"]
    assert len(events) == 1 and events[0]["qty"] == 8.0

    # 采购单原封不动：数量、下发时速率都不被重算改写。
    orders = {item["order_id"]: item for item in platform.spares_orders(PART)["orders"]}
    assert orders[order_id]["qty"] == frozen_qty
    assert orders[order_id]["status"] == "open"

    # 差异只以提示形式给出：该单已不够覆盖新需求，提示追单，但单子不改。
    diff = platform.spares_order_diff(order_id)
    assert diff["frozen"] is True
    assert diff["ordered_qty"] == frozen_qty
    assert diff["diff"] == "short"
    assert diff["delta_qty"] > 0.0
    assert diff["suggest_qty_excluding_order"] >= frozen_qty
    assert diff["additional_suggest_qty"] >= 0.0
    assert "追加采购" in diff["message"]
    assert diff["current_rate_per_day"] >= frozen_rate


def test_diff_warns_about_excess_after_overhaul_season_ends(platform, clock):
    _seed_overhaul_history(platform, clock, qty=12.0)
    issued = platform.spares_issue_order(PART)  # 检修季按窗口速率下的大单
    order_id = issued["order"]["order_id"]
    # 高峰滑出窗口，切回周期均值，需求显著变小。
    clock.advance(55 * DAY_SECONDS)
    diff = platform.spares_order_diff(order_id)
    assert diff["frozen"] is True
    assert diff["diff"] == "excess"
    assert diff["delta_qty"] < 0.0
    orders = {item["order_id"]: item for item in platform.spares_orders(PART)["orders"]}
    assert orders[order_id]["qty"] == issued["order"]["qty"]


def test_receiving_order_replenishes_stock_and_closes_it(platform, clock):
    _seed_overhaul_history(platform, clock)
    order_id = platform.spares_issue_order(PART, qty=6.0)["order"]["order_id"]
    part = platform.spares_receive_order(order_id, qty=2.0)
    assert part["order"]["status"] == "open"
    assert part["order"]["received"] == pytest.approx(2.0)
    # 缺件挂账 13 件，先补挂账：到货 2 件后库存仍是 -11。
    assert part["stock"]["on_hand"] == pytest.approx(-11.0)
    with pytest.raises(SparesError):
        platform.spares_receive_order(order_id, qty=99.0)
    rest = platform.spares_receive_order(order_id)
    assert rest["order"]["status"] == "closed"
    assert rest["order"]["received"] == pytest.approx(6.0)
    with pytest.raises(SparesError):
        platform.spares_receive_order(order_id)
    assert platform.spares.arrivals()["received_total"] == pytest.approx(6.0)


def test_overhaul_month_triggers_reorder_while_calm_month_does_not(platform, clock):
    # 同一速率口径配置下，检修集中月与平静月的建议紧急度应明显不同。
    platform.spares_register_part("p-calm", name="平静备件", initial_stock=2.0)
    now = clock.now()
    for days in (300.0, 220.0, 140.0, 60.0):
        platform.spares_record_replacement(
            "p-calm", qty=1.0, ts=now - days * DAY_SECONDS, backfilled=False
        )
    calm = platform.spares_recommend("p-calm")
    assert calm["basis"] == "cycle"
    platform.spares_set_stock("p-calm", 5.0)
    calm = platform.spares_recommend("p-calm")
    assert calm["urgency"] in ("ok", "reorder")

    _seed_overhaul_history(platform, clock, part_id="p-busy")
    platform.spares_set_stock("p-busy", 2.0)
    busy = platform.spares_recommend("p-busy")
    assert busy["basis"] == "window"
    assert busy["min_stock"] > calm["min_stock"]
    assert busy["urgency"] in ("stockout", "urgent", "reorder")
    assert busy["urgency"] != calm["urgency"] or busy["suggest_qty"] > calm["suggest_qty"]


def test_station_consumption_split_and_pooled_views(platform, clock):
    platform.spares_register_part(PART, name="散热风机")
    now = clock.now()
    # A 站近窗口集中更换，B 站平静。
    for days in (300.0, 200.0, 100.0):
        platform.spares_record_replacement(
            PART, qty=1.0, station=STATION_A, ts=now - days * DAY_SECONDS, backfilled=False
        )
    platform.spares_record_replacement(
        PART, qty=8.0, station=STATION_A, ts=now - 30.0 * DAY_SECONDS, backfilled=False
    )
    for days in (300.0, 200.0, 100.0, 40.0):
        platform.spares_record_replacement(
            PART, qty=1.0, station=STATION_B, ts=now - days * DAY_SECONDS, backfilled=False
        )

    view = platform.spares_part_overview(PART)
    by_station = {item["scope"]: item for item in view["per_station"]}
    assert by_station[STATION_A]["basis"] == "window"
    assert by_station[STATION_B]["basis"] == "cycle"
    # 合并视图把两站事件一起估计，速率约为两站之和。
    pooled = view["pooled"]
    assert pooled["scope"] == "*"
    assert pooled["window_rate_per_day"] == pytest.approx(
        by_station[STATION_A]["window_rate_per_day"] + by_station[STATION_B]["window_rate_per_day"]
    )
    # 库存按站独立。
    platform.spares_set_stock(PART, 5.0, station=STATION_A)
    assert platform.spares_stock(PART, STATION_A)["on_hand"] == pytest.approx(5.0 - 11.0)
    assert platform.spares_stock(PART, STATION_B)["on_hand"] == pytest.approx(-4.0)
    assert platform.spares_stock(PART, "*")["on_hand"] == pytest.approx(5.0 - 15.0)


def test_pooled_order_receipt_can_be_landed_at_a_station(platform, clock):
    platform.spares_register_part(PART)
    platform.spares_record_replacement(PART, qty=2.0, station=STATION_A, ts=clock.now() - DAY_SECONDS)
    order = platform.spares_issue_order(PART, qty=5.0, station="*")
    assert order["order"]["station"] == "*"
    received = platform.spares_receive_order(order["order"]["order_id"], qty=5.0, station=STATION_A)
    assert received["station"] == STATION_A
    assert received["stock"]["on_hand"] == pytest.approx(3.0)


def test_ledger_replays_after_restart(tmp_path, clock):
    from batterystore.app import build_platform
    from batterystore.config import default_config

    platform = build_platform(default_config(), clock=clock, storage_root=str(tmp_path / "state"), rack_count=1)
    _seed_overhaul_history(platform, clock)
    order_id = platform.spares_issue_order(PART, qty=4.0)["order"]["order_id"]
    platform.spares_receive_order(order_id, qty=1.0)

    rebuilt = build_platform(default_config(), clock=clock, storage_root=str(tmp_path / "state"), rack_count=1)
    assert PART in {part["part_id"] for part in rebuilt.spares_snapshot()["parts"]}
    order = {item["order_id"]: item for item in rebuilt.spares_orders(PART)["orders"]}[order_id]
    assert order["qty"] == 4.0 and order["received"] == pytest.approx(1.0)
    rate = rebuilt.spares_rate(PART)
    assert rate["selected_basis"] == "window"
    assert rebuilt.spares_stock(PART)["consumed_total"] == pytest.approx(13.0)

    # 重放幂等：流水条数不因重建而增加。
    assert rebuilt.spares_snapshot()["journal_count"] == platform.spares_snapshot()["journal_count"]


def test_low_confidence_when_history_is_sparse(platform, clock):
    platform.spares_register_part("p-new")
    platform.spares_record_replacement("p-new", qty=1.0, ts=clock.now() - 5.0 * DAY_SECONDS)
    rate = platform.spares_rate("p-new")
    assert rate["low_confidence"] is True
    assert any("可信度低" in reason for reason in rate["reasons"])


def test_rejects_unknown_part_nonpositive_qty_and_future_event(platform, clock):
    with pytest.raises(SparesError):
        platform.spares_record_replacement("nope")
    platform.spares_register_part("p")
    with pytest.raises(SparesError):
        platform.spares_record_replacement("p", qty=0.0)
    with pytest.raises(SparesError):
        platform.spares_record_replacement("p", ts=clock.now() + 10.0)
    with pytest.raises(SparesError):
        platform.spares_set_basis("p", "bogus")
    # 无任何消耗记录时即时建议量为 0，按建议下单会被拒绝。
    assert platform.spares_recommend("p")["suggest_qty"] == 0.0
    with pytest.raises(SparesError):
        platform.spares_issue_order("p")

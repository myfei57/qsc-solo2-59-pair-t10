"""备件消耗估计、最低库存、补货建议与采购单台账。"""

from __future__ import annotations

import pytest

from batterystore.errors import InventoryError
from batterystore.spare.events import is_backfilled
from batterystore.spare.order import CLOSED, OVERDUE, PARTIAL, PurchaseLedger
from batterystore.spare.rate import CYCLE, WINDOW, choose_basis, estimate_rate, rate_stats
from batterystore.spare.stock import SHORTAGE, REORDER, OVERSTOCK, daily_demand_std, normal_quantile

DAY = 86_400.0


def _seed(platform, part="fan-01", stock=0, lead_days=14.0, lot_size=1):
    return platform.spare_register_part(
        part,
        name=part,
        lead_days=lead_days,
        min_order_qty=1,
        lot_size=lot_size,
        initial_stock=stock,
        station="A",
    )


def test_normal_quantile_matches_known_points():
    assert normal_quantile(0.5) == pytest.approx(0.0, abs=1e-9)
    assert normal_quantile(0.95) == pytest.approx(1.64485, abs=1e-3)
    assert normal_quantile(0.9772499) == pytest.approx(2.0, abs=1e-3)
    assert normal_quantile(0.05) == pytest.approx(-1.64485, abs=1e-3)
    assert normal_quantile(0.999) == pytest.approx(3.09023, abs=1e-3)
    with pytest.raises(ValueError):
        normal_quantile(1.0)


def test_rate_stats_window_and_cycle_windows(platform, clock):
    _seed(platform)
    # 窗口外、周期内的一次更换
    platform.spare_record_replacement("fan-01", qty=2, station="A")
    clock.advance(31 * DAY)
    stats = rate_stats(
        [(e["ts"], e["qty"]) for e in platform.spare_events("fan-01")],
        clock.now(),
        30.0,
        180.0,
    )
    assert stats["window_events"] == 0
    assert stats["cycle_events"] == 1
    assert stats["window_qty"] == 0
    assert stats["cycle_qty"] == 2


def test_choose_basis_switching_rules():
    # 平稳：两口径接近 -> 周期
    basis, _ = choose_basis(0.10, 0.10, 3, switch_ratio=1.5)
    assert basis == CYCLE
    # 检修上量：窗口显著高 -> 窗口
    basis, _ = choose_basis(0.4, 0.1, 6, switch_ratio=1.5)
    assert basis == WINDOW
    # 检修刚过：窗口显著低 -> 也切窗口，避免按旧高峰囤货
    basis, _ = choose_basis(0.05, 0.2, 1, switch_ratio=1.5)
    assert basis == WINDOW
    # 窗口内零更换：退回周期均值，不把最低库存估成零
    basis, _ = choose_basis(0.0, 0.2, 0, switch_ratio=1.5)
    assert basis == CYCLE


def test_estimate_rate_uses_window_during_maintenance_burst(platform, clock):
    _seed(platform)
    # 周期内零星更换
    clock.advance(150 * DAY)
    platform.spare_record_replacement("fan-01", qty=1, station="A")
    clock.advance(120 * DAY)
    for _ in range(6):
        platform.spare_record_replacement("fan-01", qty=2, station="A")
    points = [(event["ts"], event["qty"]) for event in platform.spare_events("fan-01")]
    estimate = estimate_rate(
        [type("E", (), {"ts": ts, "qty": qty})() for ts, qty in points],
        clock.now(),
        platform.config.spare,
    )
    assert estimate.basis == WINDOW
    assert estimate.window_rate == pytest.approx(12 / 30)
    assert estimate.window_rate / estimate.cycle_rate >= platform.config.spare.switch_ratio
    # 可强制口径
    forced = estimate_rate(
        [type("E", (), {"ts": ts, "qty": qty})() for ts, qty in points],
        clock.now(),
        platform.config.spare,
        basis=CYCLE,
    )
    assert forced.basis == CYCLE
    assert forced.rate_per_day == estimate.cycle_rate


def test_estimate_rate_short_observation_does_not_over_dilute(platform, clock):
    _seed(platform)
    platform.spare_record_replacement("fan-01", qty=10, station="A")
    clock.advance(10 * DAY)
    plan = platform.spare_plan("fan-01")
    # 只观测了10天，周期均值应按10天摊（1/天），而不是按180天摊
    assert plan["rate"]["cycle_rate"] == pytest.approx(1.0)
    assert plan["rate"]["observed_days"] == pytest.approx(10.0)


def test_backfilled_replacement_feeds_rate_but_not_stock(platform, clock):
    _seed(platform, stock=10)
    on_hand_before = platform.spare_stock("fan-01")["fan-01"]["on_hand"]
    happened = clock.now() - 20 * DAY
    result = platform.spare_record_replacement(
        "fan-01", qty=6, station="A", ts=happened, recorded_ts=clock.now(), note="补录"
    )
    assert result["event"]["backfilled"] is True
    assert result["served"] == 0
    assert result["shortage"] == 0
    assert platform.spare_stock("fan-01")["fan-01"]["on_hand"] == on_hand_before
    events = platform.spare_events("fan-01")
    assert events[0]["backfilled"] is True
    # 速率统计里包含补录量
    plan = platform.spare_plan("fan-01", station="A")
    assert plan["rate"]["window_qty"] == 6
    assert is_backfilled(happened, clock.now()) is True


def test_live_replacement_consumes_stock_and_records_shortage(platform, clock):
    _seed(platform, stock=2)
    result = platform.spare_record_replacement("fan-01", qty=5, station="A")
    assert result["served"] == 2
    assert result["shortage"] == 3
    view = platform.spare_stock("fan-01")["fan-01"]
    assert view["on_hand"] == 0
    assert view["backlog"] == 3
    plan = platform.spare_plan("fan-01", station="A")
    assert plan["policy"]["status"] == SHORTAGE
    assert plan["policy"]["min_stock"] >= 3  # 欠储计入最低库存


def test_unknown_part_and_bad_quantities_rejected(platform):
    with pytest.raises(InventoryError):
        platform.spare_record_replacement("nope", qty=1)
    _seed(platform, stock=1)
    with pytest.raises(InventoryError):
        platform.spare_record_replacement("fan-01", qty=0)
    with pytest.raises(InventoryError):
        platform.spare_set_stock("fan-01", -1, station="A")


def test_min_stock_and_lot_rounded_suggestion(platform, clock):
    _seed(platform, stock=0, lead_days=14.0, lot_size=4)
    # 近窗口均匀消耗，制造可计算的安全库存与建议量
    for days_ago in range(1, 29, 2):
        platform.spare_record_replacement(
            "fan-01", qty=1, station="A", ts=clock.now() - days_ago * DAY, recorded_ts=clock.now() - days_ago * DAY
        )
    plan = platform.spare_plan("fan-01", station="A")
    policy = plan["policy"]
    assert policy["min_stock"] >= 1
    assert policy["target_stock"] >= policy["min_stock"]
    assert policy["suggested_qty"] > 0
    assert policy["suggested_qty"] % 4 == 0  # 按批量取整
    assert policy["status"] in (REORDER, SHORTAGE)


def test_overstock_status_when_stock_above_target(platform, clock):
    _seed(platform, stock=500, lead_days=7.0)
    plan = platform.spare_plan("fan-01", station="A")
    assert plan["policy"]["suggested_qty"] == 0
    assert plan["policy"]["status"] == OVERSTOCK


def test_issued_order_is_frozen_and_only_diff_is_reported(platform, clock):
    _seed(platform, stock=0, lead_days=14.0)
    for _ in range(8):
        platform.spare_record_replacement("fan-01", qty=3, station="A")
    issued = platform.spare_issue_order("fan-01", qty=40)
    order_id = issued["order"]["order_id"]
    # 补录历史 -> 建议重算
    platform.spare_record_replacement(
        "fan-01", qty=30, station="A", ts=clock.now() - 10 * DAY, recorded_ts=clock.now()
    )
    order_after = [o for o in platform.spare_orders("fan-01")["orders"] if o["order_id"] == order_id][0]
    assert order_after["qty"] == 40  # 原单一字未改
    diff = platform.spare_order_diff(order_id)
    assert diff["issued_qty"] == 40
    assert diff["delta"] != 0
    assert diff["hint"] in ("short", "surplus")
    assert diff["message"]


def test_receive_tracks_partial_closed_and_overdue(platform, clock):
    _seed(platform, stock=10, lead_days=10.0)
    issued = platform.spare_issue_order("fan-01", qty=20)
    order_id = issued["order"]["order_id"]
    # 先实时换 12 件：库存只有 10，产生 2 件欠储
    consumed = platform.spare_record_replacement("fan-01", qty=12, station="A")
    assert consumed["shortage"] == 2
    first = platform.spare_receive_order(order_id, qty=8, station="A")
    assert first["order"]["status"] == PARTIAL
    assert first["order"]["received"] == 8
    # 到货先补 2 件欠储，余下 6 件入库
    assert first["covered_backlog"] == 2
    assert platform.spare_stock("fan-01")["fan-01"]["backlog"] == 0
    assert platform.spare_stock("fan-01")["fan-01"]["on_hand"] == 6
    second = platform.spare_receive_order(order_id, qty=12, station="A")
    assert second["order"]["status"] == CLOSED
    # 齐套后不能再收
    with pytest.raises(InventoryError):
        platform.spare_receive_order(order_id, qty=1, station="A")
    # 逾期：新开一张，推进过预期到货
    pending = platform.spare_issue_order("fan-01", qty=5)
    clock.advance(11 * DAY)
    statuses = {o["order_id"]: o["status"] for o in platform.spare_orders("fan-01")["orders"]}
    assert statuses[pending["order"]["order_id"]] == OVERDUE


def test_purchase_ledger_rejects_over_receive_and_duplicate_id():
    ledger = PurchaseLedger()
    order = ledger.issue("po-1", "fan-01", 5, issued_ts=0.0, lead_days=1.0)
    ledger.receive("po-1", 3, ts=10.0, station="A")
    with pytest.raises(InventoryError):
        ledger.receive("po-1", 3, ts=11.0, station="A")
    with pytest.raises(InventoryError):
        ledger.issue("po-1", "fan-01", 1, issued_ts=0.0, lead_days=1.0)
    assert order.in_transit == 5
    assert ledger.in_transit_for("fan-01", 10.0) == 2
    assert ledger.get("po-1").status_at(10.0) == PARTIAL


def test_daily_demand_std_counts_zero_days():
    events = [type("E", (), {"ts": -2 * DAY, "qty": 4})()]
    std = daily_demand_std(events, now=0.0, span_days=4)
    assert std > 0  # 成批一天换4个、其余天为零，波动非零


def test_multi_station_separate_and_merged_views(platform, clock):
    _seed(platform, stock=10)
    platform.spare_set_stock("fan-01", 4, station="B")
    platform.spare_record_replacement("fan-01", qty=2, station="A")
    platform.spare_record_replacement("fan-01", qty=3, station="B")
    plan_a = platform.spare_plan("fan-01", station="A")
    plan_b = platform.spare_plan("fan-01", station="B")
    merged = platform.spare_plan("fan-01")
    assert plan_a["events"] == 1
    assert plan_b["events"] == 1
    assert merged["events"] == 2
    assert set(merged["stations"]) == {"A", "B"}
    view = platform.spare_stock("fan-01")["fan-01"]
    assert view["stations"]["A"]["on_hand"] == 8
    assert view["stations"]["B"]["on_hand"] == 1
    assert view["on_hand"] == 9
    overview = platform.spare_overview()
    assert len(overview["overview"]) == 1
    assert overview["overview"][0]["events"] == 2


def test_spare_state_survives_rebuild(tmp_path):
    from batterystore.app import build_platform
    from batterystore.clock import ManualClock
    from batterystore.config import default_config

    storage = str(tmp_path / "state")
    first = build_platform(default_config(), clock=ManualClock(), storage_root=storage, rack_count=1)
    first.spare_register_part("fan-01", lead_days=10.0, initial_stock=1, station="A")
    first.spare_record_replacement("fan-01", qty=3, station="A")  # 1 件在手 + 2 件欠储
    order = first.spare_issue_order("fan-01", qty=4)
    first.spare_receive_order(order["order"]["order_id"], qty=4, station="A")

    second = build_platform(default_config(), clock=ManualClock(), storage_root=storage, rack_count=1)
    assert len(second.spares.list_parts()) == 1
    assert len(second.spare_events("fan-01")) == 1
    orders = second.spare_orders("fan-01")["orders"]
    assert orders[0]["received"] == 4
    assert orders[0]["status"] == CLOSED
    view = second.spare_stock("fan-01")["fan-01"]
    assert view["backlog"] == 0          # 到货已补齐欠储
    assert view["on_hand"] == 2          # 1 在手-1 消耗，4 到货-2 补欠储+2 入库

    # 欠储本身也要跨重启保留：再造一次缺件不补齐直接重建
    first.spare_record_replacement("fan-01", qty=9, station="A")
    assert first.spare_stock("fan-01")["fan-01"]["backlog"] == 7
    third = build_platform(default_config(), clock=ManualClock(), storage_root=storage, rack_count=1)
    assert third.spare_stock("fan-01")["fan-01"]["backlog"] == 7


def test_spare_snapshot_reports_backfills_and_overdue(platform, clock):
    _seed(platform, stock=10, lead_days=5.0)
    platform.spare_record_replacement(
        "fan-01", qty=2, station="A", ts=clock.now() - 10 * DAY, recorded_ts=clock.now()
    )
    platform.spare_issue_order("fan-01", qty=3)
    clock.advance(6 * DAY)
    snapshot = platform.spare_snapshot()
    assert snapshot["events"] == 1
    assert snapshot["backfilled_events"] == 1
    assert snapshot["open_orders"] == 1
    assert snapshot["overdue_orders"] == 1
    assert "A" in snapshot["stations"]

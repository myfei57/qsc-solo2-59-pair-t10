"""控制台 API 与页面服务。"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import support
from batterystore.console.pages import PageLibrary
from batterystore.console.server import ConsoleServer, probe_endpoints


def _get(base_url: str, path: str):
    with urllib.request.urlopen(f"{base_url}{path}", timeout=5) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def _post(base_url: str, path: str, payload: dict):
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def test_page_library_reports_pages(tmp_path):
    library = PageLibrary("web")
    assert library.resolve("/racks") == "racks"
    assert library.resolve("/alarms.html") == "alarms"
    assert library.resolve("/api/status") is None
    assert library.exists() is True
    assert library.render("racks").startswith("<!DOCTYPE html>")
    assert len(library.index()) == 4
    try:
        library.path_of("bogus")
    except KeyError:
        pass
    else:  # pragma: no cover - 明确失败路径
        raise AssertionError("未知页面应当报错")


def test_console_serves_json_and_pages(platform):
    support.bring_online(platform)
    server = ConsoleServer(platform, port=0)
    base_url = server.start()
    try:
        status, payload = _get(base_url, "/api/status")
        assert status == 200
        assert payload["station"] == "station1"
        status, snapshot = _get(base_url, "/api/snapshot")
        assert status == 200
        assert "config" in snapshot
        for path in ("/api/racks", "/api/pcs", "/api/grid", "/api/quota", "/api/soc", "/api/alerts"):
            code, _body = _get(base_url, path)
            assert code == 200, path
        code, entry = _get(base_url, "/api/namespace/station:pcs")
        assert code == 200
        assert entry["kind"] == "power-conversion"
        with urllib.request.urlopen(f"{base_url}/racks", timeout=5) as response:
            html = response.read().decode("utf-8")
        assert "电池簇状态" in html
    finally:
        server.stop()


def test_console_actions_and_errors(platform):
    support.bring_online(platform)
    server = ConsoleServer(platform, port=0)
    base_url = server.start()
    try:
        code, payload = _post(base_url, "/api/transfer", {"power_kw": 150.0})
        assert code == 200
        assert payload["ok"] is True
        code, payload = _post(base_url, "/api/fault", {"rack_id": "rk-01", "code": "cell_over_temp"})
        assert code == 200
        assert payload["count"] == 1
        code, payload = _post(base_url, "/api/temperature", {"temperature_c": 30.0})
        assert code == 200
        code, payload = _post(base_url, "/api/balance", {"max_steps": 50})
        assert code == 200
        code, payload = _post(base_url, "/api/charge", {"steps": 2, "dt": 60.0})
        assert code == 200
        code, payload = _post(base_url, "/api/disturb", {"cell_index": 3, "delta_v": 0.02})
        assert code == 200
        code, payload = _post(base_url, "/api/calibrate", {"soc_pct": 55.0})
        assert code == 200
        code, payload = _post(base_url, "/api/fire/drill", {"zone": "Z1", "confirm_dt": 5.0})
        assert code == 200
        assert payload["ok"] is True
        code, payload = _post(base_url, "/api/fire/release", {})
        assert code == 200
        probe = probe_endpoints(base_url, ["/api/status", "/api/audit"])
        assert probe["/api/status"]["ok"] is True
        assert probe["/api/audit"]["ok"] is True
        try:
            _get(base_url, "/api/not-a-route")
        except urllib.error.HTTPError as error:
            assert error.code == 404
        else:  # pragma: no cover - 明确失败路径
            raise AssertionError("未知路由应当返回 404")
    finally:
        server.stop()


def test_console_spares_workflow(platform):
    support.bring_online(platform)
    server = ConsoleServer(platform, port=0)
    base_url = server.start()
    try:
        code, created = _post(base_url, "/api/spares/part", {"part_id": "fan", "initial_stock": 2.0})
        assert code == 200 and created["part"]["part_id"] == "fan"
        code, _repl = _post(
            base_url,
            "/api/spares/replacement",
            {"part_id": "fan", "qty": 3.0, "backfilled": True, "ts": platform.clock.now() - 86400.0},
        )
        assert code == 200
        code, stock = _get(base_url, "/api/spares/part/fan/stock")
        assert code == 200 and stock["on_hand"] == -1.0
        code, rec = _get(base_url, "/api/spares/part/fan/recommend")
        assert code == 200 and "min_stock" in rec and "suggest_qty" in rec
        code, order = _post(base_url, "/api/spares/order", {"part_id": "fan", "qty": 2.0})
        assert code == 200
        order_id = order["order"]["order_id"]
        code, diff = _get(base_url, f"/api/spares/order/{order_id}/diff")
        assert code == 200 and diff["frozen"] is True and "message" in diff
        code, received = _post(base_url, f"/api/spares/order/{order_id}/receive", {"qty": 2.0})
        assert code == 200 and received["stock"]["on_hand"] == 1.0
        code, overview = _get(base_url, "/api/spares")
        assert code == 200 and overview["parts"][0]["part"]["part_id"] == "fan"
        code, events = _get(base_url, "/api/spares/events?backfilled=true")
        assert code == 200 and len(events["events"]) == 1
        code, orders = _get(base_url, "/api/spares/orders")
        assert code == 200 and orders["arrivals"]["closed_orders"] == 1
    finally:
        server.stop()


def test_console_rejects_bad_numeric_parameter(platform):
    support.bring_online(platform)
    server = ConsoleServer(platform, port=0)
    base_url = server.start()
    try:
        request = urllib.request.Request(
            f"{base_url}/api/discharge",
            data=json.dumps({"power_kw": "not-a-number"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as error:
            assert error.code == 409
            body = json.loads(error.read().decode("utf-8"))
            assert body["component"] == "console"
        else:  # pragma: no cover - 明确失败路径
            raise AssertionError("非法参数应当返回 409")
    finally:
        server.stop()

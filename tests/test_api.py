"""HTTP surface: health never exposes the key, scenarios, inject guard, the SSE stream."""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app import main
from app.config import openrouter_key


def test_health_has_boot_id_and_no_key():
    with TestClient(main.app) as c:
        d = c.get("/api/health").json()
    assert d["ok"] and d["boot_id"] == main.BOOT_ID
    assert set(d["models"]) == {"triage", "render", "engineer"}
    assert isinstance(d["key_present"], bool)
    key = openrouter_key()
    if key:
        assert key not in json.dumps(d)


def test_scenarios_and_inject_guard():
    with TestClient(main.app) as c:
        c.post("/api/reset")
        sc = c.get("/api/scenarios").json()
        assert len(sc["scenarios"]) == 8
        assert sum(s["visitor"] for s in sc["scenarios"]) == 3
        assert c.post("/api/inject", json={"scenario_id": "nope"}).status_code == 404
        r = c.post("/api/inject", json={"scenario_id": "core_uplink_down"})
        assert r.status_code == 200 and r.json()["ok"]
        r2 = c.post("/api/inject", json={"scenario_id": "cert_expiry"})
        assert r2.status_code == 409 and r2.json()["error"] == "busy"
        c.post("/api/reset")


def test_index_and_static():
    with TestClient(main.app) as c:
        html = c.get("/").text
        assert "Signal Desk" in html and "alpinejs@3.17.4" in html
        assert "barcik<b>.</b>training" in html
        assert c.get("/static/riso.css").status_code == 200

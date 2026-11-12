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


FORBIDDEN = ('"truth"', "scenario_id", '"pattern"', "noise:", "tight:", "group:", "orphan:", "SECRET")


def test_no_api_response_carries_ground_truth():
    """Every JSON endpoint, with a scenario playing and events released: no hidden truth."""
    with TestClient(main.app) as c:
        c.post("/api/reset")
        bodies = [c.post("/api/inject", json={"scenario_id": "core_uplink_down"}).text]
        for _ in range(5):                                            # 5 x 4 simulated minutes
            main.sim.clock.pause()
            main.sim.clock._sim_anchor += 240
            main.sim.step()
        assert main.sim.total > 0 and any(e.truth for e in main.sim.recent)   # truth exists server-side
        for path in ("/api/health", "/api/scenarios", "/api/proposals", "/api/rules", "/api/incidents",
                     "/api/budget"):
            r = c.get(path)
            assert r.status_code == 200, path
            bodies.append(r.text)
        bodies.append(json.dumps(main.sim.snapshot()))                # the stream's hello / tick
        bodies.append(json.dumps(main.engineer.snapshot()))
        for b in bodies:
            for bad in FORBIDDEN:
                assert bad not in b, (bad, b[:200])
        c.post("/api/reset")


def test_public_mode_tightens_caps(monkeypatch):
    from app import config
    monkeypatch.setenv("SIGNAL_DESK_PUBLIC", "1")
    config.load_config.cache_clear()
    try:
        cfg = config.load_config()
        pub = cfg["public"]["caps"]
        assert cfg["caps"]["engineer_cooldown_s"] == pub["engineer_cooldown_s"] >= 300
        assert cfg["caps"]["usd_per_day_global"] == pub["usd_per_day_global"]
        assert not config.dev_rules_enabled()
    finally:
        monkeypatch.delenv("SIGNAL_DESK_PUBLIC")
        config.load_config.cache_clear()
    assert config.load_config()["caps"]["engineer_cooldown_s"] == 90


def test_unhandled_errors_are_calm_json(monkeypatch):
    def boom():
        raise RuntimeError("secret detail")
    monkeypatch.setattr(main.budget, "snapshot", boom)
    with TestClient(main.app, raise_server_exceptions=False) as c:
        r = c.get("/api/budget")
    assert r.status_code == 500 and r.json() == {"ok": False, "error": "server_error"}
    assert "secret detail" not in r.text


def test_login_gate(monkeypatch):
    monkeypatch.setenv("LOGIN_ENABLED", "1")
    monkeypatch.setenv("APP_PASSWORD", "orbit-maple-1234")
    monkeypatch.setenv("ADMIN_PASSWORD", "admin-pass-5678")
    with TestClient(main.app) as c:
        assert c.get("/", follow_redirects=False).status_code == 303
        assert c.get("/api/health").status_code == 401
        assert c.get("/static/riso.css").status_code == 200           # the login page's stylesheet
        assert "Sign in" in c.get("/login").text
        bad = c.post("/login", data={"username": "guest", "password": "nope"}, follow_redirects=False)
        assert bad.status_code == 401 and "orbit" not in bad.text
        ok = c.post("/login", data={"username": "guest", "password": "orbit-maple-1234"}, follow_redirects=False)
        assert ok.status_code == 303 and "sd_session" in ok.headers.get("set-cookie", "")
        h = c.get("/api/health").json()
        assert h["login"] is True and "orbit" not in json.dumps(h) and "admin-pass" not in json.dumps(h)
    monkeypatch.delenv("LOGIN_ENABLED")
    with TestClient(main.app) as c:
        assert c.get("/api/health").status_code == 200                # local runs stay open


def test_index_and_static():
    with TestClient(main.app) as c:
        html = c.get("/").text
        assert "Signal Desk" in html and "alpinejs@3.17.4" in html
        assert "barcik<b>.</b>training" in html
        assert c.get("/static/riso.css").status_code == 200

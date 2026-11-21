"""Phase 5: login passcode on every device, phone access only from your own Tailscale devices, PIN on
phone sends (off by default). Fake secrets only; nothing real is sent."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.auth import Auth, AuthError, hash_secret, request_origin
from app.main import create_app
from app.settings import RemoteConfig
from tests.test_api import LOCAL, make_services
from tests.test_orders_submit import ORDER_WRITES, new_draft, setup

PASS, PIN = "correct horse", "4821"
REMOTE = "https://desk-pc.tail1234.ts.net"
ME = {"Tailscale-User-Login": "me@example.com"}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def make_auth(tmp_path: Path, now: list[float], **cfg: Any) -> Auth:
    p = tmp_path / "secrets" / "login.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"passcode": hash_secret(PASS), "pin": hash_secret(PIN)}), encoding="utf-8")
    return Auth(RemoteConfig(**{"allow_remote": True, **cfg}), p, clock=lambda: now[0],
                devices_path=tmp_path / "secrets" / "trusted_devices.json")


def test_hash_is_not_the_secret_and_verifies(tmp_path: Path) -> None:
    rec = hash_secret(PASS)
    assert PASS not in json.dumps(rec)
    a = make_auth(tmp_path, [0.0])
    assert a.passcode_set() and a.pin_set()
    assert PASS not in (tmp_path / "secrets" / "login.json").read_text(encoding="utf-8")


def test_origin_detection() -> None:
    assert request_origin({b"host": b"127.0.0.1:8000"}) == (False, None)
    assert request_origin({b"host": b"desk-pc.tail1234.ts.net"}) == (True, None)          # no identity: refused
    assert request_origin({b"host": b"127.0.0.1", b"tailscale-user-login": b"me@example.com"}) == (True, "me@example.com")


def test_every_api_route_needs_login(tmp_path: Path) -> None:
    now = [1000.0]
    svc = make_services(tmp_path)
    app = create_app(svc, start_pollers=False, auth=make_auth(tmp_path, now))
    with TestClient(app, base_url=LOCAL) as c:
        checked = 0
        for path, ops in app.openapi()["paths"].items():
            if not path.startswith("/api/") or path.startswith("/api/auth/"):
                continue
            for m in (o.upper() for o in ops):
                r = c.request(m, re.sub(r"\{[^}]+\}", "x", path), json={})
                assert r.status_code == 401 and r.json()["code"] == "login_required", (m, path)
                checked += 1
        assert checked > 30
        st = c.get("/api/auth/status").json()
        assert st["required"] and not st["logged_in"] and not st["remote"] and st["remote_orders"] is False


def test_login_lockout_expiry_logout(tmp_path: Path) -> None:
    now = [1000.0]
    svc = make_services(tmp_path)
    app = create_app(svc, start_pollers=False, auth=make_auth(tmp_path, now))
    with TestClient(app, base_url=LOCAL) as c:
        r = c.post("/api/auth/login", json={"passcode": "nope"})
        assert r.status_code == 401 and r.json() == {"detail": "wrong_passcode", "code": "wrong_passcode", "attempts_left": 4}
        for _ in range(4):
            r = c.post("/api/auth/login", json={"passcode": "nope"})
        assert r.status_code == 429 and r.json()["code"] == "locked"
        assert c.post("/api/auth/login", json={"passcode": PASS}).status_code == 429     # locked even when right
        now[0] += 15 * 60 + 1
        r = c.post("/api/auth/login", json={"passcode": PASS})
        assert r.status_code == 200
        cookie = r.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=strict" in cookie and "max-age=2592000" in cookie
        assert PASS not in r.text
        assert c.get("/api/health").status_code == 200
        now[0] += 30 * 86400 + 1                                                      # home session: 30 days
        assert c.get("/api/health").status_code == 401
        assert c.post("/api/auth/login", json={"passcode": PASS}).status_code == 200
        c.post("/api/auth/logout")
        assert c.get("/api/health").status_code == 401


def test_passcode_not_set_refuses_login(tmp_path: Path) -> None:
    svc = make_services(tmp_path)
    auth = Auth(RemoteConfig(), tmp_path / "none.json")
    with TestClient(create_app(svc, start_pollers=False, auth=auth), base_url=LOCAL) as c:
        assert c.post("/api/auth/login", json={"passcode": PASS}).json()["code"] == "passcode_not_set"
        assert c.get("/api/auth/status").json()["passcode_set"] is False


def test_remote_needs_tailscale_identity(tmp_path: Path) -> None:
    now = [1000.0]
    svc = make_services(tmp_path)
    app = create_app(svc, start_pollers=False, auth=make_auth(tmp_path, now))
    with TestClient(app, base_url=REMOTE) as c:
        assert c.get("/api/auth/status").status_code == 403                     # public (Funnel) / unknown traffic
        assert c.post("/api/auth/login", json={"passcode": PASS}).status_code == 403
        r = c.post("/api/auth/login", json={"passcode": PASS}, headers={**ME, "Origin": REMOTE})
        assert r.status_code == 200 and "secure" in r.headers["set-cookie"].lower()
        assert "max-age=86400" in r.headers["set-cookie"].lower()                     # phone session: 24 h
        assert c.get("/api/health", headers=ME).status_code == 200
        assert c.get("/api/auth/status", headers=ME).json()["remote"] is True
        assert c.post("/api/auth/logout", headers={**ME, "Origin": "https://evil.example"}).status_code == 403
    other = create_app(svc, start_pollers=False, auth=make_auth(tmp_path, now, allowed_logins=["me@example.com"]))
    with TestClient(other, base_url=REMOTE) as c:
        assert c.get("/api/auth/status", headers={"Tailscale-User-Login": "someone@else.com"}).status_code == 403
        assert c.get("/api/auth/status", headers=ME).status_code == 200


def test_this_computer_only_refuses_every_remote_request(tmp_path: Path) -> None:
    svc = make_services(tmp_path)
    app = create_app(svc, start_pollers=False, auth=make_auth(tmp_path, [1000.0], allow_remote=False))
    with TestClient(app, base_url=REMOTE) as c:
        r = c.post("/api/auth/login", json={"passcode": PASS}, headers={**ME, "Origin": REMOTE})
        assert r.status_code == 403 and "allow_remote" in r.json()["detail"]
        assert c.get("/", headers=ME).status_code == 403
    with TestClient(app, base_url=LOCAL) as c:                                   # this computer still works
        assert c.post("/api/auth/login", json={"passcode": PASS}).status_code == 200
    assert RemoteConfig().allow_remote is False                                  # default: this computer only


def test_home_session_does_not_work_remotely(tmp_path: Path) -> None:
    a = make_auth(tmp_path, [1000.0])
    token, _ttl = a.login(PASS, remote=False, who=None)
    assert a.check(token, remote=False) and not a.check(token, remote=True)


@pytest.mark.anyio
async def test_phone_send_rules(tmp_path: Path) -> None:
    now = [1000.0]
    svc = await setup(tmp_path)
    limit = await new_draft(svc)
    market = await new_draft(svc, order_type="MARKET", price=None)
    for enabled in (False, True):
        app = create_app(svc, start_pollers=False, auth=make_auth(tmp_path, now, orders_enabled=enabled))
        with TestClient(app, base_url=REMOTE, headers={**ME, "Origin": REMOTE}) as c:
            assert c.post("/api/auth/login", json={"passcode": PASS}).status_code == 200
            r = c.post("/api/orders/submit", json={"draft_id": limit.id, "approved": True, "pin": PIN})
            if not enabled:
                assert r.status_code == 403 and r.json()["error"] == "remote_orders_off"
                continue
            assert c.post("/api/orders/submit", json={"draft_id": market.id, "approved": True, "pin": PIN}) \
                .json()["error"] == "remote_market_blocked"
            bad = c.post("/api/orders/submit", json={"draft_id": limit.id, "approved": True, "pin": "0000"})
            assert bad.status_code == 401 and bad.json()["error"] == "wrong_pin"
            assert c.post("/api/orders/submit", json={"draft_id": limit.id, "approved": True}).json()["error"] == "wrong_pin"
            ok = c.post("/api/orders/submit", json={"draft_id": limit.id, "approved": True, "pin": PIN}).json()
            assert ok["status"] == "simulated"                                       # DRY RUN still applies
    assert ORDER_WRITES == []
    assert PIN not in (tmp_path / "trades.jsonl").read_text(encoding="utf-8")


def test_real_app_requires_login() -> None:
    import app.main as m

    assert m.app.state.auth_holder["required"] is True                            # fails closed without auth


@pytest.mark.anyio
async def test_trusted_device_skips_pin_untrusted_still_needs_it(tmp_path: Path) -> None:
    """Cloud plan (2026-11-21): PIN once trusts a browser; it then sends like the home PC."""
    now = [1000.0]
    svc = await setup(tmp_path)
    limit = await new_draft(svc)
    market = await new_draft(svc, order_type="MARKET", price=None)
    auth = make_auth(tmp_path, now)                       # orders_enabled stays False (phone rules)
    app = create_app(svc, start_pollers=False, auth=auth)
    with TestClient(app, base_url=REMOTE, headers={**ME, "Origin": REMOTE}) as c:
        assert c.post("/api/auth/login", json={"passcode": PASS}).status_code == 200
        st = c.get("/api/auth/status").json()
        assert st["remote"] is True and st["trusted"] is False
        assert c.post("/api/orders/submit", json={"draft_id": limit.id, "approved": True}).json()["error"]             == "remote_orders_off"                        # untrusted: phone rules
        assert c.post("/api/auth/trust", json={"pin": "0000", "name": "Desk PC"}).json()["code"] == "wrong_pin"
        assert c.post("/api/auth/trust", json={"pin": PIN, "name": "  "}).json()["code"] == "device_name_missing"
        r = c.post("/api/auth/trust", json={"pin": PIN, "name": "Desk PC"})
        assert r.status_code == 200 and "desk_device" in r.headers["set-cookie"]
        assert "httponly" in r.headers["set-cookie"].lower() and "secure" in r.headers["set-cookie"].lower()
        assert c.get("/api/auth/status").json()["trusted"] is True
        ok = c.post("/api/orders/submit", json={"draft_id": limit.id, "approved": True}).json()
        assert ok["status"] == "simulated"                # no PIN, no orders_enabled needed; DRY RUN still on
        assert c.post("/api/orders/submit", json={"draft_id": market.id, "approved": True}).json().get("error")             != "remote_market_blocked"                    # home-PC rules: market orders not blocked by phone rule
        devs = c.get("/api/auth/devices").json()["devices"]
        assert [(d["name"], d["current"]) for d in devs] == [("Desk PC", True)]
        stored = (tmp_path / "secrets" / "trusted_devices.json").read_text(encoding="utf-8")
        assert c.cookies.get("desk_device") not in stored  # only the hash is stored
        assert c.delete(f"/api/auth/devices/{devs[0]['id']}").json()["ok"] is True
        assert c.get("/api/auth/status").json()["trusted"] is False
        late = await new_draft(svc)
        assert c.post("/api/orders/submit", json={"draft_id": late.id, "approved": True}).json()["error"]             == "remote_orders_off"                        # removed: back to phone rules
    assert ORDER_WRITES == []


def test_trust_is_bound_to_the_tailscale_login_and_limited(tmp_path: Path) -> None:
    a = make_auth(tmp_path, [1000.0])
    with pytest.raises(AuthError):
        a.trust_device(PIN, None, "home")                 # the home PC needs no trust
    token, max_age = a.trust_device(PIN, "me@example.com", "Desk PC")
    assert max_age == 365 * 86400
    assert a.trusted(token, "me@example.com") and not a.trusted(token, "friend@example.com")
    assert not a.trusted("made-up", "me@example.com") and not a.trusted(None, "me@example.com")
    for i in range(4):
        a.trust_device(PIN, "me@example.com", f"dev {i}")
    with pytest.raises(AuthError) as e:
        a.trust_device(PIN, "me@example.com", "one too many")
    assert e.value.code == "too_many_devices"
    assert a.remove_all_devices() == 5 and not a.trusted(token, "me@example.com")


def test_trusted_login_lasts_like_the_home_pc(tmp_path: Path) -> None:
    a = make_auth(tmp_path, [1000.0], remote_session_hours=24, local_session_days=30)
    assert a.login(PASS, remote=True, who="me@example.com")[1] == 24 * 3600
    assert a.login(PASS, remote=True, who="me@example.com", trusted=True)[1] == 30 * 86400


def test_device_routes_need_login(tmp_path: Path) -> None:
    now = [1000.0]
    app = create_app(make_services(tmp_path), start_pollers=False, auth=make_auth(tmp_path, now))
    with TestClient(app, base_url=REMOTE, headers={**ME, "Origin": REMOTE}) as c:
        assert c.get("/api/auth/devices").status_code == 401
        assert c.post("/api/auth/trust", json={"pin": PIN, "name": "x"}).status_code == 401
        assert c.post("/api/auth/devices/remove-all").status_code == 401

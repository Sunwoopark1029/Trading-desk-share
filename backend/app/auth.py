"""Login passcode, order PIN and sessions (phase 5).

- The passcode and the PIN are set by the user with scripts\\set_passcode.cmd (app.set_passcode). Only a
  scrypt hash is stored, in secrets/login.json (gitignored; Claude doesn't read secrets/). Never logged.
- Every /api route needs a session (cookie), on every device. Sessions live in memory only (a restart logs
  everyone out). Home PC sessions last remote.local_session_days, other devices remote.remote_session_hours.
- Too many wrong passcodes / PINs lock that device out for remote.lockout_minutes.

Remote access (Tailscale docs, read 2026-11-17, https://tailscale.com/kb/1312/serve):
- "tailscale serve" proxies tailnet HTTPS to this localhost-only server and adds Tailscale-User-Login
  (+ -Name, -Profile-Pic) for tailnet traffic only; Funnel (public) traffic has NO identity headers, and
  incoming copies of these headers are stripped. So a request that looks remote but has no
  Tailscale-User-Login is refused: that is public (Funnel) or unknown traffic.
- Whether serve rewrites the Host header is not documented, so "remote" = identity header present OR a
  *.ts.net Host.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.settings import REPO_ROOT

LOGIN_PATH = REPO_ROOT / "secrets" / "login.json"
DEVICES_PATH = REPO_ROOT / "secrets" / "trusted_devices.json"   # hashes of trusted-device tokens only
COOKIE = "desk_session"
DEVICE_COOKIE = "desk_device"
DEVICE_COOKIE_DAYS = 365
MAX_DEVICES = 5
SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1, "dklen": 32}
PUBLIC_API = frozenset({"/api/auth/status", "/api/auth/login", "/api/auth/logout"})
IDENTITY = b"tailscale-user-login"


class AuthError(Exception):
    def __init__(self, code: str, status: int = 401, **params: Any) -> None:
        super().__init__(code)
        self.code, self.status, self.params = code, status, params


def hash_secret(value: str) -> dict[str, Any]:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(value.encode("utf-8"), salt=salt, n=SCRYPT["n"], r=SCRYPT["r"], p=SCRYPT["p"],
                        dklen=SCRYPT["dklen"], maxmem=64 * 1024 * 1024)
    return {"salt": base64.b64encode(salt).decode(), "hash": base64.b64encode(dk).decode(), **SCRYPT}


def verify_secret(value: str, rec: dict[str, Any]) -> bool:
    try:
        salt = base64.b64decode(rec["salt"])
        want = base64.b64decode(rec["hash"])
        dk = hashlib.scrypt(value.encode("utf-8"), salt=salt, n=int(rec["n"]), r=int(rec["r"]), p=int(rec["p"]),
                            dklen=len(want), maxmem=64 * 1024 * 1024)
    except (KeyError, ValueError, TypeError):
        return False
    return hmac.compare_digest(dk, want)


def request_origin(headers: dict[bytes, bytes]) -> tuple[bool, str | None]:
    """(remote?, Tailscale login or None) from raw ASGI headers."""
    login = headers.get(IDENTITY)
    host = headers.get(b"host", b"").decode("latin-1").split(":")[0].lower()
    who = login.decode("latin-1").strip() if login else None
    return (who is not None or host.endswith(".ts.net")), (who or None)


@dataclass
class Session:
    expires: float
    remote: bool


@dataclass
class _Fails:
    count: int = 0
    locked_until: float = 0.0


def _token_hash(token: str) -> str:
    """Device tokens are 32 random bytes, so a plain SHA-256 is enough (no slow hash needed)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass
class Auth:
    cfg: Any                                   # settings.RemoteConfig
    path: Path = LOGIN_PATH
    clock: Callable[[], float] = time.time
    sessions: dict[str, Session] = field(default_factory=dict)
    fails: dict[str, _Fails] = field(default_factory=dict)
    devices_path: Path = DEVICES_PATH

    # ── stored secrets ──
    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def passcode_set(self) -> bool:
        return isinstance(self._load().get("passcode"), dict)

    def pin_set(self) -> bool:
        return isinstance(self._load().get("pin"), dict)

    # ── lockout ──
    def _check_lock(self, key: str) -> None:
        f = self.fails.get(key)
        now = self.clock()
        if f and f.locked_until > now:
            raise AuthError("locked", 429, minutes=max(1, round((f.locked_until - now) / 60)))

    def _failed(self, key: str, code: str) -> AuthError:
        f = self.fails.setdefault(key, _Fails())
        f.count += 1
        left = self.cfg.max_failed_attempts - f.count
        if left <= 0:
            f.count, f.locked_until = 0, self.clock() + self.cfg.lockout_minutes * 60
            return AuthError("locked", 429, minutes=self.cfg.lockout_minutes)
        return AuthError(code, 401, attempts_left=left)

    # ── sessions ──
    def login(self, passcode: str, remote: bool, who: str | None, trusted: bool = False) -> tuple[str, int]:
        """(token, max_age seconds). Raises AuthError (wrong / locked / not set).
        A trusted browser gets the home-PC login length."""
        key = f"login:{who or ('remote' if remote else 'local')}"
        self._check_lock(key)
        rec = self._load().get("passcode")
        if not isinstance(rec, dict):
            raise AuthError("passcode_not_set", 403)
        if not isinstance(passcode, str) or not passcode or not verify_secret(passcode, rec):
            raise self._failed(key, "wrong_passcode")
        self.fails.pop(key, None)
        ttl = int(self.cfg.remote_session_hours * 3600 if remote and not trusted
                  else self.cfg.local_session_days * 86400)
        token = secrets.token_urlsafe(32)
        now = self.clock()
        self.sessions = {k: s for k, s in self.sessions.items() if s.expires > now}
        self.sessions[token] = Session(now + ttl, remote)
        return token, ttl

    def check(self, token: str | None, remote: bool) -> bool:
        s = self.sessions.get(token or "")
        if s is None:
            return False
        if s.expires <= self.clock() or s.remote != remote:   # a home session can't be used from away
            if s.expires <= self.clock():
                self.sessions.pop(token or "", None)
            return False
        return True

    def logout(self, token: str | None) -> None:
        self.sessions.pop(token or "", None)

    def verify_pin(self, pin: Any, who: str | None) -> None:
        key = f"pin:{who or 'remote'}"
        self._check_lock(key)
        rec = self._load().get("pin")
        if not isinstance(rec, dict):
            raise AuthError("pin_not_set", 403)
        if not isinstance(pin, str) or not pin or not verify_secret(pin, rec):
            raise self._failed(key, "wrong_pin")
        self.fails.pop(key, None)

    # ── trusted devices (cloud plan, user choice 2026-11-21) ──
    # A browser becomes trusted after the PIN is typed on it once. It then follows the home-PC order rules
    # (no PIN per order / arm). Only the hash of its random token is stored, together with the Tailscale
    # login that trusted it; the token only counts for that same login.
    def _devices(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self.devices_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []

    def _save_devices(self, devices: list[dict[str, Any]]) -> None:
        self.devices_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.devices_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(devices), encoding="utf-8")
        tmp.replace(self.devices_path)

    def trust_device(self, pin: Any, who: str | None, name: Any) -> tuple[str, int]:
        """PIN once -> (device token, cookie max_age). Raises AuthError."""
        if who is None:
            raise AuthError("trust_remote_only", 400)       # the home PC needs no trust
        clean = " ".join(str(name or "").split())[:40]
        if not clean:
            raise AuthError("device_name_missing", 400)
        self.verify_pin(pin, who)
        devices = self._devices()
        if len(devices) >= MAX_DEVICES:
            raise AuthError("too_many_devices", 409, max=MAX_DEVICES)
        token = secrets.token_urlsafe(32)
        now = int(self.clock())
        devices.append({"id": secrets.token_hex(6), "name": clean, "who": who, "hash": _token_hash(token),
                        "added": now, "last_used": now})
        self._save_devices(devices)
        return token, DEVICE_COOKIE_DAYS * 86400

    def trusted(self, token: str | None, who: str | None) -> bool:
        if not token or who is None:
            return False
        h = _token_hash(token)
        devices = self._devices()
        for d in devices:
            if d.get("who") == who and hmac.compare_digest(str(d.get("hash", "")), h):
                now = int(self.clock())
                if now - int(d.get("last_used") or 0) > 3600:   # write at most once an hour
                    d["last_used"] = now
                    self._save_devices(devices)
                return True
        return False

    def list_devices(self, current: str | None = None) -> list[dict[str, Any]]:
        h = _token_hash(current) if current else None
        return [{"id": d.get("id"), "name": d.get("name"), "added": d.get("added"),
                 "last_used": d.get("last_used"), "current": h is not None and d.get("hash") == h}
                for d in self._devices()]

    def remove_device(self, device_id: str | None) -> bool:
        devices = self._devices()
        left = [d for d in devices if d.get("id") != device_id]
        if len(left) == len(devices):
            return False
        self._save_devices(left)
        return True

    def remove_all_devices(self) -> int:
        n = len(self._devices())
        self._save_devices([])
        return n


def cookie_token(headers: dict[bytes, bytes], name: str = COOKIE) -> str | None:
    raw = headers.get(b"cookie", b"").decode("latin-1")
    for part in raw.split(";"):
        k, _, v = part.strip().partition("=")
        if k == name:
            return v or None
    return None


def request_trusted(request: Any) -> bool:
    """Is this (remote) request coming from a trusted browser of the same Tailscale login?"""
    auth = request.app.state.auth_holder.get("auth")
    if auth is None:
        return False
    return auth.trusted(request.cookies.get(DEVICE_COOKIE), getattr(request.state, "who", None))

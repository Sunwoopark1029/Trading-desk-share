"""Set the login passcode and the order PIN (run by scripts\\set_passcode.cmd).

You type them here, hidden; nothing is shown or printed. Only a scrypt hash is saved, to
secrets/login.json (gitignored). Restarting the dashboard is not needed; logged-in devices stay logged in
until their session ends (use "Log out" to end one sooner).
"""

from __future__ import annotations

import getpass
import json
import os
import sys

from app.auth import DEVICES_PATH, LOGIN_PATH, hash_secret


def ask(prompt: str, check) -> str:  # type: ignore[no-untyped-def]
    while True:
        a = getpass.getpass(prompt)
        problem = check(a)
        if problem:
            print(problem)
            continue
        if getpass.getpass("Type it again: ") != a:
            print("They don't match. Try again.")
            continue
        return a


def passcode_problem(v: str) -> str | None:
    if len(v) < 8:
        return "Use at least 8 characters."
    return None


def pin_problem(v: str) -> str | None:
    if not v.isdigit() or not 4 <= len(v) <= 8:
        return "The PIN is 4 to 8 digits."
    return None


def main() -> int:
    data: dict = {}
    if LOGIN_PATH.exists():
        try:
            data = json.loads(LOGIN_PATH.read_text(encoding="utf-8"))
        except ValueError:
            data = {}
    print("Login passcode: used on every device to open the dashboard (at least 8 characters).")
    print("Order PIN: asked again before an order is sent from your phone (4 to 8 digits).")
    print("Nothing you type is shown.\n")
    which = "both"
    if data.get("passcode") and data.get("pin"):
        c = input("Change 1) passcode, 2) PIN, 3) both? [3]: ").strip() or "3"
        which = {"1": "passcode", "2": "pin"}.get(c, "both")
    if which in ("passcode", "both"):
        data["passcode"] = hash_secret(ask("New login passcode: ", passcode_problem))
    if which in ("pin", "both"):
        data["pin"] = hash_secret(ask("New order PIN: ", pin_problem))
    LOGIN_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = LOGIN_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, LOGIN_PATH)
    print("\nSaved (only a scrambled hash is stored in secrets\\login.json).")
    if DEVICES_PATH.exists():                 # a new passcode / PIN un-trusts every device
        DEVICES_PATH.unlink()
        print("Trusted devices removed: each device needs the PIN once again to be trusted.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

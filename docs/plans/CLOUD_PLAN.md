# Cloud server plan (approved 2026-11-21; trusted devices replace "PIN for every order")

CLAUDE.md and ROADMAP rules apply. This moves the "Move to a cloud server" item out of ROADMAP "Later".
Provider chosen by the user: **AWS Lightsail, Seoul region, Ubuntu LTS, 2 GB plan, static IP**.

Goal: the dashboard runs all the time on a small cloud server, so data, automatic rules and the chat work
while the home PC is off. Only the user's own devices reach it, through Tailscale. Nothing is public.

---

## 1. What stays the same

- One user, one copy that is "live". Same code, same `config.yaml`, same safety rules (DRY_RUN default,
  drafts, armed rules, PIN for orders from another device, logs in `logs/trades.jsonl`).
- The server listens on `127.0.0.1` only. Tailscale serve is the only way in (as in phase 5).
  No public ports, no port forwarding, no Funnel, no public `0.0.0.0` binding.
- Keys are never in git, chat, logs or the screen.

## 2. How it fits together

```
your PC / phone ──Tailscale (private, encrypted)──▶ Lightsail server (Seoul)
                                                     ├─ tailscale serve (HTTPS, adds your identity)
                                                     ├─ uvicorn on 127.0.0.1:8000 (auto-start, auto-restart)
                                                     ├─ data/  logs/  secrets/  .env   (only on the server)
                                                     └─ calls Toss / Anthropic / Finnhub / FRED / SEC / Yahoo
                                                        from the static IP registered with Toss
GitHub (code only) ──git pull (update script)──▶ server
```

## 3. Editing after the move ("workshop" and "live")

- The **server is the live copy**. The **PC is the workshop**: code is changed and tested there, pushed to
  GitHub, and the server updates with one command (`update.sh`). Nobody edits files on the server.
- New setting `deploy.role: live | workshop` in `config.yaml` (default `live` so nothing changes for anyone
  who does not set it):
  - `workshop`: DRY_RUN is forced on, automatic rules are off (nothing can be armed or fire), and a banner
    says "WORKSHOP — not the live copy".
  - `live`: as today.
- **Toss allows one token per key; a new token cancels the old one.** The Toss client already stops
  (instead of fighting back) when another machine takes the token. Two ways to work with live Toss data
  on the PC:
  - **A (default):** pause the server while you test on the PC (`pause.sh` / `resume.sh`, or a button
    later). Simple, nothing new from Toss.
  - **B:** a second Toss Open API key just for the PC, if Toss allows more than one per account
    (**unverified**: check in the Toss console). Then both can run at once.
- Automatic rules live only on the server (`logs/rules.json` there). The PC never fires anything.

## 4. Keys on the server

- `.env` on the server, created by you over a Tailscale SSH session (steps in the README; never pasted into
  chat). File owned by a dedicated `trading` user, permission `600` (only that user can read it).
- Disk encryption at rest: Lightsail is expected to encrypt instance disks (**unverified**: check the
  Lightsail docs/console before putting keys there).
- An AWS secrets manager was considered: on Lightsail it would need another AWS access key stored on the
  same server, so it adds steps without real protection. Revisit if the server ever moves to EC2.
- Limits on what a leaked key can do: Toss accepts only the registered IP; Anthropic monthly spend limit
  with a separate key for the server; the app's own price band and optional per-order / per-day limits.

## 5. Logins on the server

- On the server every visit comes through Tailscale, so every device counts as "remote":
  `remote.allow_remote: true` and `allowed_logins` set to your own Tailscale account.
- **Trusted devices (user choice 2026-11-21, replaces "PIN for every order everywhere"):**
  - Tailscale's documented identity headers name the user, not the device, so trust is given per
    **browser**: on a device you want to trust, open Settings → "Trust this device", type a name and the
    PIN once. The server stores only a hash of a random device token (in `secrets/`, like the passcode)
    and puts the token in a long-lived, HttpOnly, Secure, SameSite=Strict cookie on that browser.
  - A trusted browser behaves like the home PC does today: no PIN per order or arm, market orders allowed,
    no `orders_enabled` requirement, and logins last `local_session_days`.
  - Every other device (e.g. the phone, unless you trust it) keeps the phone rules: `orders_enabled`, PIN
    for each order / arm, market orders blocked by default, `remote_session_hours` logins.
  - The token only counts together with your Tailscale login (`allowed_logins`) and only while it is not
    revoked. Settings lists trusted devices (name, added, last used) with **Remove** and **Remove all**.
    Changing the passcode or PIN (`set_passcode`) removes them all. At most 5.
- On the home PC (`127.0.0.1`) nothing changes.

## 5b. Settings that belong to one machine

- `config.yaml` comes from git, so an edit made on the server would be overwritten (or block) the next
  `update.sh`. New optional file **`data/config.local.yaml`** (gitignored): any key in it overrides the
  same key in `config.yaml` (e.g. `orders.dry_run`, `remote.allow_remote`, `deploy.role`).
- The PC keeps working as today; it can move its own changes (e.g. `dry_run: false`) into that file later.

## 6. Backups

- Daily script: a compressed copy of `data/` and `logs/` kept 14 days on the server (`backups/`, gitignored).
- Lightsail automatic snapshots of the whole server (daily; small extra cost; **check** the current price).
- Optional: download a backup to the PC now and then (one command in the README).

## 7. What you do (one time)

1. AWS account: two-step login (2FA) on, billing alert (e.g. USD 20/month).
2. Lightsail → create instance: Seoul (`ap-northeast-2`), Linux / Ubuntu LTS, 2 GB plan. Attach a static IP.
   Turn on automatic snapshots.
3. Lightsail firewall: remove the public SSH and HTTP rules once Tailscale works (step 5); until then SSH
   only from your home IP.
4. Toss Open API console: register the server's static IP. **Check** whether the home IP can stay
   registered too, and whether the terms allow running on a cloud server.
5. Install Tailscale on the server (commands in the README), log in with your account, turn on Tailscale SSH.
6. Run `setup.sh` (one command), set the login passcode, create `.env` yourself.
7. Anthropic console: a separate key for the server and a monthly spend limit.
8. Run on the server in **DRY_RUN for a few days**, then decide about real orders (you switch it yourself).

## 8. What Claude Code builds (after approval)

| File | Change |
|---|---|
| `scripts/linux/setup.sh` (new) | Installs system packages, Python 3.15 (via `uv`), Node LTS, the venv and frontend packages, builds the screen, creates the `trading` user and folders, installs the service and the daily backup timer. Safe to run again |
| `scripts/linux/trading-desk.service` (new) | systemd service: starts on boot, restarts on crash, `127.0.0.1:8000` only |
| `scripts/linux/update.sh` (new) | `git pull` → install packages if changed → run tests → build screen → restart → check it answers; on failure goes back to the previous version |
| `scripts/linux/pause.sh`, `resume.sh` (new) | Stop / start the live copy (for workshop option A) |
| `scripts/linux/backup.sh` + timer (new) | Daily copy of `data/` and `logs/`, keeps 14 days |
| `backend/app/settings.py` | `deploy.role` (`live` / `workshop`); `data/config.local.yaml` overrides |
| `backend/app/main.py`, rules / orders start-up | Workshop: force DRY_RUN, rules off |
| `backend/app/auth.py`, `orders/routes.py`, `rules/routes.py`, `main.py` | Trusted devices: trust (PIN once) / list / remove routes, cookie check in the remote order and arm checks, session length |
| `frontend/src/components/HealthBar.tsx`, Settings page, i18n | Workshop banner; "Trusted devices" panel |
| `backend/tests/...` | Workshop mode (DRY_RUN forced, rules refuse to arm), local config overrides, trusted devices (PIN needed to trust, trusted skips PIN, revoked / other login / untrusted still need it) |
| `README.md`, `README.ko.md` | New section "Cloud server": every step above in plain words, update / pause / backup / restore |
| `CLAUDE.md` | Security: a cloud server is allowed under the same rules (localhost + Tailscale only) |
| `docs/ROADMAP.md` | New section from "Later", decision log |

Build order: workshop mode + local config + tests → trusted devices + tests → Linux scripts → README →
you create the server → setup together → DRY_RUN days → switch the PC to workshop.

## 9. Cost (rough, check the Lightsail pricing page)

- Lightsail 2 GB instance: about USD 10–12 / month, static IP included while attached.
- Automatic snapshots: a few USD / month at most for a server this size.
- Claude API, Finnhub, FRED, SEC: unchanged.

## 10. Risks and open questions

1. **Toss**: more than one registered IP? more than one API key per account (workshop option B)? cloud
   use allowed by the terms? (all **unverified**)
2. **Yahoo (yfinance)** often blocks cloud server addresses. The strip boxes, options panel and some
   Research fallbacks may show "error / stale" on the server. **Accepted for now** (user 2026-11-21);
   alternatives are a later decision.
3. **Desktop as "remote"**: decided 2026-11-21: trusted devices (section 5).
4. **Always-on rules**: armed rules will fire while you sleep. That is the purpose, and also the risk;
   the per-order / per-day limits in Settings are worth setting before real orders run on the server.
5. **Lightsail disk encryption** to confirm (section 4).

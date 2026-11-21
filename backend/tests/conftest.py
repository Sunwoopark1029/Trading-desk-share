"""Shared test setup: no test may write to the real logs/ files (trade log, rules)."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _private_logs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.orders.submit as submit
    import app.rules.engine as rules_engine

    monkeypatch.setattr(submit, "TRADES_PATH", tmp_path / "logs" / "trades.jsonl")
    monkeypatch.setattr(rules_engine, "RULES_PATH", tmp_path / "logs" / "rules.json")
    import app.research.store as research_store
    monkeypatch.setattr(research_store, "RESEARCH_DB_PATH", tmp_path / "logs" / "research" / "research.sqlite")


@pytest.fixture(autouse=True)
def _offline_events(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never start the event calendar's background refresh (BLS / FRED / Toss / Finnhub)."""
    import app.events.service as events_service

    monkeypatch.setattr(events_service.EventsService, "kick", lambda self: None)


@pytest.fixture(autouse=True)
def _offline_options(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never call Yahoo: option chains are 'not listed' unless a test sets its own fetch."""
    import app.data.yf_options as yf_options

    def no_chain(symbol: str, n: int) -> None:
        raise LookupError("no_options")

    monkeypatch.setattr(yf_options, "fetch_snapshot", no_chain)


@pytest.fixture(autouse=True)
def _private_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No test reads or writes the real data/ folder: Settings page values (data/user_settings.json) and the
    caches (data/cache: SEC files, sector index, certificate bundle) go to the test's own temp folder."""
    import app.settings as settings
    import app.settings_routes as settings_routes
    from app.user_settings import SettingsFile

    monkeypatch.setattr(settings, "CACHE_DIR", tmp_path / "data" / "cache")

    def private_file(path: Path | None = None, old_orders: Path | None = None) -> SettingsFile:
        return SettingsFile(path or tmp_path / "data" / "user_settings.json",
                            old_orders or tmp_path / "logs" / "orders" / "settings.json")

    monkeypatch.setattr(settings_routes, "SettingsFile", private_file)

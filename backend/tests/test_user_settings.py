"""Settings page: validation, file round trip, live apply, routes. dry_run can never be set here."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import settings_routes
from app.main import create_app
from app.settings import AppConfig, load_config
from app.user_settings import SettingsError, SettingsFile, apply, defaults_from, merge
from tests.test_api import LOCAL, make_services

D = Decimal


def test_defaults_match_config_and_chart_defaults() -> None:
    d = defaults_from(load_config())
    assert d.tickers.default == "SOXS" and d.tickers.watchlist == ["SOXX"]
    assert d.chart.timeframe == "5m"
    assert d.chart.toggles == {"ema": True, "vwap": True, "rsi": False, "fib": False, "swings": False, "lines": True,
                               "aiLines": True, "sessions": False, "volume": True}
    assert d.orders.price_band_pct == D("3") and d.orders.max_order_usd is None and d.ai.daily_usd == D("5")
    assert d.chart.dotted_style == "sparse_dotted" and d.chart.vwap_width == 2
    assert d.chart.color_scheme == "kr" and d.chart.grid is False
    assert d.chart.dotted_opacity == 45 and d.chart.zone_fill_pct == 8 and d.chart.solid_width == 1
    old = merge(d, {"chart": {"vwap_style": "large_dashed"}})          # earlier saved name still loads
    assert old.chart.dotted_style == "large_dashed"


@pytest.mark.parametrize("patch", [
    {"orders": {"dry_run": False}},                    # never settable from the page
    {"orders": {"price_band_pct": "0"}},
    {"orders": {"draft_ttl_seconds": 5}},
    {"orders": {"max_order_usd": "-1"}},
    {"orderbook": {"spread_warn_pct": "50"}},
    {"ai": {"daily_usd": "0"}},
    {"ai": {"default_tier": "turbo"}},
    {"chart": {"toggles": {"macd": True}}},
    {"chart": {"height_pct": 95}},
    {"chart": {"dotted_style": "zigzag"}},
    {"chart": {"dotted_opacity": 5}},
    {"chart": {"zone_fill_pct": 50}},
    {"chart": {"vwap_width": 9}},
    {"tickers": {"watchlist": ["NV DA"]}},
    {"general": {"language": "jp"}},
    {"keys": {"ANTHROPIC_API_KEY": "x"}},
    {"news": {"max_age_hours": 0}},
    "not an object",
])
def test_invalid_settings_rejected(patch: object) -> None:
    with pytest.raises(SettingsError):
        merge(defaults_from(AppConfig()), patch)


def test_merge_partial_normalizes_tickers() -> None:
    s = merge(defaults_from(AppConfig()), {"tickers": {"default": "nvda", "watchlist": ["amd", "AMD", "soxx"],
                                                       "related": {"nvda": ["tsm"]}},
                                           "orders": {"max_order_usd": "500"}})
    assert s.tickers.default == "NVDA" and s.tickers.watchlist == ["AMD", "SOXX"]
    assert s.tickers.related == {"NVDA": ["TSM"]} and s.orders.max_order_usd == D("500")
    assert s.orders.price_band_pct == D("3")          # untouched sections keep their values


def test_apply_changes_live_config() -> None:
    cfg = load_config()
    before = cfg.orders.dry_run                       # whatever the user set in config.yaml
    s = merge(defaults_from(cfg), {"tickers": {"default": "NVDA", "watchlist": ["NVDA", "AMD"]},
                                   "orderbook": {"spread_warn_pct": "0.5"},
                                   "orders": {"price_band_pct": "1.5", "max_orders_per_day": 3},
                                   "ai": {"daily_usd": "2", "raw_candles": False, "web_search_default": True},
                                   "news": {"max_age_hours": 24, "tag_enabled": False}})
    apply(s, cfg)
    assert cfg.tickers.default == "NVDA" and cfg.tickers.compare == ["AMD"] and cfg.news.extra == ["NVDA", "AMD"]
    assert cfg.spread.warn_pct == D("0.5") and cfg.orders.price_band_pct == D("1.5")
    assert cfg.orders.max_orders_per_day == 3 and cfg.budget.daily_usd == D("2")
    assert cfg.llm.candles.mode == "A" and cfg.llm.web_search.default_on is True
    assert cfg.news.max_age_hours == 24 and cfg.news.tag_enabled is False
    assert cfg.orders.dry_run is before                # never changed by the Settings page


def test_file_round_trip_broken_file_and_migration(tmp_path: Path) -> None:
    old = tmp_path / "old.json"
    old.write_text(json.dumps({"price_band_pct": "2", "draft_ttl_seconds": 300}), encoding="utf-8")
    f = SettingsFile(tmp_path / "user_settings.json", old)
    d = defaults_from(AppConfig())
    s = f.load(d)
    assert s.orders.price_band_pct == D("2") and s.orders.draft_ttl_seconds == 300   # migrated
    f.save(merge(s, {"general": {"language": "ko"}}))
    assert f.load(d).general.language == "ko" and f.load(d).orders.price_band_pct == D("2")
    (tmp_path / "user_settings.json").write_text("{broken", encoding="utf-8")
    assert f.load(d).general.language == "en"          # broken file: defaults, app keeps running


@pytest.mark.anyio
async def test_settings_routes(tmp_path: Path) -> None:
    svc = make_services(tmp_path)
    await svc.refresh_stocks()                         # AAPL and SOXS exist in the fake list
    settings_routes.init(svc, SettingsFile(tmp_path / "us.json", tmp_path / "none.json"))
    with TestClient(create_app(svc, start_pollers=False), base_url=LOCAL) as c:
        body = c.get("/api/settings").json()
        assert body["read_only"]["dry_run"] is True and body["settings"]["general"]["language"] == "en"
        r = c.put("/api/settings", json={"orders": {"price_band_pct": "1"}, "tickers": {"watchlist": ["AAPL"]}})
        assert r.status_code == 200 and r.json()["settings"]["orders"]["price_band_pct"] == "1"
        assert svc.config.orders.price_band_pct == D("1") and svc.config.tickers.compare == ["AAPL"]
        assert json.loads((tmp_path / "us.json").read_text(encoding="utf-8"))["orders"]["price_band_pct"] == "1"
        bad = c.put("/api/settings", json={"tickers": {"watchlist": ["ZZZZ"]}})
        assert bad.status_code == 400 and "ZZZZ" in bad.json()["detail"]
        assert c.put("/api/settings", json={"orders": {"dry_run": False}}).status_code == 400
        assert svc.config.orders.dry_run is True
        assert c.put("/api/settings", content="x", headers={"Content-Type": "text/plain"}).status_code == 415
        assert c.put("/api/settings", json={}, headers={"Origin": "https://evil.example"}).status_code == 403
        ui = c.get("/api/ui-config").json()
        assert ui["tickers"]["compare"] == ["AAPL"] and ui["user"]["chart"]["toggles"]["fib"] is False
        reset = c.post("/api/settings/reset").json()
        assert reset["settings"]["orders"]["price_band_pct"] == "3" and svc.config.orders.price_band_pct == D("3")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"

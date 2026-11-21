from decimal import Decimal
from pathlib import Path

import pytest

from app.settings import CONFIG_PATH, ConfigError, key_status, load_config, load_secrets

SECRET_VARS = ("TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET", "FRED_API_KEY")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in SECRET_VARS:
        monkeypatch.delenv(name, raising=False)


def test_repo_config_loads():
    cfg = load_config(CONFIG_PATH)
    assert cfg.pnl.fee_rate == Decimal("0.001")
    assert cfg.pnl.fee_free_below_usd == Decimal("10")
    assert cfg.pnl.fx_spread_bp is None
    assert cfg.polling.stale_after_factor == 3
    assert cfg.toss.rate_limit_safety == 0.8
    assert cfg.tickers.default == "SOXS"
    assert all(e.time.utcoffset() is not None for e in cfg.events)


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_event_without_offset_rejected(tmp_path):
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, 'events:\n  - name: CPI\n    time: "2026-12-10T08:30:00"\n'))
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, "events:\n  - name: CPI\n    time: 2026-12-10T08:30:00\n"))


def test_event_with_offset_ok(tmp_path):
    cfg = load_config(_write(tmp_path, "events:\n  - name: CPI\n    time: 2026-12-10T08:30:00-05:00\n"))
    assert cfg.events[0].time.utcoffset().total_seconds() == -5 * 3600


def test_unknown_key_and_bad_yaml_rejected(tmp_path):
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, "pnl:\n  fee_rat: 0.001\n"))
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, "pnl: [unclosed\n"))
    with pytest.raises(ConfigError):
        load_config(tmp_path / "missing.yaml")


def test_no_server_host_in_config():
    text = CONFIG_PATH.read_text(encoding="utf-8")
    assert "0.0.0.0" not in text
    assert "host:" not in text and "port:" not in text


def test_key_status_set_and_empty_without_values(tmp_path):
    env = tmp_path / ".env"
    env.write_text("TOSS_CLIENT_ID=abc123secret\nTOSS_CLIENT_SECRET=\nOTHER=1\n", encoding="utf-8")
    secrets = load_secrets(env)
    status = key_status(secrets)
    assert status == {"TOSS_CLIENT_ID": "set", "TOSS_CLIENT_SECRET": "empty", "FRED_API_KEY": "empty",
                      "ANTHROPIC_API_KEY": "empty", "FINNHUB_API_KEY": "empty", "MASSIVE_API_KEY": "empty",
                      "SEC_USER_AGENT": "empty"}
    assert "abc123secret" not in repr(secrets) and "abc123secret" not in str(status)
    assert secrets.TOSS_CLIENT_ID.get_secret_value() == "abc123secret"


def test_missing_env_file_is_all_empty(tmp_path):
    status = key_status(load_secrets(tmp_path / "nope.env"))
    assert set(status.values()) == {"empty"}


@pytest.mark.parametrize("url", ["https://evil.example", "http://openapi.tossinvest.com",
                                 "https://openapi.tossinvest.com.evil.example"])
def test_toss_base_url_pinned_to_documented_host(tmp_path, url):
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, f"toss:\n  base_url: {url}\n"))
    ok = load_config(_write(tmp_path, "toss:\n  base_url: https://openapi.tossinvest.com/\n"))
    assert ok.toss.base_url == "https://openapi.tossinvest.com"

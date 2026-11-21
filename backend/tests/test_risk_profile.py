"""Settings -> AI -> Risk profile: "standard" (default for new copies) or "aggressive" (the owner's original text)."""

from pathlib import Path
from typing import Any

import pytest

from app.chat.prompt import RISK_PROFILES, system_blocks, system_prompt
from app.settings import AppConfig, load_config
from app.user_settings import apply, defaults_from, merge
from tests.test_chat_engine import make_engine


def test_profiles_fill_the_prompt() -> None:
    std, agg = system_prompt("standard"), system_prompt("aggressive")
    assert "@RISK@" not in std and "@RISK@" not in agg
    assert "100% of this account can be lost" in agg and "100% of this account" not in std
    assert "Risk tolerance: standard" in std
    assert system_prompt("made-up") == std            # unknown value: the safe default
    assert system_prompt() == std
    for text in (std, agg):                            # the hard rules stay in both
        assert "Numbers: the most important rule" in text and "orders are drafts the user approves" in text
    assert set(RISK_PROFILES) == {"standard", "aggressive"}


def test_system_blocks_use_the_profile_and_keep_the_cache_point() -> None:
    b = system_blocks("1h", "aggressive")
    assert b[0]["text"] == system_prompt("aggressive") and b[0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert system_blocks("1h")[0]["text"] == system_prompt("standard")


def test_default_is_standard_and_settings_page_switches_it() -> None:
    cfg = load_config()
    assert cfg.llm is not None and cfg.llm.risk_profile == "standard"
    d = defaults_from(cfg)
    assert d.ai.risk_profile == "standard"
    s = merge(d, {"ai": {"risk_profile": "aggressive"}})
    apply(s, cfg)
    assert cfg.llm.risk_profile == "aggressive"


def test_bad_value_is_refused() -> None:
    from app.user_settings import SettingsError

    with pytest.raises(SettingsError):
        merge(defaults_from(AppConfig()), {"ai": {"risk_profile": "yolo"}})


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_engine_sends_the_chosen_profile(tmp_path: Path) -> None:
    from tests.test_chat_engine import FakeClient, ready_services   # same helpers as the engine tests

    svc = await ready_services(tmp_path)
    for profile in ("standard", "aggressive"):
        eng = make_engine(svc, tmp_path, FakeClient([]), risk_profile=profile)
        p: dict[str, Any] = eng.params("normal", [{"role": "user", "content": "hi"}])
        assert p["system"][0]["text"] == system_prompt(profile)
        r = eng.params("normal", [{"role": "user", "content": "hi"}], kind="research")
        assert "100% of this account" not in r["system"][0]["text"]   # research chat has no risk text

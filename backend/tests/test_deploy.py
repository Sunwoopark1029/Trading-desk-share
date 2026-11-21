"""Cloud plan: workshop mode (deploy.role) and the per-machine data/config.local.yaml overrides."""

from pathlib import Path

import pytest

from app.settings import LOCAL_CONFIG_PATH, DATA_DIR, ConfigError, load_config


def _cfg(tmp_path: Path, main: str, local: str | None = None) -> Path | tuple[Path, Path]:
    m = tmp_path / "config.yaml"
    m.write_text(main, encoding="utf-8")
    if local is None:
        return m
    lo = tmp_path / "config.local.yaml"
    lo.write_text(local, encoding="utf-8")
    return m, lo


def test_local_file_lives_in_data_and_default_role_is_live() -> None:
    assert LOCAL_CONFIG_PATH == DATA_DIR / "config.local.yaml"
    cfg = load_config(Path(__file__).parents[2] / "config.yaml")   # the committed config.yaml alone
    assert cfg.deploy.role == "live"


def test_live_keeps_settings(tmp_path: Path) -> None:
    cfg = load_config(_cfg(tmp_path, "orders:\n  dry_run: false\nrules:\n  enabled: true\n"))
    assert cfg.deploy.role == "live"
    assert cfg.orders.dry_run is False and cfg.rules.enabled is True


def test_workshop_forces_dry_run_and_rules_off(tmp_path: Path) -> None:
    cfg = load_config(_cfg(tmp_path, "deploy:\n  role: workshop\norders:\n  dry_run: false\nrules:\n  enabled: true\n"))
    assert cfg.deploy.role == "workshop"
    assert cfg.orders.dry_run is True      # never sends, whatever the file says
    assert cfg.rules.enabled is False      # nothing can be armed or fire


def test_local_overrides_merge_nested_keys(tmp_path: Path) -> None:
    main, local = _cfg(tmp_path,
                       "orders:\n  dry_run: true\n  price_band_pct: 3\nremote:\n  allow_remote: false\n",
                       "orders:\n  dry_run: false\nremote:\n  allow_remote: true\n")
    cfg = load_config(main, local)
    assert cfg.orders.dry_run is False and cfg.remote.allow_remote is True
    assert str(cfg.orders.price_band_pct) == "3"          # untouched key kept from config.yaml


def test_local_workshop_wins_over_main_dry_run_false(tmp_path: Path) -> None:
    main, local = _cfg(tmp_path, "orders:\n  dry_run: false\n", "deploy:\n  role: workshop\n")
    cfg = load_config(main, local)
    assert cfg.orders.dry_run is True and cfg.rules.enabled is False


def test_missing_local_file_is_fine(tmp_path: Path) -> None:
    cfg = load_config(_cfg(tmp_path, "orders:\n  dry_run: false\n"), tmp_path / "nope.yaml")
    assert cfg.orders.dry_run is False


def test_bad_local_file_names_both_files(tmp_path: Path) -> None:
    main, local = _cfg(tmp_path, "", "deploy:\n  role: staging\n")
    with pytest.raises(ConfigError, match=r"config\.yaml \+ config\.local\.yaml"):
        load_config(main, local)
    main, local = _cfg(tmp_path, "", "- not a mapping\n")
    with pytest.raises(ConfigError, match="mapping"):
        load_config(main, local)

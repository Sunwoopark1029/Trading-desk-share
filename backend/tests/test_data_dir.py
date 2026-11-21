from pathlib import Path

from app import store, user_settings
from app.data_dir import migrate_to_data_dir
from app.settings import CACHE_DIR, DATA_DIR, REPO_ROOT


def test_paths_point_into_data_folder() -> None:
    assert DATA_DIR == REPO_ROOT / "data" and CACHE_DIR == DATA_DIR / "cache"
    assert store.POSITIONS_PATH == DATA_DIR / "positions.json"
    assert store.OVERLAYS_PATH == DATA_DIR / "overlays.json"
    assert user_settings.SETTINGS_PATH == DATA_DIR / "user_settings.json"
    assert (DATA_DIR / "positions.example.json").is_file()


def test_moves_root_files_and_cache(tmp_path: Path) -> None:
    root, data = tmp_path, tmp_path / "data"
    for name in ("positions.json", "overlays.json", "user_settings.json", "watchlist.json"):
        (root / name).write_text(f'{{"from": "{name}"}}', encoding="utf-8")
    (root / "cache" / "edgar").mkdir(parents=True)
    (root / "cache" / "edgar" / "facts.json").write_text("{}", encoding="utf-8")
    (root / "cache" / "sector_index.json").write_text("[]", encoding="utf-8")

    moved = migrate_to_data_dir(root, data)

    assert moved == ["positions.json", "overlays.json", "user_settings.json", "watchlist.json", "cache/"]
    for name in ("positions.json", "overlays.json", "user_settings.json", "watchlist.json"):
        assert not (root / name).exists()
        assert (data / name).read_text(encoding="utf-8") == f'{{"from": "{name}"}}'
    assert (data / "cache" / "edgar" / "facts.json").is_file()
    assert (data / "cache" / "sector_index.json").is_file()
    assert not (root / "cache").exists()


def test_never_overwrites_existing_data(tmp_path: Path) -> None:
    root, data = tmp_path, tmp_path / "data"
    data.mkdir()
    (root / "watchlist.json").write_text("old", encoding="utf-8")
    (data / "watchlist.json").write_text("new", encoding="utf-8")
    (root / "cache").mkdir()
    (root / "cache" / "a.json").write_text("old a", encoding="utf-8")
    (root / "cache" / "b.json").write_text("old b", encoding="utf-8")
    (data / "cache").mkdir()
    (data / "cache" / "a.json").write_text("new a", encoding="utf-8")

    migrate_to_data_dir(root, data)

    assert (data / "watchlist.json").read_text(encoding="utf-8") == "new"
    assert (root / "watchlist.json").read_text(encoding="utf-8") == "old"      # left in place, not lost
    assert (data / "cache" / "a.json").read_text(encoding="utf-8") == "new a"
    assert (root / "cache" / "a.json").read_text(encoding="utf-8") == "old a"
    assert (data / "cache" / "b.json").read_text(encoding="utf-8") == "old b"  # missing one moved over


def test_nothing_to_move_is_a_no_op(tmp_path: Path) -> None:
    assert migrate_to_data_dir(tmp_path, tmp_path / "data") == []
    assert not (tmp_path / "data").exists()

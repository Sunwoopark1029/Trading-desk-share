"""One-time move of the app's data files from the repo root into data/ (tidy-up 2026-11-21).

Before: positions.json, overlays.json, user_settings.json, watchlist.json and cache/ sat in the
repo root. Now they live in data/. On start the backend moves any that are still in the root.
Nothing is overwritten: a file that already exists in data/ wins and the old copy is left where
it is (and logged). cache/ is merged entry by entry; the old folder is removed only when empty.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

log = logging.getLogger(__name__)

MOVED_FILES = ("positions.json", "overlays.json", "user_settings.json", "watchlist.json")
MOVED_DIRS = ("cache",)


def migrate_to_data_dir(root: Path, data: Path) -> list[str]:
    """Move old root-level data into `data`. Returns the names moved (for the log and tests)."""
    moved: list[str] = []
    for name in MOVED_FILES:
        old, new = root / name, data / name
        if not old.is_file():
            continue
        if new.exists():
            log.warning("data move: %s exists in both places; kept data/%s, old copy left in place", name, name)
            continue
        data.mkdir(parents=True, exist_ok=True)
        os.replace(old, new)   # same drive: an atomic rename
        moved.append(name)
    for name in MOVED_DIRS:
        old, new = root / name, data / name
        if not old.is_dir():
            continue
        new.mkdir(parents=True, exist_ok=True)
        left = False
        for item in old.iterdir():
            target = new / item.name
            if target.exists():
                left = True
                continue
            shutil.move(str(item), str(target))
        if left:
            log.warning("data move: some entries of %s/ already exist in data/%s/; old ones left in place", name, name)
        else:
            old.rmdir()
        moved.append(name + "/")
    if moved:
        log.info("data move: moved %s into %s", ", ".join(moved), data.name)
    return moved

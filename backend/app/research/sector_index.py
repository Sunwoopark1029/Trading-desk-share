"""Stock -> Toss sector map (phase 6 part B, user choice 2026-11-21).

Toss has no "sector of a stock" endpoint, only sector -> stocks. So once a day, and only after a Company view was
opened, the app reads the stock list of every leaf sector (no child sectors) for the US market and keeps:
  members[sector_id] = US symbols in market-cap order (Toss sorts them so), and
  sectors_of[symbol] = the leaf sectors it belongs to (a stock can be in several).
About one read-only Toss call per leaf sector, paced by the client's rate limiter (SECTOR group). Saved to
data/cache/sector_index.json so a restart the same day does not rebuild it. Peers = top N of the deepest sector.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.services import error_info

log = logging.getLogger("desk.sector_index")


@dataclass(frozen=True)
class SectorRef:
    sector_id: str
    name: str
    depth: int


class SectorIndex:
    def __init__(self, svc: Any, path: Path, max_age_s: float, clock: Any = time.time) -> None:
        self.svc = svc
        self.path = path
        self.max_age_s = max_age_s
        self.clock = clock
        self.built_at: float | None = None
        self.members: dict[str, list[str]] = {}
        self.sectors: dict[str, SectorRef] = {}
        self.sectors_of: dict[str, list[str]] = {}
        self.status = "not built"            # not built | building | ok | error
        self.error: str | None = None
        self.progress = (0, 0)
        self._task: asyncio.Task[None] | None = None
        self._failed_at: float | None = None
        self.skipped = 0
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            built = float(raw["built_at"])
            if self.clock() - built >= self.max_age_s:
                return
            self.sectors = {k: SectorRef(k, v["name"], int(v["depth"])) for k, v in raw["sectors"].items()}
            self.members = {k: list(v) for k, v in raw["members"].items()}
            self._reindex()
            self.built_at, self.status = built, "ok"
        except (OSError, ValueError, KeyError, TypeError):
            pass                              # no / old / broken file: built on first use

    def _reindex(self) -> None:
        self.sectors_of = {}
        for sid, syms in self.members.items():
            for s in syms:
                self.sectors_of.setdefault(s, []).append(sid)

    @property
    def fresh(self) -> bool:
        return self.built_at is not None and self.clock() - self.built_at < self.max_age_s

    def ensure(self) -> None:
        """Start a background build when missing or older than max_age (no-op while one runs)."""
        if self.fresh or (self._task is not None and not self._task.done()):
            return
        if self._failed_at is not None and self.clock() - self._failed_at < 600:
            return                                       # a failed build waits 10 min before the next try
        self.status, self.error = "building", None
        self._task = asyncio.create_task(self._build(), name="sector-index")

    async def _build(self) -> None:
        toss = getattr(self.svc, "toss", None)
        if toss is None:
            self.status, self.error = "error", "Toss keys not set in .env"
            self._failed_at = self.clock()       # retried after 10 min like any failed build
            return
        self.status, self.error = "building", None
        try:
            tree = await toss.sectors()
            has_child = {s.parent_sector_id for s in tree if s.parent_sector_id}
            leaves = [s for s in tree if s.sector_id not in has_child]
            sectors = {s.sector_id: SectorRef(s.sector_id, s.name, s.depth) for s in tree}
            members: dict[str, list[str]] = {}
            self.progress = (0, len(leaves))
            skipped = 0
            for i, s in enumerate(leaves):
                try:
                    stocks = await toss.sector_stocks(s.sector_id, "US")
                except Exception as exc:  # noqa: BLE001 - one sector failing does not stop the build
                    skipped += 1
                    log.info("sector %s skipped: %s", s.sector_id, error_info(exc)[0])
                    continue
                members[s.sector_id] = [x.symbol.upper() for x in stocks]
                self.progress = (i + 1, len(leaves))
            if leaves and skipped == len(leaves):
                raise RuntimeError("all sectors failed")
        except Exception as exc:  # noqa: BLE001 - keep the old index; shown as status error
            self.status = "error"
            self.error = "every sector list failed" if isinstance(exc, RuntimeError) else error_info(exc)[1]
            self._failed_at = self.clock()
            log.warning("sector index build failed: %s", self.error)
            return
        self.skipped = skipped
        self.sectors, self.members = sectors, members
        self._reindex()
        self.built_at, self.status = self.clock(), "ok"
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps({"built_at": self.built_at, "members": members,
                                       "sectors": {k: {"name": v.name, "depth": v.depth} for k, v in sectors.items()}},
                                      ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            pass                              # works from memory; rebuilt after a restart

    def sector_for(self, symbol: str) -> SectorRef | None:
        """The deepest leaf sector of a symbol (ties: the one where it ranks highest by market cap)."""
        sids = self.sectors_of.get(symbol.upper(), [])
        if not sids:
            return None
        best = max(sids, key=lambda sid: (self.sectors[sid].depth if sid in self.sectors else -1,
                                          -self.members[sid].index(symbol.upper())))
        return self.sectors.get(best)

    def meta(self) -> dict[str, Any]:
        from app.services import epoch_kst
        return {"status": self.status, "error": self.error, "progress": list(self.progress),
                "built_at": epoch_kst(self.built_at) if self.built_at else None, "sectors": len(self.members),
                "skipped": self.skipped}

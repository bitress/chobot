"""
Who lives on an island right now, and whether a villager move-in would clash with them.

Plots here are SysBot house indexes, 0–9. People-facing labels ("House 1–10") are
added by the website; nothing in this module or the API it backs is 1-based.

Used by the website's /api/islands/<name>/residents and villager inject routes. The
Discord drop bot has its own copy of this logic (bots/cogs/dropbot/actions.py).
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

PLOT_MIN = 0
PLOT_MAX = 9
CACHE_TTL = 15.0

_cache: dict[str, tuple[float, list["Resident"]]] = {}
_lock = threading.Lock()


@dataclass(frozen=True)
class Resident:
    name: str
    plot: int | None  # only set when SysBot reports an explicit house index

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "plot": self.plot}


def clean_key(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def parse_plot(value: Any) -> int | None:
    """A house index from request or SysBot data, or None when absent/invalid. 0 is a real plot."""
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        plot = int(value)
    except (TypeError, ValueError):
        return None
    return plot if PLOT_MIN <= plot <= PLOT_MAX else None


def parse_residents(raw: Any) -> list[Resident]:
    """Residents from a SysBot GET /api/villagers payload; tolerant of plain names or dict entries."""
    entries = raw.get("villagers") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        return []
    residents: list[Resident] = []
    for entry in entries:
        if isinstance(entry, dict):
            name = str(entry.get("name") or entry.get("Name") or entry.get("villager") or "").strip()
            plot = next(
                (p for p in (parse_plot(entry.get(k)) for k in ("plot", "Plot", "house", "index")) if p is not None),
                None,
            )
        else:
            name, plot = str(entry or "").strip(), None
        if name:
            residents.append(Resident(name.title() if name.islower() else name, plot))
    return residents


def get_live_residents(island: str, *, fresh: bool = False) -> list[Resident] | None:
    """
    Residents straight from the island's SysBot, cached for CACHE_TTL seconds.
    None means SysBot couldn't be asked (offline, error); [] means it answered with nobody.
    """
    now = time.time()
    if not fresh:
        with _lock:
            hit = _cache.get(island)
        if hit and now - hit[0] < CACHE_TTL:
            return list(hit[1])
    try:
        from utils.sysbot_api import get_island_sysbot_client

        data = get_island_sysbot_client(island, timeout=3).get_villagers_sync()
    except Exception as exc:  # network, unknown island, bad payload
        logger.debug("Resident lookup failed for %s: %s", island, exc)
        return None
    if not isinstance(data, dict) or not data.get("success", True) or not isinstance(data.get("villagers"), list):
        return None
    residents = parse_residents(data)
    with _lock:
        _cache[island] = (now, residents)
    return list(residents)


def forget(island: str) -> None:
    """Drop the cached residents after a move-in so the next read is live."""
    with _lock:
        _cache.pop(island, None)


@dataclass(frozen=True)
class Clash:
    code: str               # "already_resident" | "plot_occupied" | "duplicate_villager" | "duplicate_plot"
    villager: str
    plot: int | None = None
    occupant: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {"code": self.code, "villager": self.villager, "plot": self.plot, "occupant": self.occupant}


def find_clashes(entries: list[tuple[str, int | None]], residents: list[Resident]) -> tuple[list[Clash], list[Clash]]:
    """
    (blocking, replacements) for a set of (villager name, plot) move-ins.

    blocking: can never work (villager already lives there, same villager or plot twice in one request).
    replacements: would overwrite a known resident; allowed only once the user confirms.
    A resident moving to a plot that another requested villager is vacating isn't modelled; SysBot
    applies the batch in one go, so only the final occupant per plot matters.
    """
    blocking: list[Clash] = []
    replacements: list[Clash] = []
    resident_keys = {clean_key(r.name) for r in residents}
    by_plot = {r.plot: r.name for r in residents if r.plot is not None}
    seen_villagers: set[str] = set()
    seen_plots: set[int] = set()

    for villager, plot in entries:
        key = clean_key(villager)
        if key in seen_villagers:
            blocking.append(Clash("duplicate_villager", villager, plot))
        seen_villagers.add(key)
        if plot is not None:
            if plot in seen_plots:
                blocking.append(Clash("duplicate_plot", villager, plot))
            seen_plots.add(plot)
        if key in resident_keys:
            blocking.append(Clash("already_resident", villager, plot))
            continue
        occupant = by_plot.get(plot) if plot is not None else None
        if occupant and clean_key(occupant) != key:
            replacements.append(Clash("plot_occupied", villager, plot, occupant))
    return blocking, replacements

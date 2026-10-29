"""Process-wide in-memory guards: background tasks, cooldowns, drop locks, the shared HTTP session."""

from __future__ import annotations

import aiohttp
import asyncio
import time
from collections import Counter

from .constants import ACTION_COOLDOWN, WEBHOOK_DEBOUNCE_TTL


# Background tasks need a strong reference or they can be garbage-collected mid-flight.
_BACKGROUND_TASKS: set[asyncio.Task] = set()


def _spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return task


# ─── Small in-memory guards ────────────────────────────────────────────────────

_WEBHOOK_DEBOUNCE: dict[str, float] = {}


_COOLDOWNS: dict[tuple[int, str, str], float] = {}


_ACCESS_CACHE: dict[int, tuple[float, str | None]] = {}


def _debounced(key: str) -> bool:
    """True if `key` fired within the TTL. Prunes expired entries so the dict can't grow forever."""
    now = time.monotonic()
    for k in [k for k, t in _WEBHOOK_DEBOUNCE.items() if now - t > WEBHOOK_DEBOUNCE_TTL]:
        del _WEBHOOK_DEBOUNCE[k]
    if key in _WEBHOOK_DEBOUNCE:
        return True
    _WEBHOOK_DEBOUNCE[key] = now
    return False


def _cooldown_left(user_id: int, island: str, action: str, seconds: float = ACTION_COOLDOWN) -> float:
    """Seconds the user still has to wait; 0.0 (and the cooldown is armed) if they may proceed."""
    now = time.monotonic()
    for k in [k for k, t in _COOLDOWNS.items() if now - t > 60]:
        del _COOLDOWNS[k]
    key = (user_id, island, action)
    last = _COOLDOWNS.get(key)
    if last is not None and now - last < seconds:
        return seconds - (now - last)
    _COOLDOWNS[key] = now
    return 0.0


def _pop_cooldown(user_id: int, island: str, action: str) -> None:
    """Remove an armed cooldown after an unattempted or immediately failed action."""
    _COOLDOWNS.pop((user_id, island, action), None)


# One island bot handles one drop at a time. Serialising submissions per island keeps concurrent
# members from interleaving payloads, and the waiting counter lets us tell them where they are in line.
DROP_SUBMIT_TIMEOUT = 30.0   # a hung island bot must not block everyone queued behind it


MAX_DROP_QUEUE = 8          # refuse new drops once this many are already waiting on one island


_ISLAND_DROP_LOCKS: dict[str, asyncio.Lock] = {}


_ISLAND_DROP_WAITING: Counter[str] = Counter()   # submissions in flight or waiting, per island


def _island_drop_lock(island: str) -> asyncio.Lock:
    lock = _ISLAND_DROP_LOCKS.get(island)
    if lock is None:
        lock = _ISLAND_DROP_LOCKS[island] = asyncio.Lock()
    return lock


def _cooldown_eta_ts(wait: float) -> str:
    """Return a Discord-native relative timestamp that ticks down on its own."""
    return f"<t:{int(time.time() + wait)}:R>"


# ─── Shared HTTP session ───────────────────────────────────────────────────────

_HTTP_SESSION: aiohttp.ClientSession | None = None


def _http() -> aiohttp.ClientSession:
    global _HTTP_SESSION
    if _HTTP_SESSION is None or _HTTP_SESSION.closed:
        _HTTP_SESSION = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8))
    return _HTTP_SESSION


async def _close_http() -> None:
    global _HTTP_SESSION
    if _HTTP_SESSION is not None and not _HTTP_SESSION.closed:
        await _HTTP_SESSION.close()
    _HTTP_SESSION = None


_PANEL_CHANNELS_CACHE: dict[str, tuple[float, list[int]]] = {}

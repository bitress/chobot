"""
Chobot Unified In-Memory Cache
================================
Thread-safe TTL cache used across catalog_map_service, sysbot_api,
nhl_map_parser, and any other module that needs fast, short-lived storage.

Features
--------
- Generic key/value storage with per-entry or store-default TTL
- Thread-safe operations via threading.RLock
- Optional LRU eviction when max_size is set
- Atomic get_or_set() helper
- Prefix-based invalidation (invalidate_prefix)
- Sync and async @cached decorator with custom key generators
- Pre-built module-level stores for domain objects:
    * map_cache         (parsed .nhl island maps, 60s default)
    * telemetry_cache   (SysBot telemetry polling, 5s default)
    * render_cache      (rendered map images / PNGs, 120s default)
    * bot_status_cache  (order bot health & queue, 3s default)
    * catalog_cache     (item/recipe search queries, 300s default)
    * bundles_cache     (bundles API payloads, 300s default)
    * players_cache     (live visitor tracking, 5s default)
"""

from __future__ import annotations

import asyncio
import inspect
import functools
import hashlib
import json
import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple, TypeVar

logger = logging.getLogger("ChobotCache")

T = TypeVar("T")


class CacheEntry:
    __slots__ = ("key", "value", "expires_at", "last_accessed", "hits")

    def __init__(self, key: str, value: Any, ttl_seconds: Optional[float] = None):
        self.key = key
        self.value = value
        now = time.monotonic()
        self.expires_at = (now + ttl_seconds) if (ttl_seconds and ttl_seconds > 0) else None
        self.last_accessed = now
        self.hits = 0

    @property
    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return time.monotonic() >= self.expires_at


class CacheStore:
    """Thread-safe in-memory cache with TTL expiration and optional LRU eviction."""

    def __init__(
        self,
        name: str = "default",
        default_ttl: float = 60.0,
        max_size: Optional[int] = 1000,
    ):
        self.name = name
        self.default_ttl = default_ttl
        self.max_size = max_size
        self._store: Dict[str, CacheEntry] = {}
        self._lock = threading.RLock()
        self._hits = 0
        self._misses = 0

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self._misses += 1
                return default
            if entry.is_expired:
                del self._store[key]
                self._misses += 1
                return default
            entry.last_accessed = time.monotonic()
            entry.hits += 1
            self._hits += 1
            return entry.value

    def set(self, key: str, value: Any, ttl: Optional[float] = None) -> None:
        effective_ttl = self.default_ttl if ttl is None else ttl
        with self._lock:
            if self.max_size and len(self._store) >= self.max_size and key not in self._store:
                self._evict_lru()

            self._store[key] = CacheEntry(key, value, effective_ttl)

    def delete(self, key: str) -> bool:
        with self._lock:
            if key in self._store:
                del self._store[key]
                return True
            return False

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

    def get_or_set(
        self,
        key: str,
        factory: Callable[[], T],
        ttl: Optional[float] = None,
    ) -> T:
        with self._lock:
            val = self.get(key)
            if val is not None:
                return val
            computed = factory()
            self.set(key, computed, ttl=ttl)
            return computed

    async def get_or_set_async(
        self,
        key: str,
        factory_coro: Callable[[], Any],
        ttl: Optional[float] = None,
    ) -> Any:
        with self._lock:
            val = self.get(key)
            if val is not None:
                return val

        computed = await factory_coro()

        with self._lock:
            self.set(key, computed, ttl=ttl)
            return computed

    def invalidate_prefix(self, prefix: str) -> int:
        with self._lock:
            matching = [k for k in self._store if k.startswith(prefix)]
            for k in matching:
                del self._store[k]
            return len(matching)

    def cleanup_expired(self) -> int:
        with self._lock:
            now = time.monotonic()
            expired = [
                k for k, v in self._store.items()
                if v.expires_at is not None and now >= v.expires_at
            ]
            for k in expired:
                del self._store[k]
            return len(expired)

    def _evict_lru(self) -> None:
        if self.cleanup_expired() > 0 and len(self._store) < (self.max_size or 0):
            return

        if not self._store:
            return
        lru_key = min(self._store.keys(), key=lambda k: self._store[k].last_accessed)
        del self._store[lru_key]

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            total_reqs = self._hits + self._misses
            hit_rate = (self._hits / total_reqs) if total_reqs > 0 else 0.0
            return {
                "name": self.name,
                "size": len(self._store),
                "max_size": self.max_size,
                "default_ttl": self.default_ttl,
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": round(hit_rate, 4),
            }


# -----------------------------------------------------------------------------
# Pre-configured Domain Stores
# -----------------------------------------------------------------------------

map_cache = CacheStore(name="island_maps", default_ttl=60.0, max_size=100)
telemetry_cache = CacheStore(name="telemetry", default_ttl=5.0, max_size=200)
render_cache = CacheStore(name="rendered_images", default_ttl=120.0, max_size=150)
bot_status_cache = CacheStore(name="bot_status", default_ttl=3.0, max_size=50)
catalog_cache = CacheStore(name="catalog_search", default_ttl=300.0, max_size=1000)
bundles_cache = CacheStore(name="bundles", default_ttl=300.0, max_size=20)
players_cache = CacheStore(name="live_players", default_ttl=5.0, max_size=100)

_ALL_STORES = [
    map_cache,
    telemetry_cache,
    render_cache,
    bot_status_cache,
    catalog_cache,
    bundles_cache,
    players_cache,
]


def get_all_cache_stats() -> Dict[str, Any]:
    """Return aggregated metrics across all active caches for diagnostics."""
    return {store.name: store.stats() for store in _ALL_STORES}


def clear_all_caches() -> None:
    """Flush all domain caches (useful during reload or configuration changes)."""
    for store in _ALL_STORES:
        store.clear()
    logger.info("[ChobotCache] All caches cleared.")


# -----------------------------------------------------------------------------
# Decorator: @cached
# -----------------------------------------------------------------------------

def _make_cache_key(prefix: str, args: tuple, kwargs: dict) -> str:
    try:
        key_raw = f"{prefix}:{args}:{sorted(kwargs.items())}"
        if len(key_raw) > 128:
            return f"{prefix}:{hashlib.md5(key_raw.encode('utf-8')).hexdigest()}"
        return key_raw
    except Exception:
        return f"{prefix}:{hash(args)}:{hash(frozenset(kwargs.keys()))}"


def cached(
    store: Optional[CacheStore] = None,
    ttl: Optional[float] = None,
    key_prefix: Optional[str] = None,
):
    """
    Function decorator for transparent caching.
    Supports both synchronous and asynchronous functions.
    """
    target_store = store or catalog_cache

    def decorator(fn: Callable):
        prefix = key_prefix or f"{fn.__module__}.{fn.__name__}"

        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def async_wrapper(*args, **kwargs):
                cache_key = _make_cache_key(prefix, args, kwargs)
                val = target_store.get(cache_key)
                if val is not None:
                    return val

                result = await fn(*args, **kwargs)
                target_store.set(cache_key, result, ttl=ttl)
                return result

            return async_wrapper
        else:
            @functools.wraps(fn)
            def sync_wrapper(*args, **kwargs):
                cache_key = _make_cache_key(prefix, args, kwargs)
                val = target_store.get(cache_key)
                if val is not None:
                    return val

                result = fn(*args, **kwargs)
                target_store.set(cache_key, result, ttl=ttl)
                return result

            return sync_wrapper

    return decorator

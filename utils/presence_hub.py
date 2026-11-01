"""In-process pub/sub hub that feeds the Server-Sent Events presence stream.

The Flask API runs as a single threaded process, so a plain thread-safe registry
is enough: each open ``/api/presence/stream`` connection owns a bounded queue,
and publishers drop events onto the queues of whoever should see them.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from typing import Any, Iterable

MAX_SUBSCRIBERS = 400
QUEUE_SIZE = 32
# Roster/radar fan-out is coalesced so a burst of joins costs one broadcast, not N.
BROADCAST_MIN_INTERVAL = 2.0


class Subscriber:
    __slots__ = ("keys", "queue", "created_at")

    def __init__(self, keys: Iterable[str]):
        # Lower-cased identifiers this connection answers to (session ids, usernames).
        self.keys = frozenset(k.strip().lower() for k in keys if k and k.strip())
        self.queue: "queue.Queue[tuple[str, dict]]" = queue.Queue(maxsize=QUEUE_SIZE)
        self.created_at = time.time()

    def offer(self, event: str, data: dict) -> bool:
        try:
            self.queue.put_nowait((event, data))
            return True
        except queue.Full:
            # A reader that cannot keep up is dropped from the fan-out rather than blocking publishers.
            return False


class PresenceHub:
    def __init__(self, max_subscribers: int = MAX_SUBSCRIBERS):
        self._lock = threading.Lock()
        self._subscribers: set[Subscriber] = set()
        self._max = max_subscribers
        self._last_broadcast = 0.0
        self._last_radar_json = ""
        self._pending_timer: threading.Timer | None = None

    # ── subscription ────────────────────────────────────────────────────────
    def subscribe(self, keys: Iterable[str]) -> Subscriber | None:
        sub = Subscriber(keys)
        with self._lock:
            if len(self._subscribers) >= self._max:
                return None
            self._subscribers.add(sub)
        return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        with self._lock:
            self._subscribers.discard(sub)

    def count_for(self, key: str) -> int:
        key = (key or "").strip().lower()
        with self._lock:
            return sum(1 for s in self._subscribers if key in s.keys)

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    # ── publishing ──────────────────────────────────────────────────────────
    def publish_to(self, keys: Iterable[str], event: str, data: dict) -> int:
        """Send to every connection answering to any of ``keys``. Returns how many got it."""
        wanted = {k.strip().lower() for k in keys if k and k.strip()}
        if not wanted:
            return 0
        with self._lock:
            targets = [s for s in self._subscribers if s.keys & wanted]
        return sum(1 for s in targets if s.offer(event, data))

    def publish_all(self, event: str, data: dict) -> int:
        with self._lock:
            targets = list(self._subscribers)
        return sum(1 for s in targets if s.offer(event, data))

    def broadcast_radar(self, build_payload, *, force: bool = False) -> None:
        """Push a fresh radar snapshot to everyone, at most once per BROADCAST_MIN_INTERVAL.

        ``build_payload`` is called lazily so nothing is computed when nobody is listening.
        Calls inside the interval schedule one trailing broadcast instead of being lost.
        """
        if self.subscriber_count == 0:
            return
        now = time.time()
        with self._lock:
            wait = self._last_broadcast + BROADCAST_MIN_INTERVAL - now
            if wait > 0 and not force:
                if self._pending_timer is None:
                    timer = threading.Timer(wait, self._flush, args=(build_payload,))
                    timer.daemon = True
                    self._pending_timer = timer
                    timer.start()
                return
            self._last_broadcast = now
        self._send_radar(build_payload)

    def _flush(self, build_payload) -> None:
        with self._lock:
            self._pending_timer = None
            self._last_broadcast = time.time()
        self._send_radar(build_payload)

    def _send_radar(self, build_payload) -> None:
        try:
            payload = build_payload()
        except Exception:
            return
        encoded = json.dumps(payload, sort_keys=True, default=str)
        with self._lock:
            # Skip when nothing a listener could see has changed.
            if encoded == self._last_radar_json:
                return
            self._last_radar_json = encoded
        self.publish_all("radar", payload)


hub = PresenceHub()


def format_sse(event: str, data: Any, event_id: str | None = None) -> str:
    """Serialise one SSE frame."""
    lines = []
    if event_id:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event}")
    lines.append(f"data: {json.dumps(data, separators=(',', ':'), default=str)}")
    return "\n".join(lines) + "\n\n"

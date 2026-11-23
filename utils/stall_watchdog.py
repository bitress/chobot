"""Process-wide stall detector.

Several API requests sometimes finish together after 1-3 s even though the DB pool is idle: the
whole process paused. That is a thread holding the GIL (heavy pure-Python work) or a long garbage
collection. This watchdog wakes every TICK seconds; when it wakes STALL_SECONDS late it logs the
stack of every busy thread, and the GC hook logs any collection slower than GC_SLOW_SECONDS.
The log line names the culprit, which code reading alone could not.
"""

from __future__ import annotations

import gc
import logging
import os
import sys
import threading
import time
import traceback

logger = logging.getLogger("StallWatchdog")

TICK = 0.1
STALL_SECONDS = float(os.getenv("STALL_WATCHDOG_SECONDS", "1.0") or 1.0)
GC_SLOW_SECONDS = 0.2
# Don't flood the log if the process is stalling continuously.
MIN_SECONDS_BETWEEN_DUMPS = 30.0

# Innermost frames that mean "this thread is waiting, not working".
_IDLE_FUNCS = {
    "wait", "select", "poll", "_poll", "accept", "recv", "recv_into", "readinto", "read",
    "sleep", "get", "_wait_for_tstate_lock", "serve_forever", "run_forever", "_run_once",
    "_worker", "handle_request", "acquire", "join",
}

_started = False
_gc_start: dict[int, float] = {}


def _busy_stacks(skip_ident: int) -> list[str]:
    names = {t.ident: t.name for t in threading.enumerate()}
    out = []
    for ident, frame in sys._current_frames().items():
        if ident == skip_ident or frame.f_code.co_name in _IDLE_FUNCS:
            continue
        stack = "".join(traceback.format_stack(frame, limit=12))
        out.append(f"--- thread {names.get(ident, ident)} ---\n{stack}")
    return out


def _gc_callback(phase: str, info: dict) -> None:
    gen = info.get("generation", -1)
    if phase == "start":
        _gc_start[gen] = time.perf_counter()
        return
    started = _gc_start.pop(gen, None)
    if started is None:
        return
    took = time.perf_counter() - started
    if took >= GC_SLOW_SECONDS:
        logger.warning(
            "[STALL] Garbage collection gen%s took %.0f ms (collected=%s)", gen, took * 1000, info.get("collected"),
        )


def _run() -> None:
    me = threading.get_ident()
    last_dump = 0.0
    expected = time.perf_counter() + TICK
    while True:
        time.sleep(TICK)
        now = time.perf_counter()
        lag = now - expected
        expected = now + TICK
        if lag < STALL_SECONDS or now - last_dump < MIN_SECONDS_BETWEEN_DUMPS:
            continue
        last_dump = now
        stacks = _busy_stacks(me)
        logger.warning(
            "[STALL] Process paused %.0f ms (threads=%d). Busy threads at wake-up:\n%s",
            lag * 1000, threading.active_count(), "\n".join(stacks) or "(none busy: likely GC or OS-level pause)",
        )


def start() -> None:
    """Start once per process; safe to call repeatedly."""
    global _started
    if _started or os.getenv("STALL_WATCHDOG", "1").strip().lower() in ("0", "false", "no", "off"):
        return
    _started = True
    gc.callbacks.append(_gc_callback)
    threading.Thread(target=_run, name="StallWatchdog", daemon=True).start()
    logger.info("[STALL] Watchdog on: logging pauses over %.1f s and GC over %.0f ms", STALL_SECONDS, GC_SLOW_SECONDS * 1000)

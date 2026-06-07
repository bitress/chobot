"""Small synchronous Discord REST helper with 429 retry handling."""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("DiscordHTTP")

DEFAULT_USER_AGENT = "DiscordBot (https://chopaeng.com, 1.0)"
MAX_RETRIES = 3
MAX_RETRY_AFTER_SECONDS = 30.0
_GLOBAL_MIN_INTERVAL_SECONDS = 0.25
_lock = threading.Lock()
_next_request_at = 0.0


@dataclass
class DiscordHTTPResponse:
    status: int
    body: str
    headers: Any

    def json(self) -> Any:
        return json.loads(self.body) if self.body else None


def _sleep_for_global_spacing() -> None:
    global _next_request_at
    with _lock:
        now = time.monotonic()
        wait = max(_next_request_at - now, 0.0)
        _next_request_at = max(now, _next_request_at) + _GLOBAL_MIN_INTERVAL_SECONDS
    if wait > 0:
        time.sleep(wait)


def _retry_after_from_headers_or_body(headers: Any, body: str) -> float | None:
    for key in ("Retry-After", "retry-after", "X-RateLimit-Reset-After", "x-ratelimit-reset-after"):
        value = headers.get(key) if headers else None
        if value:
            try:
                return float(value)
            except (TypeError, ValueError):
                pass
    if body:
        try:
            payload = json.loads(body)
            retry_after = payload.get("retry_after")
            if retry_after is not None:
                return float(retry_after)
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    return None


def request(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
    timeout: int = 10,
    max_retries: int = MAX_RETRIES,
) -> DiscordHTTPResponse:
    """Run a Discord HTTP request and wait/retry on 429 responses."""
    request_headers = {"User-Agent": DEFAULT_USER_AGENT}
    request_headers.update(headers or {})

    last_error: urllib.error.HTTPError | None = None
    for attempt in range(max_retries + 1):
        _sleep_for_global_spacing()
        req = urllib.request.Request(url, data=data, headers=request_headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode(errors="replace")
                return DiscordHTTPResponse(resp.status, body, resp.headers)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")
            if exc.code != 429 or attempt >= max_retries:
                raise
            last_error = exc
            retry_after = _retry_after_from_headers_or_body(exc.headers, body)
            wait = min(max(retry_after or 1.0, 0.25), MAX_RETRY_AFTER_SECONDS)
            logger.warning("Discord HTTP 429 for %s; retrying in %.2fs", url, wait)
            time.sleep(wait)

    if last_error:
        raise last_error
    raise RuntimeError("Discord HTTP request failed without an exception")


def json_request(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    payload: dict | None = None,
    timeout: int = 10,
    max_retries: int = MAX_RETRIES,
) -> Any:
    data = json.dumps(payload).encode() if payload is not None else None
    request_headers = dict(headers or {})
    if payload is not None:
        request_headers.setdefault("Content-Type", "application/json")
    response = request(
        url,
        method=method,
        headers=request_headers,
        data=data,
        timeout=timeout,
        max_retries=max_retries,
    )
    return response.json()

"""YouTube live status for the website, without an API key.

YouTube's https://www.youtube.com/channel/<id>/live page redirects to the channel's current live
stream when there is one. Live means both:
  - the canonical link is a watch?v= video (not the channel itself), and
  - that video's player data (videoDetails) says "isLive":true and belongs to our channel.
A scheduled stream that hasn't started also gets a watch page, but with isLive absent.

This reads YouTube's page markup, which isn't an official API and can change. If the markers can't be
found the result is "unknown" (stale), never a confident "offline". The page is about 1.3 MB, so
results are cached for a couple of minutes and shared by every visitor.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time

import requests

from utils.config import Config

logger = logging.getLogger("YouTubeLive")

STATUS_TTL_SECONDS = 120.0
ERROR_TTL_SECONDS = 30.0
REQUEST_TIMEOUT_SECONDS = 10

# A normal desktop browser; YouTube serves a reduced page to unknown clients.
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

_CANONICAL = re.compile(r'<link rel="canonical" href="([^"]+)"')
# Start of the main video's player data: "videoDetails":{"videoId":"…","title":"…", …
_VIDEO_DETAILS = re.compile(r'"videoDetails":\{"videoId":"([\w-]{11})","title":"((?:[^"\\]|\\.)*)"')
_WATCHING_NOW = re.compile(r'"originalViewCount":"(\d+)"')

# Held while fetching from YouTube, so only one fetch is ever in flight.
_lock = threading.Lock()
_status_cache: dict[str, tuple[float, dict]] = {}
# Background refreshes of an expired status, one per channel at a time.
_refreshing: dict[str, threading.Thread] = {}
_refreshing_guard = threading.Lock()


class YouTubePageChanged(Exception):
    """The page didn't look like either a channel page or a watch page; treat the status as unknown."""


def _reset_for_tests() -> None:
    with _lock:
        _status_cache.clear()
    with _refreshing_guard:
        _refreshing.clear()


def _offline(channel_id: str) -> dict:
    return {"live": False, "channel_id": channel_id, "url": f"https://www.youtube.com/channel/{channel_id}"}


def _flat_fields_after(html: str, start: int) -> str:
    """The rest of a JSON object's top-level scalar fields, up to its first nested object or its end.

    videoDetails lists isLive and channelId before any nested object (thumbnail etc.), so stopping at the
    first unquoted brace keeps the check inside this object instead of whatever follows it in the page.
    Braces inside quoted strings (titles, descriptions) are skipped.
    """
    in_string = escaped = False
    for i in range(start, min(len(html), start + 20000)):
        ch = html[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch in "{}":
            return html[start:i]
    return html[start:start + 20000]


def parse_live_page(html: str, channel_id: str) -> dict:
    """Interpret a /channel/<id>/live page. Raises YouTubePageChanged if it can't tell."""
    canonical = _CANONICAL.search(html)
    if not canonical:
        raise YouTubePageChanged("no canonical link (consent page or changed markup?)")

    href = canonical.group(1)
    if "/watch?v=" not in href:
        return _offline(channel_id)  # /live fell back to the channel page: nothing is live

    details = _VIDEO_DETAILS.search(html)
    if not details:
        raise YouTubePageChanged("watch page without videoDetails")

    video_id = details.group(1)
    fields = _flat_fields_after(html, details.end())
    is_ours = f'"channelId":"{channel_id}"' in fields
    if not is_ours or '"isLive":true' not in fields:
        return _offline(channel_id)  # scheduled/upcoming stream, a past stream, or someone else's video

    try:
        title = json.loads(f'"{details.group(2)}"')
    except ValueError:
        title = details.group(2)
    watching = _WATCHING_NOW.search(html)
    return {
        **_offline(channel_id),
        "live": True,
        "video_id": video_id,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "title": title,
        "viewers": int(watching.group(1)) if watching else None,
        "thumbnail": f"https://i.ytimg.com/vi/{video_id}/hqdefault_live.jpg",
    }


def _fetch_status(channel_id: str) -> dict:
    res = requests.get(
        f"https://www.youtube.com/channel/{channel_id}/live",
        headers=_HEADERS,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    res.raise_for_status()
    return parse_live_page(res.text, channel_id)


def get_live_status(channel_id: str | None = None) -> dict:
    """Live status for a YouTube channel (default: Config.YOUTUBE_CHANNEL_ID), cached ~2 minutes.

    Never raises. On failure the last known status is returned with "stale": True, or an
    offline-but-stale result if there is none.

    Only the first call after startup waits for YouTube; after that an expired status is returned
    at once while a background thread refreshes it (see twitch_api.get_stream_status).
    """
    channel_id = (channel_id or Config.YOUTUBE_CHANNEL_ID or "").strip()
    if not channel_id:
        return {"live": False, "channel_id": "", "url": "", "stale": True}

    cached = _status_cache.get(channel_id)
    if cached:
        if cached[0] <= time.time():
            _start_background_refresh(channel_id)
        return cached[1]

    with _lock:  # cold cache: one caller fetches, concurrent callers wait for its answer
        cached = _status_cache.get(channel_id)
        if cached:
            return cached[1]
        return _refresh_locked(channel_id)


def _refresh_locked(channel_id: str) -> dict:
    """Fetch and store the status; caller holds _lock."""
    now = time.time()
    cached = _status_cache.get(channel_id)
    try:
        status = _fetch_status(channel_id)
        ttl = STATUS_TTL_SECONDS
    except Exception as exc:  # network errors, consent/bot pages, markup changes
        logger.warning("YouTube live status for %s unavailable: %s", channel_id, exc)
        previous = cached[1] if cached else None
        status = {**previous, "stale": True} if previous else {**_offline(channel_id), "stale": True}
        ttl = ERROR_TTL_SECONDS

    _status_cache[channel_id] = (now + ttl, status)
    return status


def _start_background_refresh(channel_id: str) -> None:
    with _refreshing_guard:
        running = _refreshing.get(channel_id)
        if running and running.is_alive():
            return
        thread = threading.Thread(target=_background_refresh, args=(channel_id,), name="YouTubeLiveStatus", daemon=True)
        _refreshing[channel_id] = thread
        thread.start()


def _background_refresh(channel_id: str) -> None:
    with _lock:
        cached = _status_cache.get(channel_id)
        if cached and cached[0] > time.time():
            return  # someone else refreshed it meanwhile
        _refresh_locked(channel_id)

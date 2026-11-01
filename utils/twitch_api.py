"""Twitch live-stream status for the website.

Uses Twitch's Helix "Get Streams" endpoint with an app access token (client-credentials flow), so no
user login is involved. Results are cached briefly and shared by every visitor: the website polls this,
Twitch is called at most about once a minute per channel.
"""

from __future__ import annotations

import logging
import threading
import time

import requests

from utils.config import Config

logger = logging.getLogger("TwitchAPI")

TOKEN_URL = "https://id.twitch.tv/oauth2/token"
STREAMS_URL = "https://api.twitch.tv/helix/streams"

STATUS_TTL_SECONDS = 60.0
# After a failure, retry sooner than the normal TTL but don't hammer Twitch.
ERROR_TTL_SECONDS = 15.0
REQUEST_TIMEOUT_SECONDS = 5
# Renew the app token a little before Twitch says it expires.
TOKEN_EXPIRY_MARGIN_SECONDS = 300

# One lock for token + status: concurrent requests wait for a single in-flight Twitch call
# instead of each firing their own when the cache expires.
_lock = threading.Lock()
_token: dict = {"value": None, "expires_at": 0.0}
_status_cache: dict[str, tuple[float, dict]] = {}


def _reset_for_tests() -> None:
    with _lock:
        _token.update(value=None, expires_at=0.0)
        _status_cache.clear()


def _is_configured() -> bool:
    return bool(Config.TWITCH_CLIENT_ID and Config.TWITCH_CLIENT_SECRET)


def _app_token(force_refresh: bool = False) -> str:
    now = time.time()
    if not force_refresh and _token["value"] and now < _token["expires_at"]:
        return _token["value"]

    res = requests.post(
        TOKEN_URL,
        data={
            "client_id": Config.TWITCH_CLIENT_ID,
            "client_secret": Config.TWITCH_CLIENT_SECRET,
            "grant_type": "client_credentials",
        },
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    res.raise_for_status()
    body = res.json()
    _token["value"] = body["access_token"]
    _token["expires_at"] = now + max(0, int(body.get("expires_in", 3600)) - TOKEN_EXPIRY_MARGIN_SECONDS)
    return _token["value"]


def _get_streams(login: str, token: str) -> requests.Response:
    return requests.get(
        STREAMS_URL,
        params={"user_login": login},
        headers={"Client-Id": Config.TWITCH_CLIENT_ID, "Authorization": f"Bearer {token}"},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )


def _offline(login: str) -> dict:
    return {"live": False, "channel": login, "url": f"https://www.twitch.tv/{login}"}


def _fetch_status(login: str) -> dict:
    res = _get_streams(login, _app_token())
    if res.status_code == 401:
        # Token revoked or expired early: get a fresh one and retry once.
        res = _get_streams(login, _app_token(force_refresh=True))
    res.raise_for_status()

    streams = res.json().get("data") or []
    if not streams or streams[0].get("type") != "live":
        return _offline(login)

    stream = streams[0]
    thumbnail = (stream.get("thumbnail_url") or "").replace("{width}", "640").replace("{height}", "360")
    return {
        **_offline(login),
        "live": True,
        "title": stream.get("title") or "",
        "game": stream.get("game_name") or "",
        "viewers": int(stream.get("viewer_count") or 0),
        "started_at": stream.get("started_at"),
        "thumbnail": thumbnail or None,
    }


def get_stream_status(login: str | None = None) -> dict:
    """Live status for a Twitch channel (default: Config.TWITCH_CHANNEL), cached for about a minute.

    Never raises. If Twitch can't be reached, the last known status is returned with "stale": True,
    or "offline" if there is none, so a Twitch outage can't break the page.
    """
    login = (login or Config.TWITCH_CHANNEL or "chopaeng").strip().lower()
    if not _is_configured():
        return _offline(login)

    now = time.time()
    with _lock:
        cached = _status_cache.get(login)
        if cached and cached[0] > now:
            return cached[1]

        try:
            status = _fetch_status(login)
            ttl = STATUS_TTL_SECONDS
        except Exception as exc:  # network errors, bad credentials, unexpected payloads
            logger.warning("Twitch stream status for %s unavailable: %s", login, exc)
            previous = cached[1] if cached else None
            status = {**previous, "stale": True} if previous else {**_offline(login), "stale": True}
            ttl = ERROR_TTL_SECONDS

        _status_cache[login] = (now + ttl, status)
        return status

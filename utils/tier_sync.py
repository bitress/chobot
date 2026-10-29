"""
Re-checks subscription tiers of linked Patreon and Twitch accounts.

Linked tiers are otherwise only checked when a member links the account, so a cancelled
pledge kept opening sub islands until LINKED_TIER_MAX_AGE_DAYS ran out. These syncs read
the creator's side (every patron / subscriber at once), so no member tokens are stored:

- Patreon: the campaign's members, with the creator token (PATREON_TOKEN), plus the
  members webhook (PATREON_WEBHOOK_SECRET) for changes between syncs.
- Twitch: the channel's subscribers, with a broadcaster token that has the
  channel:read:subscriptions scope (TWITCH_SUBS_REFRESH_TOKEN). Skipped when unset.

Each sync stamps the link's updated_at, which is when its tiers were last confirmed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import urllib.parse
import urllib.request
from typing import Any, Iterable

from utils.config import Config
from utils.database import connect_db

logger = logging.getLogger("TierSync")

PATREON_MEMBERS_URL = (
    "https://www.patreon.com/api/oauth2/v2/campaigns/{campaign}/members"
    "?include=user,currently_entitled_tiers&fields%5Bmember%5D=patron_status"
    "&fields%5Btier%5D=title&page%5Bcount%5D=1000"
)


def patreon_tier_roles(titles: Iterable[str]) -> list[str]:
    """Tier names for Patreon tier titles (also used when a member links Patreon)."""
    roles: list[str] = []
    for title in titles:
        t = (title or "").lower()
        if "chosoup" in t:
            roles.append("ChoSoup")
        elif "chocolate" in t:
            roles.append("ChoColate")
        elif "chofries" in t:
            roles.append("ChoFries")
    return list(dict.fromkeys(roles))


def _get_json(url: str, headers: dict[str, str]) -> dict:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _linked_ids(conn, provider: str) -> list[str]:
    rows = conn.execute(
        "SELECT provider_user_id FROM user_provider_links WHERE provider = ?", (provider,)
    ).fetchall()
    return [str(r[0]) for r in rows if r[0]]


def _set_link_tiers(conn, provider: str, provider_user_id: str, roles: list[str]) -> None:
    from utils.unified_auth import expand_roles

    conn.execute(
        "UPDATE user_provider_links SET roles_json = ?, sub_tier = ?, updated_at = ? "
        "WHERE provider = ? AND provider_user_id = ?",
        (
            json.dumps(expand_roles(roles), separators=(",", ":")),
            roles[0] if roles else "",
            int(time.time()),
            provider,
            str(provider_user_id),
        ),
    )


def _member_tiers(member: dict, tier_titles: dict[str, str]) -> tuple[str | None, list[str]]:
    """(Patreon user ID, tier names) for one campaign member resource."""
    rel = member.get("relationships") or {}
    user_id = ((rel.get("user") or {}).get("data") or {}).get("id")
    entitled = (rel.get("currently_entitled_tiers") or {}).get("data") or []
    titles = [tier_titles.get(str(t.get("id")), "") for t in entitled]
    return (str(user_id) if user_id else None), patreon_tier_roles(titles)


def _tier_titles(included: list[dict] | None) -> dict[str, str]:
    return {
        str(inc.get("id")): (inc.get("attributes") or {}).get("title") or ""
        for inc in included or []
        if inc.get("type") == "tier"
    }


def sync_patreon() -> dict[str, Any]:
    """Refresh every linked Patreon account's tiers from the campaign's member list."""
    if not (Config.PATREON_TOKEN and Config.PATREON_CAMPAIGN_ID):
        return {"provider": "patreon", "skipped": "PATREON_TOKEN / PATREON_CAMPAIGN_ID not set"}

    with connect_db() as conn:
        linked = _linked_ids(conn, "patreon")
    if not linked:
        return {"provider": "patreon", "linked": 0}

    # Read every page first: a partial list would wipe the tiers of patrons on missing pages
    headers = {"Authorization": f"Bearer {Config.PATREON_TOKEN}"}
    tiers_by_user: dict[str, list[str]] = {}
    url: str | None = PATREON_MEMBERS_URL.format(campaign=Config.PATREON_CAMPAIGN_ID)
    while url:
        page = _get_json(url, headers)
        titles = _tier_titles(page.get("included"))
        for member in page.get("data") or []:
            user_id, roles = _member_tiers(member, titles)
            if user_id:
                tiers_by_user[user_id] = sorted(set(tiers_by_user.get(user_id, [])) | set(roles))
        url = (page.get("links") or {}).get("next")

    changed = 0
    with connect_db() as conn:
        for pid in linked:
            roles = tiers_by_user.get(pid, [])
            if not roles:
                changed += 1 if _had_tiers(conn, "patreon", pid) else 0
            _set_link_tiers(conn, "patreon", pid, roles)
        conn.commit()
    logger.info("[TierSync] Patreon: %s linked accounts checked, %s lost their tiers", len(linked), changed)
    return {"provider": "patreon", "linked": len(linked), "lost_tiers": changed}


def _had_tiers(conn, provider: str, provider_user_id: str) -> bool:
    row = conn.execute(
        "SELECT sub_tier FROM user_provider_links WHERE provider = ? AND provider_user_id = ?",
        (provider, provider_user_id),
    ).fetchone()
    return bool(row and row[0])


def verify_patreon_webhook(body: bytes, signature: str | None) -> bool:
    """Patreon signs webhook bodies with HMAC-MD5 of the webhook secret."""
    secret = getattr(Config, "PATREON_WEBHOOK_SECRET", "") or ""
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.md5).hexdigest()
    return hmac.compare_digest(expected, signature.strip().lower())


def apply_patreon_webhook(payload: dict) -> str | None:
    """Update one linked account from a members:* webhook. Returns the Patreon user ID."""
    member = payload.get("data") or {}
    user_id, roles = _member_tiers(member, _tier_titles(payload.get("included")))
    if not user_id:
        return None
    with connect_db() as conn:
        _set_link_tiers(conn, "patreon", user_id, roles)
        conn.commit()
    return user_id


# ── Twitch ──────────────────────────────────────────────────────────────────

_twitch_refresh_token: str | None = None


def _twitch_access_token() -> str | None:
    """Exchange the broadcaster's refresh token for an access token."""
    global _twitch_refresh_token
    refresh = _twitch_refresh_token or getattr(Config, "TWITCH_SUBS_REFRESH_TOKEN", "")
    if not (refresh and Config.TWITCH_CLIENT_ID and Config.TWITCH_CLIENT_SECRET):
        return None
    req = urllib.request.Request(
        "https://id.twitch.tv/oauth2/token",
        data=urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "client_id": Config.TWITCH_CLIENT_ID,
            "client_secret": Config.TWITCH_CLIENT_SECRET,
        }).encode(),
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    new_refresh = data.get("refresh_token")
    if new_refresh and new_refresh != refresh:
        _twitch_refresh_token = new_refresh
        logger.warning("[TierSync] Twitch issued a new refresh token: update TWITCH_SUBS_REFRESH_TOKEN before the next restart")
    return data.get("access_token")


def sync_twitch() -> dict[str, Any]:
    """Refresh every linked Twitch account from the channel's subscriber list."""
    if not getattr(Config, "TWITCH_SUBS_REFRESH_TOKEN", "") or not Config.TWITCH_BROADCASTER_ID:
        return {"provider": "twitch", "skipped": "TWITCH_SUBS_REFRESH_TOKEN not set"}

    with connect_db() as conn:
        linked = _linked_ids(conn, "twitch")
    if not linked:
        return {"provider": "twitch", "linked": 0}

    token = _twitch_access_token()
    if not token:
        return {"provider": "twitch", "skipped": "could not get a broadcaster access token"}
    headers = {"Authorization": f"Bearer {token}", "Client-Id": Config.TWITCH_CLIENT_ID}
    subscribers: set[str] = set()
    cursor: str | None = None
    while True:
        url = f"https://api.twitch.tv/helix/subscriptions?broadcaster_id={Config.TWITCH_BROADCASTER_ID}&first=100"
        if cursor:
            url += f"&after={urllib.parse.quote(cursor)}"
        page = _get_json(url, headers)
        subscribers.update(str(s.get("user_id")) for s in page.get("data") or [] if s.get("user_id"))
        cursor = (page.get("pagination") or {}).get("cursor")
        if not cursor or not page.get("data"):
            break

    with connect_db() as conn:
        for tid in linked:
            _set_link_tiers(conn, "twitch", tid, ["ChoTato Club"] if tid in subscribers else [])
        conn.commit()
    lost = sum(1 for tid in linked if tid not in subscribers)
    logger.info("[TierSync] Twitch: %s linked accounts checked, %s not subscribed", len(linked), lost)
    return {"provider": "twitch", "linked": len(linked), "not_subscribed": lost}


def sync_linked_tiers() -> list[dict[str, Any]]:
    """Run every provider sync; one failing provider does not stop the others."""
    results = []
    for sync in (sync_patreon, sync_twitch):
        try:
            results.append(sync())
        except Exception as exc:
            logger.warning("[TierSync] %s failed: %s", sync.__name__, exc)
            results.append({"provider": sync.__name__.removeprefix("sync_"), "error": str(exc)})
    return results

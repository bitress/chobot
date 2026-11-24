"""Read-only Patreon API v2 client that runs on the creator access token alone.

Only ``PATREON_TOKEN`` is required. The campaign ID comes from ``PATREON_CAMPAIGN_ID`` when set,
otherwise it is looked up from the token. No OAuth flow, no patron logins.

    from utils.patreon import patreon

    patreon.campaign()        # page info + patron count
    patreon.tiers()           # tier names, prices, patron counts, Discord role IDs
    patreon.members()         # every member, flattened (slow: ~9k members, ~1-3 min; cached 15 min)
    patreon.member_by_discord_id("1234")
    patreon.posts(limit=10)   # newest first
    patreon.post("98765")
    patreon.webhooks()

Results are cached in memory (see ``ttl`` on each method). Pass ``fresh=True`` to skip the cache.
Errors raise ``PatreonError``. Member data holds names and emails: never return it from a public route.

Run ``python -m utils.patreon`` for a summary that prints counts only, no personal data.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterator
from typing import Any

import requests

from utils.config import Config

logger = logging.getLogger("Patreon")

API_BASE = "https://www.patreon.com/api/oauth2/v2"
TIMEOUT = 20
MAX_PAGES = 100  # 100 pages x 1000 members is far more than any campaign we have

CAMPAIGN_FIELDS = (
    "created_at,creation_name,image_url,is_monthly,is_nsfw,patron_count,pledge_url,"
    "published_at,summary,url,vanity"
)
TIER_FIELDS = (
    "amount_cents,created_at,description,discord_role_ids,edited_at,image_url,patron_count,"
    "published,published_at,title,url"
)
BENEFIT_FIELDS = "title,description,benefit_type,rule_type,is_deleted,deliverables_due_today_count"
MEMBER_FIELDS = (
    "campaign_lifetime_support_cents,currently_entitled_amount_cents,email,full_name,is_follower,"
    "is_free_trial,is_gifted,last_charge_date,last_charge_status,lifetime_support_cents,"
    "next_charge_date,note,patron_status,pledge_cadence,pledge_relationship_start,will_pay_amount_cents"
)
USER_FIELDS = "full_name,image_url,social_connections,url,vanity"
# ``tiers`` = IDs of the tiers allowed to read a patron-only post (the Free tier counts as one).
POST_FIELDS = "title,content,published_at,url,is_public,is_paid,embed_data,embed_url,tiers"
WEBHOOK_FIELDS = "last_attempted_at,num_consecutive_times_failed,paused,triggers,uri"


class PatreonError(Exception):
    """A Patreon API call failed. ``status`` is the HTTP status, or None for network errors."""

    def __init__(self, message: str, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


class PatreonClient:
    def __init__(self, token: str | None = None, campaign_id: str | None = None):
        self._token = token
        self._campaign_id = campaign_id
        self._session = requests.Session()
        self._cache: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    # ---- plumbing -------------------------------------------------------------------------

    @property
    def token(self) -> str:
        token = self._token or Config.PATREON_TOKEN
        if not token:
            raise PatreonError("PATREON_TOKEN is not set")
        return token

    @property
    def configured(self) -> bool:
        return bool(self._token or Config.PATREON_TOKEN)

    def _request(self, url: str, params: dict | None = None) -> dict:
        """GET one URL. Retries once on 429 (rate limit) or a network error."""
        headers = {"Authorization": f"Bearer {self.token}", "User-Agent": "Chobot - Patreon client"}
        for attempt in (1, 2):
            try:
                resp = self._session.get(url, headers=headers, params=params, timeout=TIMEOUT)
            except requests.RequestException as exc:
                if attempt == 2:
                    raise PatreonError(f"Patreon request failed: {exc}") from exc
                time.sleep(1)
                continue
            if resp.status_code == 429 and attempt == 1:
                wait = min(float(resp.headers.get("Retry-After") or 2), 30)
                logger.warning("Patreon rate limited, retrying in %.0fs", wait)
                time.sleep(wait)
                continue
            if not resp.ok:
                raise PatreonError(
                    f"Patreon API {resp.status_code} for {url.replace(API_BASE, '')}",
                    status=resp.status_code,
                    body=resp.text[:500],
                )
            return resp.json()
        raise PatreonError("Patreon request failed")  # not reached

    def get(self, path: str, params: dict | None = None) -> dict:
        """Raw GET against the API, e.g. ``get("/identity", {"include": "campaign"})``."""
        return self._request(f"{API_BASE}{path}", params)

    def _pages(self, path: str, params: dict) -> Iterator[dict]:
        """Yield each page of a paginated endpoint, following ``links.next``."""
        url, query = f"{API_BASE}{path}", dict(params)
        for _ in range(MAX_PAGES):
            page = self._request(url, query)
            yield page
            url = (page.get("links") or {}).get("next")
            if not url:
                return
            query = None  # the next link already carries every parameter
        logger.warning("Patreon pagination stopped after %d pages for %s", MAX_PAGES, path)

    def _cached(self, key: str, ttl: float, fresh: bool, load):
        now = time.monotonic()
        if not fresh:
            with self._lock:
                hit = self._cache.get(key)
            if hit and now - hit[0] < ttl:
                return hit[1]
        value = load()
        with self._lock:
            self._cache[key] = (now, value)
        return value

    def clear_cache(self) -> None:
        with self._lock:
            self._cache.clear()

    # ---- campaign -------------------------------------------------------------------------

    def campaign_id(self) -> str:
        """``PATREON_CAMPAIGN_ID`` if set, otherwise the token owner's first campaign."""
        configured = self._campaign_id or Config.PATREON_CAMPAIGN_ID
        if configured:
            return str(configured)

        def load():
            data = self.get("/campaigns").get("data") or []
            if not data:
                raise PatreonError("This token has no campaign")
            return str(data[0]["id"])

        return self._cached("campaign_id", 86400, False, load)

    def identity(self, fresh: bool = False) -> dict:
        """The token owner (the creator account), with campaign IDs."""
        def load():
            raw = self.get("/identity", {
                "include": "campaign",
                "fields[user]": "full_name,email,image_url,url,vanity,created",
            })
            out = _flatten(raw["data"])
            out["campaign_ids"] = [c["id"] for c in raw.get("included") or [] if c.get("type") == "campaign"]
            return out

        return self._cached("identity", 3600, fresh, load)

    def campaign(self, fresh: bool = False) -> dict:
        """Campaign info plus its tiers (``tiers``) and benefits (``benefits``)."""
        def load():
            raw = self.get(f"/campaigns/{self.campaign_id()}", {
                "include": "tiers,benefits",
                "fields[campaign]": CAMPAIGN_FIELDS,
                "fields[tier]": TIER_FIELDS,
                "fields[benefit]": BENEFIT_FIELDS,
            })
            included = raw.get("included") or []
            out = _flatten(raw["data"])
            out["tiers"] = sorted(
                (_flatten(i) for i in included if i.get("type") == "tier"),
                key=lambda t: t.get("amount_cents") or 0,
            )
            out["benefits"] = [_flatten(i) for i in included if i.get("type") == "benefit"]
            return out

        return self._cached("campaign", 600, fresh, load)

    def tiers(self, fresh: bool = False, published_only: bool = True) -> list[dict]:
        """Tiers sorted by price. Includes the free tier (amount_cents 0) if the campaign has one."""
        tiers = self.campaign(fresh=fresh)["tiers"]
        return [t for t in tiers if t.get("published")] if published_only else list(tiers)

    # ---- members --------------------------------------------------------------------------

    def members(self, fresh: bool = False, status: str | None = None) -> list[dict]:
        """Every member of the campaign, flattened.

        ``status`` filters on ``patron_status``: "active_patron", "declined_patron" or "former_patron".
        Followers (free members) have ``patron_status`` None.
        Each member has ``tiers`` (currently entitled tiers) and ``discord_id`` (None if they didn't
        connect Discord on Patreon).
        """
        def load():
            rows: list[dict] = []
            params = {
                "include": "currently_entitled_tiers,user",
                "fields[member]": MEMBER_FIELDS,
                "fields[tier]": "title,amount_cents",
                "fields[user]": USER_FIELDS,
                "page[count]": 1000,
            }
            for page in self._pages(f"/campaigns/{self.campaign_id()}/members", params):
                rows.extend(_member_rows(page))
            return rows

        rows = self._cached("members", 900, fresh, load)
        return [m for m in rows if m.get("patron_status") == status] if status else list(rows)

    def member(self, member_id: str) -> dict:
        """One member by Patreon member ID (not cached)."""
        raw = self.get(f"/members/{member_id}", {
            "include": "currently_entitled_tiers,user",
            "fields[member]": MEMBER_FIELDS,
            "fields[tier]": "title,amount_cents",
            "fields[user]": USER_FIELDS,
        })
        return _member_rows({"data": [raw["data"]], "included": raw.get("included")})[0]

    def member_by_discord_id(self, discord_id: str | int, fresh: bool = False) -> dict | None:
        """The member whose Patreon account is linked to this Discord user, or None."""
        target = str(discord_id)
        return next((m for m in self.members(fresh=fresh) if m.get("discord_id") == target), None)

    def member_by_email(self, email: str, fresh: bool = False) -> dict | None:
        target = (email or "").strip().lower()
        if not target:
            return None
        return next((m for m in self.members(fresh=fresh) if (m.get("email") or "").lower() == target), None)

    # ---- earnings (estimated) -------------------------------------------------------------

    def earnings(self, fresh: bool = False, days: int = 30) -> dict:
        """Estimated earnings from member pledges. All amounts are cents, GROSS (before Patreon's
        platform and payment fees, VAT and refunds). The API has no payouts endpoint; Patreon's
        own Insights/Payouts pages are the source of truth.

        - ``monthly_run_rate``: what active patrons are entitled to per month (their tier amounts).
        - ``at_risk``: monthly amount from patrons whose last payment was declined.
        - ``lifetime_total``: everything every member (incl. former) has paid this campaign.
        - ``charged_last_days``: paid charges dated in the last ``days`` days, counted at each
          patron's current pledge (Patreon doesn't expose per-charge history).
        - ``by_tier``: active patrons and monthly run rate per tier.
        """
        from datetime import datetime, timedelta, timezone

        members = self.members(fresh=fresh)
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        by_tier: dict[str, dict] = {}
        out = {
            "currency_note": "cents, campaign currency, gross",
            "active_patrons": 0, "monthly_run_rate": 0,
            "declined_patrons": 0, "at_risk": 0,
            "lifetime_total": 0,
            "days": days, "charged_last_days_count": 0, "charged_last_days": 0,
            "annual_patrons": 0,
        }
        for m in members:
            out["lifetime_total"] += m.get("campaign_lifetime_support_cents") or 0
            amount = m.get("currently_entitled_amount_cents") or 0
            status = m.get("patron_status")
            if status == "active_patron" and amount > 0:
                out["active_patrons"] += 1
                out["monthly_run_rate"] += amount
                if (m.get("pledge_cadence") or 1) > 1:
                    out["annual_patrons"] += 1
                for t in m.get("tiers") or [{"title": "(no tier)"}]:
                    if (t.get("amount_cents") or 0) == 0 and len(m.get("tiers") or []) > 1:
                        continue  # don't count the free tier alongside a paid one
                    row = by_tier.setdefault(t.get("title") or t.get("id"), {"patrons": 0, "monthly": 0})
                    row["patrons"] += 1
                    row["monthly"] += amount
                    break
            elif status == "declined_patron":
                out["declined_patrons"] += 1
                out["at_risk"] += amount or (m.get("will_pay_amount_cents") or 0)

            charged = _parse_dt(m.get("last_charge_date"))
            if m.get("last_charge_status") == "Paid" and charged and charged >= cutoff:
                out["charged_last_days_count"] += 1
                out["charged_last_days"] += m.get("will_pay_amount_cents") or amount
        out["by_tier"] = dict(sorted(by_tier.items(), key=lambda kv: -kv[1]["monthly"]))
        return out

    # ---- posts ----------------------------------------------------------------------------

    def posts(self, limit: int = 20, fresh: bool = False) -> list[dict]:
        """Newest posts first, including patron-only ones (check ``is_public`` before showing content)."""
        def load():
            rows: list[dict] = []
            params = {"fields[post]": POST_FIELDS, "sort": "-published_at", "page[count]": min(limit, 100)}
            for page in self._pages(f"/campaigns/{self.campaign_id()}/posts", params):
                rows.extend(_flatten(p) for p in page.get("data") or [])
                if len(rows) >= limit:
                    break
            return rows[:limit]

        return self._cached(f"posts:{limit}", 900, fresh, load)

    def post(self, post_id: str, fresh: bool = False) -> dict:
        return self._cached(
            f"post:{post_id}", 900, fresh,
            lambda: _flatten(self.get(f"/posts/{post_id}", {"fields[post]": POST_FIELDS})["data"]),
        )

    # ---- webhooks -------------------------------------------------------------------------

    def webhooks(self) -> list[dict]:
        """Webhooks registered by this token's client (needs the w:campaigns.webhook scope)."""
        raw = self.get("/webhooks", {"fields[webhook]": WEBHOOK_FIELDS})
        return [_flatten(w) for w in raw.get("data") or []]


# ---- helpers ------------------------------------------------------------------------------

def _flatten(resource: dict) -> dict:
    """JSON:API resource -> plain dict of id + attributes."""
    return {"id": resource.get("id"), **(resource.get("attributes") or {})}


def _parse_dt(value: str | None):
    from datetime import datetime, timezone

    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _discord_id(user_attrs: dict) -> str | None:
    discord = ((user_attrs or {}).get("social_connections") or {}).get("discord") or {}
    uid = discord.get("user_id")
    return str(uid) if uid else None


def _member_rows(page: dict) -> list[dict]:
    included = {(i.get("type"), i.get("id")): i for i in page.get("included") or []}
    rows = []
    for item in page.get("data") or []:
        rel = item.get("relationships") or {}
        tier_refs = ((rel.get("currently_entitled_tiers") or {}).get("data")) or []
        user_ref = ((rel.get("user") or {}).get("data")) or {}
        user = included.get(("user", user_ref.get("id")), {})
        user_attrs = user.get("attributes") or {}

        row = _flatten(item)
        row["tiers"] = [
            _flatten(included[("tier", t["id"])]) if ("tier", t["id"]) in included else {"id": t["id"]}
            for t in tier_refs
        ]
        row["user_id"] = user_ref.get("id")
        row["user"] = {k: v for k, v in user_attrs.items() if k != "social_connections"}
        row["discord_id"] = _discord_id(user_attrs)
        rows.append(row)
    return rows


patreon = PatreonClient()


if __name__ == "__main__":
    # Summary for checking the token. Prints counts and tier names only, no patron details.
    from collections import Counter

    logging.basicConfig(level=logging.INFO)
    client = PatreonClient()
    print("campaign id:", client.campaign_id())

    sections = {
        "campaign": lambda: client.campaign(),
        "members": lambda: client.members(),
        "posts": lambda: client.posts(limit=5),
        "webhooks": lambda: client.webhooks(),
    }
    for name, fn in sections.items():
        try:
            data = fn()
        except PatreonError as exc:
            print(f"{name}: FAILED ({exc.status}) {exc.body[:200]}")
            continue
        if name == "campaign":
            print(f"campaign: {data.get('creation_name') or data.get('vanity')!r}, "
                  f"patron_count={data.get('patron_count')}")
            for t in data["tiers"]:
                print(f"  tier {t.get('title')!r}: ${(t.get('amount_cents') or 0) / 100:.2f}, "
                      f"patrons={t.get('patron_count')}, published={t.get('published')}")
        elif name == "members":
            print(f"members: {len(data)} total, by status {dict(Counter(m.get('patron_status') for m in data))}, "
                  f"with discord linked={sum(1 for m in data if m.get('discord_id'))}, "
                  f"email visible={sum(1 for m in data if m.get('email'))}")
        elif name == "posts":
            print(f"posts: {len(data)} fetched, public={sum(1 for p in data if p.get('is_public'))}")
        else:
            print(f"webhooks: {len(data)} registered")

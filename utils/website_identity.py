"""Travelers who use ChoPaeng through the website without being in the Discord server.

The website reveals sub-island Dodo codes to accounts with no Discord link (email,
Twitch, Patreon or YouTube logins). Their flights match a saved character or a Dodo
reveal, but there is no Discord member to resolve, so the Discord flight logger and the
web auto-check both check the account's subscription tier here instead of Discord roles.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

from utils.helpers import clean_text

logger = logging.getLogger("WebsiteIdentity")

# All saved characters: their IGNs are matched with clean_text, which SQL cannot reproduce.
SAVED_CHARACTERS_SQL = (
    "SELECT user_id, ign, island_name FROM user_saved_characters WHERE TRIM(ign) != '' "
    "ORDER BY is_default DESC, updated_at DESC"
)


def saved_character_owners(rows: Iterable[Any], ign: str, origin: str) -> list[str]:
    """Owners of saved characters matching the flight: IGN + island first, else IGN only."""
    ign_clean, origin_clean = clean_text(ign), clean_text(origin)
    if not ign_clean:
        return []
    exact: list[str] = []
    loose: list[str] = []
    for user_id, c_ign, c_island in rows:
        if not user_id or clean_text(c_ign) != ign_clean:
            continue
        if origin_clean and clean_text(c_island) == origin_clean:
            exact.append(str(user_id))
        else:
            loose.append(str(user_id))
    return list(dict.fromkeys(exact or loose))


def load_website_account(user_id: Any) -> dict | None:
    """The unified website account (roles, tiers, linked providers) behind a user ID."""
    uid = str(user_id or "").strip()
    if not uid:
        return None
    try:
        from utils.unified_auth import get_unified_account_by_id, resolve_all_user_ids

        for candidate in dict.fromkeys([uid, *resolve_all_user_ids(uid)]):
            account = get_unified_account_by_id(candidate)
            if account:
                return account
    except Exception as exc:
        logger.debug("Website account lookup failed for %s: %s", uid, exc)
    return None


def is_website_only(account: dict | None) -> bool:
    """True for accounts with no Discord link: they can never be resolved to a member."""
    if not account:
        return False
    providers = {str(p).lower() for p in account.get("connected_providers") or []}
    return not account.get("discord_id") and "discord" not in providers and account.get("provider") != "discord"


def website_only_account(user_id: Any) -> dict | None:
    """The website account behind a user ID, when it has no Discord link."""
    account = load_website_account(user_id)
    return account if is_website_only(account) else None


def account_label(account: dict) -> str:
    return str(account.get("username") or account.get("discord_name") or account.get("user_id") or "Website account")


def website_member(account: dict) -> dict:
    """How a website account is stored as the traveler in a flight verdict."""
    return {"id": str(account.get("user_id") or ""), "name": account_label(account), "kind": "website"}


def account_tiers(account: dict) -> list[str]:
    """Subscription tier names an account holds through any linked platform."""
    from utils.island_access import ROLE_ID_TO_TIER_NAME, TIER_NAME_TO_ROLE_ID, normalize_roles

    raw = list(account.get("roles") or [])
    if account.get("sub_tier"):
        raw.append(account["sub_tier"])
    for linked in (account.get("connected_accounts") or {}).values():
        if isinstance(linked, dict):
            raw.extend(linked.get("roles") or [])
            if linked.get("sub_tier"):
                raw.append(linked["sub_tier"])
    tiers = {ROLE_ID_TO_TIER_NAME.get(r, r) for r in normalize_roles(raw)}
    return sorted(t for t in tiers if t in TIER_NAME_TO_ROLE_ID)


def website_verdict(account: dict, destination: str, found_via: str | None) -> tuple[str, dict]:
    """Verdict for a flight matched to a website account, using the website's own access rule."""
    from utils import island_access

    required = island_access.effective_island_required_roles("member", None, "VIP", destination)
    tiers = account_tiers(account)
    member = website_member(account)
    if island_access.has_island_access(account.get("roles") or [], required, island_name=destination, user=account):
        return "verified", {
            "reason": "Matched a website account (not in the Discord server)",
            "member": member,
            "found_via": found_via,
            "subs": tiers,
        }
    need = sorted({island_access.ROLE_ID_TO_TIER_NAME.get(str(r), str(r)) for r in required} & set(island_access.TIER_NAME_TO_ROLE_ID))
    return "island_access_mismatch", {
        "reason": (
            "Website account has no subscription for destination island "
            f"(has: {', '.join(tiers) or 'none'}; needs: {', '.join(need) or 'unknown'})"
        ),
        "member": member,
        "found_via": found_via,
        "subs": tiers,
        "required_roles": need,
    }

"""Shared actions behind every drop/injection entry point (_perform_drop, _perform_injection, guardrails)."""

from __future__ import annotations

import asyncio
import discord
import logging
import re
import time
from dataclasses import dataclass
from typing import Any
from utils.sysbot_api import get_island_sysbot_client

from .constants import PANEL_EMBED_COLOR, PANEL_EMBED_COLOR_ERR, PANEL_EMBED_COLOR_WARN, PANEL_FOOTER_ICON, PANEL_FOOTER_TEXT, PLOT_MAX, PLOT_MIN, SANRIO_VILLAGER_CODES, UNKNOWN_ISLAND_MSG, get_canonical_sanrio_name, is_sanrio_villager
from .state import DROP_SUBMIT_TIMEOUT, MAX_DROP_QUEUE, _ISLAND_DROP_WAITING, _cooldown_eta_ts, _cooldown_left, _island_drop_lock, _pop_cooldown
from .db import _log_drop, _log_injection, _record_user_drop
from .islands import _access_denied, _canonical_island, _island_url
from .parsing import build_drop_payload, parse_drop_items
from .status import _ISLAND_STATUS_CACHE, _clean_key, _err_embed, _safe_call, fetch_island_live_status
from .ui_base import _reply

logger = logging.getLogger("DropBotCog")


def _sanitize_error_message(err: Any) -> str:
    s = str(err or "").strip()
    s = re.sub(r"https?://[^\s/$.?#].[^\s]*", "<redacted-url>", s)
    s = re.sub(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b", "<redacted-host>", s)
    s = re.sub(r"\b(?:localhost|127\.0\.0\.1|0\.0\.0\.0)(?::\d+)?\b", "<redacted-host>", s, flags=re.IGNORECASE)
    if any(keyword in s.lower() for keyword in ("connectionrefused", "clientconnecterror", "oserror", "socket", "traceback")):
        return "Connection to the island bot timed out or was refused."
    return s[:200]


async def _resolve_client(interaction: discord.Interaction, island_raw: str | None) -> tuple[str, Any] | None:
    """Validate the island and build the SysBot client. Replies to the user and returns None on failure."""
    island = _canonical_island(island_raw)
    if not island:
        await _reply(interaction, embed=_err_embed(UNKNOWN_ISLAND_MSG))
        return None
    try:
        client = get_island_sysbot_client(island)
    except Exception:
        logger.exception("[DropBot] Could not create SysBot client for %s", island)
        await _reply(interaction, embed=_err_embed("This island's bot isn't configured right now. Try again later."))
        return None
    return island, client


def _is_failure(result: Any) -> bool:
    return not isinstance(result, dict) or result.get("success") is False or (
        bool(result.get("error")) and not result.get("success")
    )


# ─── Shared actions (used by modals, catalog view and confirm view) ────────────

async def _check_island_online(island: str) -> bool:
    """Quick check: is this island's bot currently running? Uses cached status snapshot."""
    canonical = _canonical_island(island) or island
    key = _clean_key(canonical)
    hit = _ISLAND_STATUS_CACHE.get(key)
    if hit and time.monotonic() - hit[0] < 60.0:
        return bool(hit[1].get("is_running", True))
    try:
        status = await fetch_island_live_status(island)
        return bool(status.get("is_running", True))
    except Exception:
        return True  # fail open


async def _perform_drop(
    user: discord.abc.User,
    island_raw: str,
    items_or_entries: list[tuple[str, int]] | str,
    *,
    heading: str,
    contents: str,
    raw_payload: bool = False,
) -> tuple[discord.Embed, bool]:
    """
    Returns (embed to show, whether the drop was queued).
    raw_payload=True sends a string verbatim (a previously built SysBot payload) without re-parsing it.
    """
    denied = await _access_denied(user)
    if denied:
        return _err_embed(denied), False
    island = _canonical_island(island_raw)
    if not island:
        return _err_embed(UNKNOWN_ISLAND_MSG), False

    # Precheck: tell user immediately if bot is offline
    if not await _check_island_online(island):
        _pop_cooldown(user.id, island, "drop")
        embed = discord.Embed(
            title="🔴 Island Bot Offline",
            description=(
                f"**{island}**'s bot appears to be offline right now.\n"
                "Your drop was **not** submitted. Please try again when the bot is back online."
            ),
            color=PANEL_EMBED_COLOR_ERR,
        )
        embed.set_footer(text=PANEL_FOOTER_TEXT, icon_url=PANEL_FOOTER_ICON)
        return embed, False

    wait = _cooldown_left(user.id, island, "drop")
    if wait:
        return _err_embed(
            f"You can drop again {_cooldown_eta_ts(wait)}.",
            title="Cooldown Active",
            hint=f"Drops have a short cooldown. Try again {_cooldown_eta_ts(wait)}.",
        ), False

    # Presence check: verify user is physically on the island before dropping
    from utils.island_presence import verify_user_on_island_async
    is_present, matched_ign, current_visitors = await verify_user_on_island_async(
        island, user=user, allow_admin_bypass=True
    )
    if not is_present:
        _pop_cooldown(user.id, island, "drop")
        embed = discord.Embed(
            title="✈️ Airport Gate Verification Failed",
            description=(
                f"You must be on **[{island}]({_island_url(island)})** before dropping items!\n\n"
                f"I have checked the island, but your character was **not detected** on {island}.\n\n"
                f"• Please fly to **{island}** first and step outside the airport gates.\n"
                f"• Make sure your server nickname or profile character matches your in-game name (IGN)."
            ),
            color=PANEL_EMBED_COLOR_ERR,
        )
        if current_visitors:
            embed.add_field(
                name="Current Visitors On Island",
                value=f"```{', '.join(current_visitors)[:300]}```",
                inline=False,
            )
        embed.set_footer(text=PANEL_FOOTER_TEXT, icon_url=PANEL_FOOTER_ICON)
        return embed, False

    recorded_entries: list[tuple[str, int]] | None = None
    if isinstance(items_or_entries, list):
        recorded_entries = list(items_or_entries)
        sysbot_payload = build_drop_payload(recorded_entries)
    elif raw_payload:
        sysbot_payload = items_or_entries
    else:
        parsed, _ = parse_drop_items(items_or_entries)
        if parsed:
            recorded_entries = parsed
            sysbot_payload = build_drop_payload(parsed)
        else:
            sysbot_payload = items_or_entries

    ahead = _ISLAND_DROP_WAITING[island]   # drops submitted before this one that haven't finished
    if ahead >= MAX_DROP_QUEUE:
        _pop_cooldown(user.id, island, "drop")
        return _err_embed(
            f"**{island}** has {ahead} drops waiting right now. Please try again in a minute.",
            title="Island Busy",
        ), False
    _ISLAND_DROP_WAITING[island] += 1
    try:
        client = get_island_sysbot_client(island)
        async with _island_drop_lock(island):
            result = await asyncio.wait_for(
                client.submit_drop(items=sysbot_payload, username=getattr(user, "display_name", user.name)),
                DROP_SUBMIT_TIMEOUT,
            )
    except Exception as exc:
        _pop_cooldown(user.id, island, "drop")
        err_msg = str(exc) or "bot did not respond"
        logger.exception("[DropBot] Drop failed island=%s user=%s: %s", island, user.id, err_msg)
        await asyncio.to_thread(_log_drop, island, user.id, sysbot_payload, False, err_msg)

        embed = discord.Embed(
            title="⚠️ Drop Failed — Bot Offline",
            description=f"The island bot for **[{island}]({_island_url(island)})** didn't respond.",
            color=PANEL_EMBED_COLOR_ERR,
        )
        embed.add_field(name="Reason", value=f"```{_sanitize_error_message(err_msg)}```", inline=False)
        embed.add_field(name="Attempted Items", value=f"```{contents[:300]}```", inline=False)
        embed.add_field(name="Status", value="Please make sure the island bot is running and connected.", inline=False)
        embed.set_footer(text=PANEL_FOOTER_TEXT, icon_url=PANEL_FOOTER_ICON)
        return embed, False
    finally:
        _ISLAND_DROP_WAITING[island] -= 1

    if _is_failure(result):
        _pop_cooldown(user.id, island, "drop")
        err = (result or {}).get("error") if isinstance(result, dict) else None
        err = err or "unknown error"
        logger.warning("[DropBot] Drop rejected island=%s user=%s: %s", island, user.id, err)
        await asyncio.to_thread(_log_drop, island, user.id, sysbot_payload, False, str(err))

        embed = discord.Embed(
            title="⚠️ Drop Request Rejected",
            description=f"Your drop request on **[{island}]({_island_url(island)})** was not accepted by the bot.",
            color=PANEL_EMBED_COLOR_ERR,
        )
        embed.add_field(name="Error Details", value=f"```{_sanitize_error_message(err)}```", inline=False)
        embed.add_field(name="Items", value=f"```{contents[:300]}```", inline=False)
        embed.add_field(
            name="💡 Troubleshooting",
            value=(
                "• Ensure item names match the in-game English name.\n"
                "• For non-stackable items (furniture, tools, clothes), do not add a count.\n"
                "• Check that the island drop area is not full."
            ),
            inline=False,
        )
        embed.set_footer(text=PANEL_FOOTER_TEXT, icon_url=PANEL_FOOTER_ICON)
        return embed, False

    # Audit log uses hex payload; success message shows human-readable names
    await asyncio.to_thread(_log_drop, island, user.id, sysbot_payload, True, str(result.get("message") or "queued"))
    _record_user_drop(user.id, island, sysbot_payload, contents, recorded_entries)

    embed = discord.Embed(
        title=heading,
        description=(
            f"Your drop on **[{island}]({_island_url(island)})** is queued.\n"
            "Items will appear near you in a few seconds. **You must be on the island.**"
        ),
        color=PANEL_EMBED_COLOR,
    )
    embed.add_field(name="Items", value=f"```{contents[:400]}```", inline=False)
    if ahead:
        embed.add_field(
            name="Queue",
            value=f"{ahead} drop{'s were' if ahead != 1 else ' was'} ahead of yours on this island.",
            inline=False,
        )
    embed.set_footer(text=PANEL_FOOTER_TEXT, icon_url=PANEL_FOOTER_ICON)
    return embed, True


async def _perform_injection(
    user: discord.abc.User,
    island_raw: str,
    villager: str,
    plot: int | None,
    *,
    cooldown_key: str = "inject",
) -> tuple[discord.Embed, bool]:
    """Returns (embed to show, whether the injection was queued)."""
    denied = await _access_denied(user)
    if denied:
        return _err_embed(denied), False
    island = _canonical_island(island_raw)
    if not island:
        return _err_embed(UNKNOWN_ISLAND_MSG), False

    # Precheck: tell user immediately if bot is offline
    if not await _check_island_online(island):
        _pop_cooldown(user.id, island, cooldown_key)
        embed = discord.Embed(
            title="🔴 Island Bot Offline",
            description=(
                f"**{island}**'s bot appears to be offline right now.\n"
                "Your injection was **not** submitted. Please try again when the bot is back online."
            ),
            color=PANEL_EMBED_COLOR_ERR,
        )
        embed.set_footer(text=PANEL_FOOTER_TEXT, icon_url=PANEL_FOOTER_ICON)
        return embed, False

    wait = _cooldown_left(user.id, island, cooldown_key)
    if wait:
        return _err_embed(
            f"You can inject again {_cooldown_eta_ts(wait)}.",
            title="Cooldown Active",
            hint=f"Injections have a short cooldown. Try again {_cooldown_eta_ts(wait)}.",
        ), False

    display_name = villager
    villager_code = villager
    if is_sanrio_villager(villager):
        display_name = get_canonical_sanrio_name(villager)
        villager_code = SANRIO_VILLAGER_CODES.get(villager.strip().lower(), villager)
    else:
        try:
            from utils.data_manager import resolve_villager_code
            villager_code = resolve_villager_code(villager)
        except Exception:
            pass

    try:
        client = get_island_sysbot_client(island)
        result = await client.inject_villagers(
            villager_code,
            plot=plot,
            allow_unadoptable=is_sanrio_villager(villager),
        )
    except Exception:
        _pop_cooldown(user.id, island, cooldown_key)
        logger.exception("[DropBot] Injection failed island=%s user=%s", island, user.id)
        await asyncio.to_thread(_log_injection, island, display_name, plot, user.id, False, "bot did not respond")
        return _err_embed("The island bot didn't respond. Check that it's online and try again."), False

    if _is_failure(result):
        _pop_cooldown(user.id, island, cooldown_key)
        err: str | None = None
        if isinstance(result, dict):
            err = result.get("error")
            if not err and result.get("errors"):
                errs = result.get("errors")
                err = errs[0] if isinstance(errs, list) and errs else str(errs)
            if not err:
                err = result.get("message")
        err = err or "unknown error"
        logger.warning("[DropBot] Injection rejected island=%s user=%s: %s", island, user.id, err)
        await asyncio.to_thread(_log_injection, island, display_name, plot, user.id, False, str(err))
        if "not adoptable" in str(err).lower() and is_sanrio_villager(villager):
            return _err_embed(
                f"The island bot rejected **{display_name}**: `{_sanitize_error_message(err)}`.\n"
                f"The island bot's SysBot binary must be updated/restarted with Sanrio support enabled."
            ), False
        return _err_embed(f"The injection was rejected: **{_sanitize_error_message(err)}**\nMake sure the island bot is online."), False

    await asyncio.to_thread(
        _log_injection, island, display_name, plot, user.id, True,
        str(result.get("message") or "Queued via Discord panel"),
    )

    desc = f"**{display_name}** is queued for injection into **[{island}]({_island_url(island)})**.\n"
    if is_sanrio_villager(villager):
        desc += "Enter Plot 1's house on the island to invite them to move to your island! 🎀"
    else:
        desc += "You **must be on the island** for the injection to take effect."

    embed = discord.Embed(
        title="🐱 Villager injection queued",
        description=desc,
        color=PANEL_EMBED_COLOR,
    )
    if plot is not None:
        embed.add_field(name="Requested plot", value=str(plot), inline=True)
    embed.set_footer(text=PANEL_FOOTER_TEXT, icon_url=PANEL_FOOTER_ICON)
    return embed, True


@dataclass(frozen=True)
class _Resident:
    name: str
    plot: int | None   # only set when the island bot reports an explicit plot number


def _parse_residents(raw: Any) -> list[_Resident]:
    """Residents from a SysBot GET /api/villagers payload; tolerant of plain names or dict entries."""
    entries = raw.get("villagers") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        return []
    residents: list[_Resident] = []
    for entry in entries:
        if isinstance(entry, dict):
            name = str(entry.get("name") or entry.get("Name") or "").strip()
            plot = next(
                (int(v) for v in (entry.get("plot"), entry.get("Plot")) if str(v).isdigit()),
                None,
            )
        else:
            name, plot = str(entry or "").strip(), None
        if name:
            residents.append(_Resident(name, plot if plot is not None and PLOT_MIN <= plot <= PLOT_MAX else None))
    return residents


async def _fetch_residents(island: str) -> list[_Resident]:
    """Who lives on the island right now. Empty when unknown (the guardrails then stay out of the way)."""
    try:
        client = get_island_sysbot_client(island, timeout=3)
    except Exception as exc:
        logger.debug("[DropBot] no SysBot client for residents of %s: %s", island, exc)
        return []
    return _parse_residents(await _safe_call(client.get_villagers(), 4, f"get_villagers[{island}]"))


def _injection_guard(
    island: str, villager: str, plot: int | None, residents: list[_Resident]
) -> tuple[discord.Embed | None, tuple[discord.Embed, str] | None]:
    """
    (blocked, confirm): `blocked` is an error embed when the injection can't make sense;
    `confirm` is (embed, replaced villager) when it would overwrite a known resident.
    """
    if not residents:
        return None, None
    key = _clean_key(villager)
    if any(_clean_key(r.name) == key for r in residents):
        return _err_embed(
            f"**{villager}** already lives on **[{island}]({_island_url(island)})**, "
            "and an island can't have the same villager twice.",
            title="Already a Resident",
            hint=f"Current residents: {', '.join(r.name for r in residents)}",
        ), None
    occupant = next((r for r in residents if plot is not None and r.plot == plot), None)
    if occupant:
        embed = discord.Embed(
            title="⚠️ Replace this resident?",
            description=(
                f"Plot **{plot}** on **[{island}]({_island_url(island)})** is currently home to "
                f"**{occupant.name}**.\n\nInjecting **{villager}** there will replace them."
            ),
            color=PANEL_EMBED_COLOR_WARN,
        )
        embed.add_field(name="Before", value=occupant.name, inline=True)
        embed.add_field(name="After", value=villager, inline=True)
        embed.set_footer(text=PANEL_FOOTER_TEXT, icon_url=PANEL_FOOTER_ICON)
        return None, (embed, occupant.name)
    return None, None

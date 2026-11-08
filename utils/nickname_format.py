"""Shared ACNH server nickname format helpers."""

from __future__ import annotations

import re

NICKNAME_FORMAT_EXAMPLE = "Character Name | Island Name"
NICKNAME_FORMAT_MESSAGE = (
    "Please set your server nickname to `Character Name | Island Name` before joining the island. "
    "Example: `ChoPaeng | ChoPaeng Camp`. You can still use this Dodo code, but staff and ChoBot "
    "need your nickname set correctly before you fly."
)


def _has_name_text(value: str) -> bool:
    return bool(re.search(r"[\w\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]", value, re.UNICODE))


def is_valid_acnh_nickname(value: str | None) -> bool:
    """Return True for nicknames shaped like one or more ACNH identity pairs."""
    if not value:
        return False

    chunks = [chunk.strip() for chunk in str(value).split("|") if chunk.strip()]
    if chunks and chunks[0].casefold() == "acnh":
        chunks = chunks[1:]

    if len(chunks) < 2:
        return False

    if len(chunks) % 2 == 0:
        pairs = [(chunks[i], chunks[i + 1]) for i in range(0, len(chunks), 2)]
    else:
        pairs = [(chunks[0], island) for island in chunks[1:]]

    for ign_raw, island_raw in pairs:
        igns = [part.strip() for part in ign_raw.split("/") if part.strip()]
        islands = [part.strip() for part in island_raw.split("/") if part.strip()]
        if not igns or not islands:
            return False
        if not all(_has_name_text(part) for part in igns + islands):
            return False

    return True


def nickname_warning_for(value: str | None) -> str | None:
    """Return the user-facing warning when a nickname is missing or invalid."""
    return None if is_valid_acnh_nickname(value) else NICKNAME_FORMAT_MESSAGE


# ---------------------------------------------------------------------------
# Nickname builder (used by the nickname panel)
# ---------------------------------------------------------------------------

DISCORD_NICK_MAX = 32
_SEPARATORS = ("|", "/")


def nickname_to_pairs(value: str | None) -> list[tuple[str, str]]:
    """Split a nickname into raw (character, island) blocks, mirroring the logger's parser.

    Returns the text of each block as typed (e.g. ("bitress", "sefire/ice")), so it can
    pre-fill the panel form. Returns [] for nicknames without the "|" format.
    """
    if not value or "|" not in value:
        return []
    chunks = [c.strip() for c in str(value).split("|") if c.strip()]
    if chunks and chunks[0].casefold() == "acnh":
        chunks = chunks[1:]
    if len(chunks) < 2:
        return []
    if len(chunks) % 2 == 0:
        return [(chunks[i], chunks[i + 1]) for i in range(0, len(chunks), 2)]
    return [(chunks[0], "/".join(chunks[1:]))]


def flatten_identities(pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """One (character, island) per identity: ("bitress", "sefire/ice") -> two entries.

    Duplicates (case-insensitive) are dropped; order is kept.
    """
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for char_raw, islands_raw in pairs:
        char = " ".join((char_raw or "").split())
        for island in (islands_raw or "").split("/"):
            island = " ".join(island.split())
            key = (char.casefold(), island.casefold())
            if char and island and key not in seen:
                seen.add(key)
                out.append((char, island))
    return out


def pairs_to_form_values(pairs: list[tuple[str, str]]) -> tuple[str, str, str]:
    """(character, island, extra lines) to pre-fill the nickname form from identities."""
    grouped: dict[str, tuple[str, list[str]]] = {}
    for char, island in flatten_identities(pairs):
        grouped.setdefault(char.casefold(), (char, []))[1].append(island)
    blocks = [(c, "/".join(isl)) for c, isl in grouped.values()]
    if not blocks:
        return "", "", ""
    first, rest = blocks[0], blocks[1:]
    return first[0], first[1], "\n".join(f"{c} | {i}" for c, i in rest)


def parse_identity_lines(text: str | None) -> tuple[list[tuple[str, str]], list[str]]:
    """Parse extra "Character | Island" lines from the panel form.

    Returns (pairs, errors). Each line needs exactly one "|"; several islands for one
    character may be separated with "/".
    """
    pairs: list[tuple[str, str]] = []
    errors: list[str] = []
    for n, line in enumerate((text or "").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) != 2 or not parts[0] or not parts[1]:
            errors.append(f"Line {n} (`{line}`) must look like `Character | Island`.")
            continue
        pairs.append((parts[0], parts[1]))
    return pairs, errors


def _name_error(label: str, value: str) -> str | None:
    if not value:
        return f"{label} is required."
    if "|" in value:
        return f"{label} `{value}` can't contain `|`."
    if not _has_name_text(value):
        return f"{label} `{value}` needs at least one letter or number."
    return None


def build_nickname(pairs: list[tuple[str, str]]) -> tuple[str | None, list[str]]:
    """Build a canonical nickname from (character, islands) pairs.

    Islands for one character can be "/"-separated. Pairs with the same character are
    merged, so ("bitress","sefire"),("bitress","ice") -> "bitress | sefire/ice".
    Multiple characters become "C1 | I1 | C2 | I2". If the result is over Discord's
    32-character limit, spaces around "|" are dropped; if it is still too long an
    error is returned. Returns (nickname, errors).
    """
    errors: list[str] = []
    grouped: dict[str, list[str]] = {}
    display: dict[str, str] = {}
    for char_raw, islands_raw in pairs:
        char = " ".join((char_raw or "").split())
        if "/" in char:
            errors.append(f"Character `{char}` can't contain `/`. Put each character on its own line.")
            continue
        err = _name_error("Character name", char)
        if err:
            errors.append(err)
            continue
        islands = [" ".join(i.split()) for i in (islands_raw or "").split("/") if i.strip()]
        if not islands:
            errors.append(f"Island name for `{char}` is required.")
            continue
        for island in islands:
            err = _name_error("Island name", island)
            if err:
                errors.append(err)
        key = char.casefold()
        display.setdefault(key, char)
        bucket = grouped.setdefault(key, [])
        for island in islands:
            if island.casefold() not in {i.casefold() for i in bucket}:
                bucket.append(island)

    if errors:
        return None, errors
    if not grouped:
        return None, ["Enter a character name and an island name."]

    blocks = [(display[k], "/".join(v)) for k, v in grouped.items()]
    nick = " | ".join(f"{c} | {i}" for c, i in blocks)
    if len(nick) > DISCORD_NICK_MAX:
        nick = "|".join(f"{c}|{i}" for c, i in blocks)
    if len(nick) > DISCORD_NICK_MAX:
        return None, [
            f"`{nick}` is {len(nick)} characters; Discord allows {DISCORD_NICK_MAX}. "
            "Shorten a name or drop an identity."
        ]
    return nick, []

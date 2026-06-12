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

"""Traveler identity matching shared by the Discord flight logger and the web dashboard.

These are the rules bots/flight_logger.py uses to tie a flight ("IGN from Island")
to a member (Discord nicknames in "IGN | Island" form, Dodo-reveal usernames) and to
check how many identities a member's nickname claims.
"""

from __future__ import annotations

import datetime
import re
import unicodedata

from utils.helpers import clean_text


def ign_matches_dodo_reveal(ign: str, username: str | None, nickname: str | None) -> bool:
    """Heuristic: flight-list IGN vs Discord username/nickname from dodo reveal webhook."""
    i = clean_text(ign)
    if not i or len(i) < 2:
        return False
    for raw in (username, nickname):
        if not raw:
            continue
        c = clean_text(raw)
        if not c:
            continue
        if i == c:
            return True
        if len(i) >= 3 and (i in c or c in i):
            return True
        if c.startswith(i) or i.startswith(c):
            return True
    return False


def normalize_identity_text(text: str) -> str:
    """Normalize IGN/island while preserving symbols for strict identity checks."""
    if not text:
        return ""
    # Keep symbols (e.g. $$$, _, -), normalize Unicode width/compat forms,
    # and collapse whitespace so cosmetic spacing differences do not break matches.
    normalized = unicodedata.normalize("NFKC", text).casefold().strip()

    # Canonicalize common punctuation variants without removing symbols.
    quote_map = {
        "‘": "'", "’": "'", "ʼ": "'", "′": "'", "＇": "'",
        "“": '"', "”": '"',
        "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "−": "-",
    }
    normalized = "".join(quote_map.get(ch, ch) for ch in normalized)

    # Normalize spaces around punctuation often introduced by mobile keyboards.
    normalized = re.sub(r"\s*'\s*", "'", normalized)
    normalized = re.sub(r'\s*"\s*', '"', normalized)
    normalized = re.sub(r"\s*-\s*", "-", normalized)
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized


def split_options(raw: str) -> list[str]:
    if not raw:
        return []
    parts = [p.strip() for p in raw.split("/") if p.strip()]
    out = []
    for p in parts:
        norm = normalize_identity_text(p)
        if norm:
            out.append(norm)
    return out


def parse_member_nick(display_name: str) -> tuple[list[str], list[str]]:
    """Split an "IGN | Island" style nickname into normalized IGN and island options."""
    if not display_name:
        return [], []

    # Support ONLY | as a nick separator
    if '|' not in display_name:
        return [], []
    chunks = [c.strip() for c in display_name.split('|') if c.strip()]

    # Skip "ACNH" prefix if present as the first chunk
    if chunks and chunks[0].upper() == "ACNH":
        chunks = chunks[1:]

    if not chunks:
        return [], []

    ign_opts = []
    island_opts = []

    if len(chunks) % 2 == 0:
        # Even number of chunks: treat as (IGN, Island) pairs
        # e.g., "IGN1 | Island1 | IGN2 | Island2" or "IGN1 | Island1"
        for i in range(0, len(chunks), 2):
            ign_opts.extend(split_options(chunks[i]))
            island_opts.extend(split_options(chunks[i + 1]))
    else:
        # Odd number of chunks (e.g., 1 or 3+): First is IGN, rest are Islands
        # e.g., "IGN1 | Island1 | Island2"
        ign_opts = split_options(chunks[0])
        for i in range(1, len(chunks)):
            island_opts.extend(split_options(chunks[i]))

    # Deduplicate results
    return list(dict.fromkeys(ign_opts)), list(dict.fromkeys(island_opts))


def is_strict_nick_match(ign_log_clean: str, island_log_clean: str, ign_opts: list[str], island_opts: list[str]) -> bool:
    """Return True only when both IGN and island from nickname match the flight log.

    Supports paired nick formats like "IGN1/IGN2 | Island1/Island2" by checking
    index-aligned pairs first; otherwise falls back to set membership for both fields.
    """
    if not ign_opts or not island_opts:
        return False

    if len(ign_opts) == len(island_opts):
        for ign_opt, island_opt in zip(ign_opts, island_opts):
            if ign_log_clean == ign_opt and island_log_clean == island_opt:
                return True

    return ign_log_clean in ign_opts and island_log_clean in island_opts


def nickname_matches_flight(display_name: str, ign: str, origin_island: str) -> bool:
    """True when a member's "IGN | Island" nickname matches the flight exactly."""
    ign_opts, island_opts = parse_member_nick(display_name)
    return is_strict_nick_match(
        normalize_identity_text(ign), normalize_identity_text(origin_island), ign_opts, island_opts
    )


# Members who joined before the one-character/island rule may keep two identities.
LEGACY_TWO_IDENTITY_CUTOFF_UTC = datetime.datetime(2022, 9, 1, tzinfo=datetime.timezone.utc)
LEGACY_TWO_IDENTITY_LIMIT = 2


def calculate_max_identities(display_name: str) -> int:
    """Calculate the number of subscriptions required based on nickname slots and options.

    Rules:
    - If a category (IGN or Island) has multiple | blocks, its count is the number of blocks.
    - If it has only one | block, its count is the number of / options in that block.
    - The total count is the maximum of the IGN count and Island count.
    """
    if not display_name or '|' not in display_name:
        return 1

    chunks = [c.strip() for c in display_name.split('|') if c.strip()]
    if chunks and chunks[0].upper() == "ACNH":
        chunks = chunks[1:]

    if not chunks:
        return 1

    ign_blocks = []
    island_blocks = []

    if len(chunks) % 2 == 0:
        # Even chunks: pairs of (IGN, Island)
        for i in range(0, len(chunks), 2):
            ign_blocks.append(chunks[i])
            island_blocks.append(chunks[i + 1])
    else:
        # Odd chunks: first is IGN, rest are Islands
        ign_blocks.append(chunks[0])
        for i in range(1, len(chunks)):
            island_blocks.append(chunks[i])

    def get_category_count(blocks):
        if not blocks:
            return 0
        if len(blocks) > 1:
            # Multiple blocks: each block is an identity (ignore internal / splits)
            return len(blocks)
        # Single block: count / options
        return len(split_options(blocks[0]))

    return max(get_category_count(ign_blocks), get_category_count(island_blocks))


def get_allowed_identity_count(joined_at: datetime.datetime | None, sub_count: int) -> int:
    """Return how many nickname identities a member may currently have.

    Legacy members who joined before the one-character/island rule took effect
    are allowed to keep up to two existing character/island identities.
    """
    allowed = max(1, sub_count)
    if joined_at is None:
        return allowed

    if joined_at.tzinfo is None:
        joined_at = joined_at.replace(tzinfo=datetime.timezone.utc)
    else:
        joined_at = joined_at.astimezone(datetime.timezone.utc)

    if joined_at < LEGACY_TWO_IDENTITY_CUTOFF_UTC:
        allowed = max(allowed, LEGACY_TWO_IDENTITY_LIMIT)
    return allowed

"""
Helper Utilities Module
Common functions used across bots and APIs
"""

import re
from thefuzz import process, fuzz

from utils import Config


def normalize_text(s: str) -> str:
    """Normalize text for searching"""
    s = s.lower().strip()
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def tokenize(s: str) -> set:
    """Tokenize text for searching"""
    s = normalize_text(s)
    return set(t for t in s.split(" ") if t)


def smart_threshold(query: str) -> int:
    """Determine fuzzy matching threshold based on query length"""
    qlen = len(normalize_text(query))
    if qlen <= 3:
        return 97
    if qlen <= 5:
        return 92
    if qlen <= 8:
        return 86
    return 80


def format_locations_text(locations_str: str):
    """Format locations for text output (Twitch)"""
    locs_list = list(set(locations_str.split(", ")))
    free_islands = []
    sub_islands = []

    for loc in locs_list:
        if loc in Config.SUB_ISLANDS:
            sub_islands.append(loc)
        else:
            free_islands.append(loc)

    parts = []
    if free_islands:
        formatted_free = " | ".join(free_islands).upper()
        label = "this Free Island" if len(free_islands) == 1 else "these Free Islands"
        parts.append(f"on {label}: {formatted_free}")

    if sub_islands:
        formatted_sub = " | ".join(sub_islands).upper()
        label = "this Sub Island" if len(sub_islands) == 1 else "these Sub Islands"
        parts.append(f"on {label}: {formatted_sub}")

    return " and ".join(parts)


def parse_locations_json(locations_str: str):
    """Parse locations for JSON API response"""
    locs_list = list(set(locations_str.split(", ")))
    free_islands = [loc for loc in locs_list if loc not in Config.FREE_ISLANDS]
    sub_islands = [loc for loc in locs_list if loc in Config.SUB_ISLANDS]
    return free_islands, sub_islands


def get_best_suggestions(query: str, keys: list, limit: int = 8) -> list:
    """Get best fuzzy match suggestions for a query"""
    qn = normalize_text(query)
    if not qn:
        return []

    q_tokens = tokenize(qn)
    thresh = smart_threshold(qn)

    # 1) Easy wins (very accurate)
    exact = [k for k in keys if k == qn]
    if exact:
        return exact[:limit]

    starts = [k for k in keys if k.startswith(qn)]
    if starts:
        return starts[:limit]

    contains = [k for k in keys if qn in k]
    if contains:
        return contains[:limit]

    # 2) Restrict candidates by token overlap
    if q_tokens:
        restricted = []
        for k in keys:
            kt = tokenize(k)
            if kt & q_tokens:
                restricted.append(k)
        candidates = restricted if restricted else keys
    else:
        candidates = keys

    # 3) Fuzzy match
    matches = process.extract(
        qn,
        candidates,
        limit=limit * 2,
        scorer=fuzz.token_set_ratio
    )

    filtered = [m[0] for m in matches if m[1] >= thresh]

    # 4) Plural/singular fallback
    if not filtered and qn.endswith("s") and len(qn) > 3:
        q2 = qn[:-1]
        matches2 = process.extract(q2, candidates, limit=limit * 2, scorer=fuzz.token_set_ratio)
        filtered = [m[0] for m in matches2 if m[1] >= smart_threshold(q2)]

    # Return unique, in order
    seen = set()
    out = []
    for k in filtered:
        if k not in seen:
            seen.add(k)
            out.append(k)
        if len(out) >= limit:
            break
    return out
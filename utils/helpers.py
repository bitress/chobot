"""
Helper Utilities Module
Common functions used across bots and APIs
"""

import re
import time
import unicodedata
from typing import List, Set, Optional, Dict
from thefuzz import process, fuzz

from utils import Config


# ============================================================================
# COOLDOWN MANAGEMENT
# ============================================================================

class CooldownManager:
    """Manage user cooldowns with automatic cleanup"""
    
    def __init__(self, cleanup_threshold: int = 100, cleanup_age: int = 60):
        """Initialize cooldown manager
        
        Args:
            cleanup_threshold: Number of entries before cleanup triggers
            cleanup_age: Age in seconds to keep entries during cleanup
        """
        self.cooldowns: Dict[str, float] = {}
        self.cleanup_threshold = cleanup_threshold
        self.cleanup_age = cleanup_age
    
    def check_cooldown(self, user_id: str, cooldown_sec: int = 3) -> bool:
        """Check if user is on cooldown
        
        Args:
            user_id: User identifier
            cooldown_sec: Cooldown period in seconds
            
        Returns:
            True if user is on cooldown, False otherwise
        """
        now = time.time()
        if user_id in self.cooldowns:
            if now - self.cooldowns[user_id] < cooldown_sec:
                return True
        self.cooldowns[user_id] = now

        # Periodic cleanup: prune entries older than cleanup_age
        if len(self.cooldowns) > self.cleanup_threshold:
            self.cooldowns = {k: v for k, v in self.cooldowns.items() if now - v < self.cleanup_age}

        return False


# ============================================================================
# TEXT NORMALIZATION
# ============================================================================


def normalize_text(s: str) -> str:
    """Normalize text for searching"""
    s = s.lower().strip()
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def clean_text(text: str) -> str:
    """Clean text for island matching: alphanumeric only, no accents, lowercase.

    Handles fancy Unicode bot names, including:
    - Mathematical styled letters (𝕙, 𝔹, ℂ…) — resolved by NFKD compatibility decomposition
    - Small capital letters (ᴀ, ʟ, ᴘ…) — resolved via Unicode character name lookup
    """
    if not text:
        return ""
    # NFKD resolves compatibility characters: mathematical bold/italic/double-struck → ASCII
    normalized = unicodedata.normalize('NFKD', text)
    # Strip combining marks (diacritics/accents)
    no_marks = "".join(c for c in normalized if not unicodedata.category(c).startswith('Mn'))
    # Map remaining non-ASCII letters (e.g. small capitals) via their Unicode name
    # e.g. ᴀ → "LATIN LETTER SMALL CAPITAL A" → A
    result = []
    for c in no_marks:
        if c.isascii():
            result.append(c)
        elif unicodedata.category(c).startswith('L'):
            name = unicodedata.name(c, '')
            letter = next((p for p in reversed(name.split()) if len(p) == 1 and p.isalpha()), None)
            if letter:
                result.append(letter)
    return "".join(ch for ch in result if ch.isalnum()).lower()


def tokenize(s: str) -> Set[str]:
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


def format_locations_text(locations_str: str) -> str:
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


def parse_locations_json(locations_str: str) -> tuple[List[str], List[str]]:
    """Parse locations for JSON API response"""
    locs_list = list(set(locations_str.split(", ")))
    free_islands = [loc for loc in locs_list if loc in Config.FREE_ISLANDS]
    sub_islands = [loc for loc in locs_list if loc in Config.SUB_ISLANDS]
    return free_islands, sub_islands


def get_best_suggestions(query: str, keys: List[str], limit: int = 8) -> List[str]:
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
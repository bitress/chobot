"""Structured intent extractor for live item/villager searches.

This module provides a small function-call style extractor that returns a
structured intent dict. It is intentionally conservative and safe to use
synchronously. The implementation is designed to be easy to extend for an LLM
function-calling pipeline later, while still working locally without any API
keys.
"""
import re
from typing import Dict


def _normalize_question(question: str) -> str:
    if not question:
        return ""
    q = question.strip()
    q = re.sub(r"\s+", " ", q)
    q = q.strip("\"' ")
    return q.rstrip("?!.,")


def _clean_query(query: str) -> str:
    if not query:
        return ""
    q = query.strip().strip("\"' ")
    q = re.sub(r"\s+", " ", q)
    q = re.sub(r"^(?:a|an|the)\s+", "", q, flags=re.IGNORECASE)
    q = re.sub(r"\s+(?:on|in|for)\s+(?:any\s+)?islands?$", "", q, flags=re.IGNORECASE)
    q = re.sub(r"\s+(?:here|there)$", "", q, flags=re.IGNORECASE)
    q = q.rstrip("?!.,")
    return q.strip()


def _is_support_or_meta_question(question: str) -> bool:
    lowered = question.lower().strip()
    support_patterns = [
        r"\b(?:open|create|submit|get|start)\s+(?:a\s+)?(?:support\s+)?ticket\b",
        r"\bsupport\s+ticket\b",
        r"\bticket\b.*\b(?:help|question|assist)\b",
        r"\b(?:need|want)\s+help\b.*\b(?:wrong|mistake|worried|unsure|rule)\b",
        r"\b(?:don'?t|do\s+not)\s+(?:want\s+to\s+)?(?:do\s+)?(?:the\s+)?wrong\b",
        r"\b(?:talk|speak)\s+to\s+(?:a\s+)?(?:mod|moderator|staff|admin)\b",
        r"\bhow\s+(?:do|can)\s+i\s+(?:open|get|create|start)\s+(?:a\s+)?(?:support\s+)?ticket\b",
        r"\b(?:who|where)\s+(?:do|can)\s+i\s+(?:ask|contact)\b",
        r"\b(?:how|what|where)\s+(?:do|can|to)\s+(?:i\s+)?(?:get|obtain|buy|subscribe|gain|earn)\b.*\b(?:access|mod|privilege|ticket)\b",
        r"\b(?:command|how\s+to|what\s+(?:command|is\s+the))\b.*\b(?:check|view|see|status|statuses)\b",
        r"\b(?:check|view|see)\s+(?:island\s+)?status\b",
    ]
    if any(re.search(pattern, lowered) for pattern in support_patterns):
        return True

    if re.search(r"\bis\s+there\s+a\s+way\s+to\b", lowered):
        return not re.search(
            r"\bis\s+there\s+a\s+way\s+to\s+(?:find|get|obtain|buy|order|locate|visit|craft|make|trade|bring|invite|catch)\b",
            lowered,
        )

    return False


def extract_search_intent(question: str) -> Dict[str, str]:
    """Extract intent for live search.

    Returns a dict with keys:
      - intent: 'item', 'villager', or 'none'
      - query: cleaned search query string
      - candidates: optional list of (kind, query) pairs for ambiguous lookups
      - should_skip: True for support/meta questions that should not hit the live search API

    The extractor is intentionally conservative: prefer 'none' when uncertain.
    """
    if not question or not question.strip():
        return {"intent": "none", "query": "", "candidates": [], "should_skip": False}

    q = _normalize_question(question)
    if _is_support_or_meta_question(q):
        return {"intent": "none", "query": "", "candidates": [], "should_skip": True}

    def _canonical_query(value: str, preserve_case: bool = False) -> str:
        cleaned = _clean_query(value)
        if preserve_case:
            return cleaned
        return cleaned.lower()

    explicit_patterns = [
        (r"^!villager\s+(.+)$", "villager"),
        (r"^!(?:find|locate)\s+(.+)$", "item"),
        (r"^(?:find|search)\s+villager\s+(.+)$", "villager"),
        (r"^(?:find|search)\s+item\s+(.+)$", "item"),
    ]
    for pattern, kind in explicit_patterns:
        match = re.match(pattern, q, flags=re.IGNORECASE)
        if match:
            query = _canonical_query(match.group(1))
            return {"intent": kind, "query": query, "candidates": [(kind, query)] if query else [], "should_skip": False}

    where_match = re.match(r"^(?:where\s+is|where's|where\s+are)\s+(.+)$", q, flags=re.IGNORECASE)
    if where_match:
        query = _canonical_query(where_match.group(1), preserve_case=True)
        if query and len(query.split()) <= 4:
            lowered_query = _canonical_query(query)
            if re.search(r"\b(?:villager|villagers)\b", q, re.IGNORECASE):
                return {"intent": "villager", "query": query, "candidates": [("villager", lowered_query), ("item", lowered_query)], "should_skip": False}
            if len(query.split()) == 1 and not re.search(r"\b(?:shovel|axe|sword|dress|shirt|hat|shoes|boot|plate|chair|furniture|item|recipe|flower|fish|bug|fruit|museum|catalog|bells)\b", query, re.IGNORECASE):
                return {"intent": "villager", "query": query, "candidates": [("villager", lowered_query), ("item", lowered_query)], "should_skip": False}
            candidates = [("villager", lowered_query), ("item", lowered_query)]
            return {"intent": "none", "query": query, "candidates": candidates, "should_skip": False}

    which_island_match = re.match(r"^which\s+island\s+is\s+(.+)\s+on$", q, flags=re.IGNORECASE)
    if which_island_match:
        query = _canonical_query(which_island_match.group(1))
        if query and len(query.split()) <= 4:
            candidates = [("villager", query), ("item", query)]
            return {"intent": "none", "query": query, "candidates": candidates, "should_skip": False}

    villager_patterns = [
        (r"^(?:is|are)\s+(?:villager\s+)?(.+?)\s+(?:on\s+any\s+island|here)$", "villager"),
    ]
    for pattern, kind in villager_patterns:
        match = re.match(pattern, q, flags=re.IGNORECASE)
        if match:
            query = _canonical_query(match.group(1))
            if query:
                return {"intent": kind, "query": query, "candidates": [(kind, query)], "should_skip": False}

    item_patterns = [
        (r"^(?:do you have|does any island(?:s)? have|does any island(?:s)? stock|do any islands have|do any islands stock)\s+(.+)$", "item"),
        (r"^(?:is there\s+(?:a|any)\s+way\s+to\s+(?:find|get|obtain|buy|order|locate|visit|craft|make|trade|bring|invite|catch)\s+(.+))$", "item"),
        (r"^(?:can i find|can you find|could i find|could you find|where can i find|where can you find)\s+(.+)$", "item"),
        (r"^(?:who has|who's got|who has got)\s+(.+)$", "item"),
        (r"^(?:which islands?\s+(?:have|has|sell|stock)|what islands?\s+(?:have|has))\s+(.+)$", "item"),
        (r"^(?:find|search|look for)\s+(.+)$", "item"),
    ]
    for pattern, kind in item_patterns:
        match = re.match(pattern, q, flags=re.IGNORECASE)
        if match:
            query = _canonical_query(match.group(2) if match.lastindex and match.lastindex >= 2 else match.group(1))
            if query:
                return {"intent": kind, "query": query, "candidates": [(kind, query)], "should_skip": False}

    # Heuristics for single-token names.
    tokens = re.findall(r"\w+", q)
    if len(tokens) == 1:
        query = _canonical_query(q)
        return {"intent": "villager", "query": query, "candidates": [("villager", query)], "should_skip": False}

    return {"intent": "none", "query": _canonical_query(q), "candidates": [], "should_skip": False}

"""Structured intent extractor for live item/villager searches.

This module provides a small function-call style extractor that returns a
structured intent dict. It is intentionally conservative and safe to use
synchronously. The implementation is designed to be easy to extend for an LLM
function-calling pipeline later, while still working locally without any API
keys.
"""
import re
from typing import TypedDict


class SearchIntent(TypedDict):
    intent: str
    query: str
    candidates: list[tuple[str, str]]
    should_skip: bool


def _normalize_question(question: str) -> str:
    if not question:
        return ""
    q = question.strip()
    q = re.sub(r"\s+", " ", q)
    q = q.strip("\"' ")
    return q.rstrip("?!.,")


_NON_VILLAGER_WORDS = {
    "apple", "apples", "banana", "bananas", "orange", "oranges", "grape", "grapes",
    "carrot", "carrots", "food", "drink", "water", "fruit", "fruits", "help", "yes",
    "yeah", "y", "ok", "okay", "no", "maybe", "villager", "villagers", "person",
    "people", "thing", "things", "place", "places", "time", "times", "name", "names",
    "question", "questions", "answer", "answers", "command", "commands", "rule", "rules",
}

_ITEM_KEYWORDS = {
    "bells", "shovel", "shovel", "axe", "sword", "dress", "shirt", "hat", "shoe", "shoes",
    "boot", "boots", "plate", "chair", "furniture", "item", "items", "recipe", "recipes",
    "flower", "flowers", "fish", "bug", "bugs", "fruit", "fruits", "diy", "diys", "clothing",
    "clothes", "lamp", "table", "wall", "floor", "door", "tool", "tools", "fence", "fencing",
    "customization", "customizations", "variant", "variants",
}


def _clean_query(query: str) -> str:
    if not query:
        return ""
    q = query.strip().strip("\"' ")
    q = re.sub(r"\s+", " ", q)
    q = re.sub(r"^(?:a|an|the)\s+", "", q, flags=re.IGNORECASE)
    q = re.sub(r"^(?:villager|villagers)\s+", "", q, flags=re.IGNORECASE)
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


def _looks_like_item_query(query: str) -> bool:
    if not query:
        return False
    lowered = query.lower().strip()
    if not lowered:
        return False
    if re.match(r"^(?:villager|villagers)\b", lowered):
        return False
    tokens = re.findall(r"[a-z]+", lowered)
    if any(token in _ITEM_KEYWORDS for token in tokens):
        return True
    if any(re.search(rf"\b{re.escape(keyword)}\b", lowered) for keyword in _ITEM_KEYWORDS):
        return True
    return False


def _looks_like_single_token_villager_name(query: str) -> bool:
    if not query:
        return False
    cleaned = _clean_query(query)
    if not cleaned:
        return False
    if len(cleaned.split()) != 1:
        return False
    lower = cleaned.lower()
    if lower in _NON_VILLAGER_WORDS:
        return False
    if _looks_like_item_query(cleaned):
        return False
    return re.fullmatch(r"[a-z][a-z'-]{2,}", lower) is not None


def _build_intent(intent: str, query: str, candidates: list[tuple[str, str]] | None = None, should_skip: bool = False) -> SearchIntent:
    return {
        "intent": intent,
        "query": _clean_query(query),
        "candidates": list(candidates or []),
        "should_skip": should_skip,
    }


def extract_search_intent(question: str) -> SearchIntent:
    """Extract intent for live search.

    Returns a dict with keys:
      - intent: 'item', 'villager', or 'none'
      - query: cleaned search query string
      - candidates: optional list of (kind, query) pairs for ambiguous lookups
      - should_skip: True for support/meta questions that should not hit the live search API

    The extractor is intentionally conservative: prefer 'none' when uncertain.
    """
    if not question or not question.strip():
        return _build_intent("none", "", candidates=[], should_skip=False)

    q = _normalize_question(question)
    if _is_support_or_meta_question(q):
        return _build_intent("none", "", candidates=[], should_skip=True)

    def _canonical_query(value: str, preserve_case: bool = False) -> str:
        cleaned = _clean_query(value)
        if preserve_case:
            return cleaned
        return cleaned.lower()

    explicit_patterns = [
        (r"^!villager\s+(.+)$", "villager"),
        (r"^!(?:find|locate)\s+(.+)$", "item"),
        (r"^villager\s+(.+)$", "villager"),
        (r"^where\s+can\s+i\s+find\s+villager\s+(.+)$", "villager"),
        (r"^where\s+is\s+villager\s+(.+)$", "villager"),
        (r"^(?:find|search)\s+villager\s+(.+)$", "villager"),
        (r"^(?:find|search)\s+item\s+(.+)$", "item"),
    ]
    for pattern, kind in explicit_patterns:
        match = re.match(pattern, q, flags=re.IGNORECASE)
        if match:
            query = _canonical_query(match.group(1))
            return _build_intent(kind, query, candidates=[(kind, query)] if query else [], should_skip=False)

    where_match = re.match(r"^(?:where\s+is|where's|where\s+are)\s+(.+)$", q, flags=re.IGNORECASE)
    if where_match:
        query = _canonical_query(where_match.group(1), preserve_case=True)
        if query and len(query.split()) <= 4:
            lowered_query = _canonical_query(query)
            if re.search(r"\b(?:villager|villagers)\b", q, re.IGNORECASE):
                return _build_intent("villager", lowered_query, candidates=[], should_skip=False)
            if _looks_like_item_query(query):
                return _build_intent("none", lowered_query, candidates=[("villager", lowered_query), ("item", lowered_query)], should_skip=False)
            if len(query.split()) == 1 and _looks_like_single_token_villager_name(query):
                return _build_intent("none", lowered_query, candidates=[("villager", lowered_query), ("item", lowered_query)], should_skip=False)
            return _build_intent("none", lowered_query, candidates=[("villager", lowered_query), ("item", lowered_query)], should_skip=False)

    which_island_match = re.match(r"^which\s+island\s+is\s+(.+)\s+on$", q, flags=re.IGNORECASE)
    if which_island_match:
        query = _canonical_query(which_island_match.group(1))
        if query and len(query.split()) <= 4:
            return _build_intent("none", query, candidates=[("villager", query), ("item", query)], should_skip=False)

    villager_patterns = [
        (r"^(?:is|are)\s+(?:villager\s+)?(.+?)\s+(?:on\s+any\s+island|here)$", "villager"),
    ]
    for pattern, kind in villager_patterns:
        match = re.match(pattern, q, flags=re.IGNORECASE)
        if match:
            query = _canonical_query(match.group(1))
            if query:
                return _build_intent(kind, query, candidates=[], should_skip=False)

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
                if re.match(r"^(?:can i find|can you find|could i find|could you find|where can i find|where can you find)\s+", q, flags=re.IGNORECASE):
                    if re.match(r"^(?:villager|villagers)\b", query, flags=re.IGNORECASE):
                        query = _canonical_query(re.sub(r"^(?:villager|villagers)\b\s*", "", query, flags=re.IGNORECASE))
                        if query:
                            return _build_intent("villager", query, candidates=[], should_skip=False)
                    if _looks_like_single_token_villager_name(query):
                        return _build_intent("villager", query, candidates=[], should_skip=False)
                    if _looks_like_item_query(query):
                        return _build_intent("item", query, candidates=[], should_skip=False)
                return _build_intent(kind, query, candidates=[], should_skip=False)

    # Heuristics for single-token names.
    tokens = re.findall(r"\w+", q)
    if len(tokens) == 1:
        query = _canonical_query(q)
        if _looks_like_single_token_villager_name(query):
            return _build_intent("villager", query, candidates=[], should_skip=False)

    return _build_intent("none", q, candidates=[], should_skip=False)

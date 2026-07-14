"""LLM-based intent and live-search decision layer.

The primary path asks the configured LLM for a compact JSON search decision.
When no LLM key is configured, or the LLM returns unusable output, a local
deterministic extractor keeps item, villager, and island searches working.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TypedDict

logger = logging.getLogger("ChopaengAI")


class SearchIntent(TypedDict):
    intent: str            # "item" | "villager" | "island" | "none"
    query: str
    needs_search: bool
    candidates: list[tuple[str, str]]
    should_skip: bool


async def decide_live_search_with_llm(
    question: str,
    live_context: str,
    kb_context: str,
    history_text: str = "",
    provider: str = "openai",
    api_key: str = "",
    base_url: str = "",
    model: str = "gpt-4o-mini",
) -> SearchIntent:
    """Let the LLM decide if a live search is needed and with what query."""
    system_prompt = (
        "You are the search-decision layer for ChoBot, an ACNH community assistant. "
        "Decide if the user's question requires searching live Chopaeng data.\n\n"
        "Return ONLY a JSON object exactly matching this schema:\n"
        "{\n"
        '  "needs_search": boolean,\n'
        '  "intent": "item" | "villager" | "island" | "none",\n'
        '  "query": "the search term, empty if none"\n'
        "}\n\n"
        "Set needs_search=true when the user asks where to find an item, where a "
        "villager is, what an island has, island status, or an island theme. "
        "Use intent=item for stock/item availability, villager for villager locations, "
        "and island for island names/status/themes."
    )

    user_prompt = (
        f"Live context:\n{live_context}\n\n"
        f"Knowledge base context:\n{kb_context}\n\n"
        f"Recent conversation:\n{history_text}\n\n"
        f"Question: {question}"
    )

    content = ""
    try:
        if provider == "openai":
            from openai import OpenAI
            import asyncio

            client_kwargs = {"api_key": api_key}
            if base_url:
                client_kwargs["base_url"] = base_url
            client = OpenAI(**client_kwargs)
            loop = asyncio.get_event_loop()

            def _call_openai():
                return client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    response_format={"type": "json_object"},
                )

            response = await loop.run_in_executor(None, _call_openai)
            content = getattr(response.choices[0].message, "content", "")

        elif provider == "gemini":
            import asyncio
            import google.generativeai as genai

            genai.configure(api_key=api_key)
            gemini_model = genai.GenerativeModel(model)
            loop = asyncio.get_event_loop()

            def _call_gemini():
                return gemini_model.generate_content(
                    f"{system_prompt}\n\n{user_prompt}",
                    generation_config=genai.types.GenerationConfig(response_mime_type="application/json"),
                )

            response = await loop.run_in_executor(None, _call_gemini)
            content = response.text.strip()

    except Exception as exc:
        logger.warning("ChopaengAI: LLM intent decision failed for %s: %s", provider, exc)
        return get_live_search_intent_fallback(question)

    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError) as exc:
        logger.warning("ChopaengAI: could not parse JSON intent args: %s | Content: %s", exc, content)
        return get_live_search_intent_fallback(question)

    needs_search = bool(data.get("needs_search"))
    kind = str(data.get("intent", "none")).strip().lower()
    query = _clean_query(str(data.get("query") or "")).lower()

    if kind not in ("item", "villager", "island") or not query:
        return _no_search_intent(should_skip=not needs_search)

    return _build_intent(kind, query, should_skip=not needs_search)


def _no_search_intent(should_skip: bool = True) -> SearchIntent:
    return {"intent": "none", "query": "", "needs_search": False, "candidates": [], "should_skip": should_skip}


_NON_VILLAGER_WORDS = {
    "apple", "apples", "banana", "bananas", "orange", "oranges", "grape", "grapes",
    "carrot", "carrots", "food", "drink", "water", "fruit", "fruits", "help", "yes",
    "yeah", "y", "ok", "okay", "no", "maybe", "villager", "villagers", "person",
    "people", "thing", "things", "place", "places", "time", "times", "name", "names",
    "question", "questions", "answer", "answers", "command", "commands", "rule", "rules",
}

_ITEM_KEYWORDS = {
    "bells", "shovel", "axe", "sword", "dress", "shirt", "hat", "shoe", "shoes",
    "boot", "boots", "plate", "chair", "furniture", "item", "items", "recipe", "recipes",
    "flower", "flowers", "fish", "bug", "bugs", "fruit", "fruits", "diy", "diys",
    "clothing", "clothes", "lamp", "table", "wall", "floor", "door", "tool", "tools",
    "fence", "fencing", "customization", "customizations", "variant", "variants",
}


def _normalize_question(question: str) -> str:
    q = (question or "").strip()
    q = re.sub(r"\s+", " ", q).strip("\"' ")
    return q.rstrip("?!.,")


def _clean_query(query: str) -> str:
    q = (query or "").strip().strip("\"' ")
    q = re.sub(r"\s+", " ", q)
    q = re.sub(r"^(?:a|an|the)\s+", "", q, flags=re.IGNORECASE)
    q = re.sub(r"\s+(?:on|in|for)\s+(?:any\s+)?islands?$", "", q, flags=re.IGNORECASE)
    q = re.sub(r"\s+(?:here|there)$", "", q, flags=re.IGNORECASE)
    return q.rstrip("?!.,").strip()


def _clean_villager_query(query: str) -> str:
    return re.sub(r"^(?:villager|villagers)\s+", "", _clean_query(query), flags=re.IGNORECASE).strip()


def _build_intent(
    intent: str,
    query: str,
    candidates: list[tuple[str, str]] | None = None,
    should_skip: bool = False,
) -> SearchIntent:
    cleaned_query = _clean_query(query).lower()
    normalized_candidates = [
        (kind, _clean_query(candidate_query).lower())
        for kind, candidate_query in (candidates or [])
        if kind in {"item", "villager", "island"} and _clean_query(candidate_query)
    ]
    needs_search = bool(normalized_candidates or (intent in {"item", "villager", "island"} and cleaned_query))
    return {
        "intent": intent if needs_search and intent in {"item", "villager", "island"} else "none",
        "query": cleaned_query if needs_search else "",
        "needs_search": needs_search,
        "candidates": normalized_candidates or ([(intent, cleaned_query)] if needs_search else []),
        "should_skip": should_skip,
    }


def _is_support_or_meta_question(question: str) -> bool:
    lowered = question.lower().strip()
    support_patterns = [
        r"\b(?:open|create|submit|get|start)\s+(?:a\s+)?(?:support\s+)?ticket\b",
        r"\bsupport\s+ticket\b",
        r"\b(?:talk|speak)\s+to\s+(?:a\s+)?(?:mod|moderator|staff|admin)\b",
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
    tokens = re.findall(r"[a-z]+", (query or "").lower())
    return any(token in _ITEM_KEYWORDS for token in tokens)


def _looks_like_single_token_villager_name(query: str) -> bool:
    cleaned = _clean_query(query)
    lower = cleaned.lower()
    return (
        len(cleaned.split()) == 1
        and lower not in _NON_VILLAGER_WORDS
        and not _looks_like_item_query(cleaned)
        and re.fullmatch(r"[a-z][a-z'-]{2,}", lower) is not None
    )


def get_live_search_intent_fallback(question: str) -> SearchIntent:
    """Extract a live-search intent without an LLM."""
    if not question or not question.strip():
        return _no_search_intent(should_skip=False)

    q = _normalize_question(question)
    if _is_support_or_meta_question(q):
        return _no_search_intent(should_skip=True)

    explicit_patterns = [
        (r"^!villager\s+(.+)$", "villager"),
        (r"^!(?:find|locate)\s+(.+)$", "item"),
        (r"^villager\s+(.+)$", "villager"),
        (r"^where\s+(?:can\s+i\s+find\s+)?villager\s+(.+)$", "villager"),
        (r"^(?:find|search)\s+villager\s+(.+)$", "villager"),
        (r"^(?:find|search)\s+item\s+(.+)$", "item"),
    ]
    for pattern, kind in explicit_patterns:
        match = re.match(pattern, q, flags=re.IGNORECASE)
        if match:
            query = _clean_villager_query(match.group(1)) if kind == "villager" else _clean_query(match.group(1))
            return _build_intent(kind, query, should_skip=False)

    stock_item_match = re.match(
        r"^(?:which islands?\s+(?:have|has|sell|stock)|what islands?\s+(?:have|has))\s+(.+)$",
        q,
        flags=re.IGNORECASE,
    )
    if stock_item_match:
        query = _clean_query(stock_item_match.group(1))
        if query:
            return _build_intent("item", query, should_skip=False)

    which_island_match = re.match(r"^which\s+island\s+is\s+(.+)\s+on$", q, flags=re.IGNORECASE)
    if which_island_match:
        query = _clean_query(which_island_match.group(1))
        if query and len(query.split()) <= 4:
            return _build_intent("none", query, candidates=[("villager", query), ("item", query)], should_skip=False)

    island_patterns = [
        r"^(?:which|what)\s+islands?\s+(?:has|have|has got|is|are)\s+(?:the\s+)?(.+?)(?:\s+theme|themed)?$",
        r"^(?:find|search)\s+(?:the\s+)?islands?\s+(?:with|by)\s+(.+)$",
    ]
    for pattern in island_patterns:
        match = re.match(pattern, q, flags=re.IGNORECASE)
        if match:
            query = _clean_query(match.group(1))
            if query and len(query.split()) <= 6:
                return _build_intent("island", query, should_skip=False)

    where_match = re.match(r"^(?:where\s+is|where's|where\s+are)\s+(.+)$", q, flags=re.IGNORECASE)
    if where_match:
        query = _clean_query(where_match.group(1))
        if query and len(query.split()) <= 4:
            if re.search(r"\b(?:villager|villagers)\b", q, re.IGNORECASE):
                return _build_intent("villager", _clean_villager_query(query), should_skip=False)
            if _looks_like_single_token_villager_name(query):
                return _build_intent("none", query, candidates=[("villager", query), ("item", query)], should_skip=False)
            return _build_intent("none", query, candidates=[("villager", query), ("item", query)], should_skip=False)

    item_patterns = [
        r"^(?:do you have|does any island(?:s)? have|does any island(?:s)? stock|do any islands have|do any islands stock)\s+(.+)$",
        r"^(?:is there\s+(?:a|any)\s+way\s+to\s+(?:find|get|obtain|buy|order|locate|visit|craft|make|trade|bring|invite|catch)\s+(.+))$",
        r"^(?:can i find|can you find|could i find|could you find|where can i find|where can you find)\s+(.+)$",
        r"^(?:who has|who's got|who has got)\s+(.+)$",
        r"^(?:which islands?\s+(?:have|has|sell|stock)|what islands?\s+(?:have|has))\s+(.+)$",
        r"^(?:find|search|look for)\s+(.+)$",
    ]
    for pattern in item_patterns:
        match = re.match(pattern, q, flags=re.IGNORECASE)
        if match:
            query = _clean_query(match.group(1))
            if query:
                if re.match(r"^(?:villager|villagers)\b", query, flags=re.IGNORECASE):
                    return _build_intent("villager", _clean_villager_query(query), should_skip=False)
                if _looks_like_single_token_villager_name(query) and not _looks_like_item_query(query):
                    if re.match(
                        r"^(?:can i find|can you find|could i find|could you find|where can i find|where can you find)\s+",
                        q,
                        flags=re.IGNORECASE,
                    ):
                        return _build_intent("villager", query, should_skip=False)
                    return _build_intent("none", query, candidates=[("villager", query), ("item", query)], should_skip=False)
                return _build_intent("item", query, should_skip=False)

    tokens = re.findall(r"\w+", q)
    if len(tokens) == 1 and _looks_like_single_token_villager_name(q):
        return _build_intent("villager", q, should_skip=False)

    return _no_search_intent(should_skip=False)


def extract_search_intent(question: str) -> SearchIntent:
    """Backward-compatible name for the local fallback extractor."""
    return get_live_search_intent_fallback(question)


async def resolve_search_intent(
    question: str,
    live_context: str,
    kb_context: str,
    history_text: str = "",
    provider: str = "openai",
    api_key: str = "",
    base_url: str = "",
    model: str = "gpt-4o-mini",
) -> SearchIntent:
    """Resolve the search intent using the configured LLM or local fallback."""
    if api_key:
        return await decide_live_search_with_llm(
            question=question,
            live_context=live_context,
            kb_context=kb_context,
            history_text=history_text,
            provider=provider,
            api_key=api_key,
            base_url=base_url,
            model=model,
        )

    logger.debug("ChopaengAI: no LLM API key configured, using local intent fallback.")
    return get_live_search_intent_fallback(question)

"""LLM-based intent + live-search decision layer.

Replaces the standalone regex `extract_search_intent` classifier as the
PRIMARY path when an LLM (OpenAI or Gemini) is configured. The regex
extractor is kept only as a fallback for when no API key is present.

The core idea: instead of pre-classifying the question with regex and then
almost always hitting the live API "just in case", we give the LLM a single
tool it can call ONLY when it decides live data is actually required. The
model already has:
  - the user's question
  - recent chat history
  - the rich `live_context` block (island status, villager locations)
  - the knowledge-base context

If that's enough to answer, it just answers. If not, it calls
`search_live_data(kind, query)` itself, and we execute that specific search.
This removes the regex guesswork entirely.
"""

from __future__ import annotations

import json
import logging
from typing import Optional, TypedDict

logger = logging.getLogger("ChopaengAI")

class SearchIntent(TypedDict):
    intent: str            # "item" | "villager" | "island" | "none"
    query: str
    needs_search: bool
    candidates: list[tuple[str, str]]
    should_skip: bool

# ---------------------------------------------------------------------------
# 1. Ask the LLM whether/what to search, returning structured JSON
# ---------------------------------------------------------------------------

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
    """Let the LLM decide if a live search is needed and with what query.

    Returns structured JSON (intent, query, needs_search: bool) in one pass.
    """
    system_prompt = (
        "You are the search-decision layer for ChoBot, an ACNH community assistant. "
        "Decide if answering the user's question requires searching the live Chopaeng API "
        "for an item, villager, or island.\n\n"
        "Return ONLY a JSON object exactly matching this schema:\n"
        "{\n"
        '  "needs_search": true if the user is asking about an item, villager, or island, false ONLY if it is a general request/greeting,\n'
        '  "intent": "item" | "villager" | "island" | "none",\n'
        '  "query": "the search term (e.g., golden shovel, Raymond, Zelda), empty if none"\n'
        "}\n\n"
        "ALWAYS set needs_search to true if the user asks for an item, villager, or island theme, so we can fetch live data."
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
            client_kwargs = {"api_key": api_key}
            if base_url:
                client_kwargs["base_url"] = base_url
            client = OpenAI(**client_kwargs)
            import asyncio
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
            import google.generativeai as genai
            genai.configure(api_key=api_key)
            gemini_model = genai.GenerativeModel(model)
            import asyncio
            loop = asyncio.get_event_loop()
            
            def _call_gemini():
                return gemini_model.generate_content(
                    f"{system_prompt}\n\n{user_prompt}",
                    generation_config=genai.types.GenerationConfig(response_mime_type="application/json")
                )
            response = await loop.run_in_executor(None, _call_gemini)
            content = response.text.strip()
            
    except Exception as exc:
        logger.warning(f"ChopaengAI: LLM intent decision failed for {provider}: {exc}")
        return _no_search_intent()

    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError) as exc:
        logger.warning(f"ChopaengAI: could not parse JSON intent args: {exc} | Content: {content}")
        return _no_search_intent()

    needs_search = bool(data.get("needs_search"))
    kind = data.get("intent", "none")
    query = (data.get("query") or "").strip().lower()

    if kind not in ("item", "villager", "island") or not query:
        needs_search = False
        kind = "none"

    return {
        "intent": kind,
        "query": query,
        "needs_search": needs_search,
        "candidates": [(kind, query)] if kind != "none" else [],
        "should_skip": not needs_search,
    }

def _no_search_intent() -> SearchIntent:
    return {"intent": "none", "query": "", "needs_search": False, "candidates": [], "should_skip": True}

# ---------------------------------------------------------------------------
# 3. Regex fallback -- ONLY used when no LLM API key is configured
# ---------------------------------------------------------------------------

def get_live_search_intent_fallback(question: str):
    """Thin wrapper around the legacy regex extractor.

    Import lazily so the regex module is not even loaded on the LLM path.
    """
    from utils.intent_extractor_legacy import extract_search_intent
    return extract_search_intent(question)

# ---------------------------------------------------------------------------
# 4. Unified entry point used by the rest of the bot
# ---------------------------------------------------------------------------

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
    """Single call-site the bot should use instead of `get_live_search_intent`.

    - If an API key is configured: use LLM JSON structured output to decide.
    - Otherwise: fall back to the deterministic regex extractor.
    """
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

    logger.debug("ChopaengAI: no LLM API key configured, using regex fallback.")
    return get_live_search_intent_fallback(question)

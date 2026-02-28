"""
Chopaeng AI Module
Answers questions about the Chopaeng community using a built-in knowledge base.
Uses Google Gemini (free tier) when a GEMINI_API_KEY is configured;
falls back to keyword-based matching when no key is present.
"""

import logging
import re
from typing import Optional

logger = logging.getLogger("ChopaengAI")

# ---------------------------------------------------------------------------
# Knowledge base about Chopaeng
# ---------------------------------------------------------------------------
CHOPAENG_KNOWLEDGE = """
# About Chopaeng

Chopaeng (also written choPaeng or ChoPaeng) is a Filipino Animal Crossing:
New Horizons (ACNH) content creator and Twitch streamer. The community revolves
around visiting Chopaeng's islands to collect in-game items, trade villagers,
and enjoy a friendly, welcoming Filipino gaming space.

## Islands
There are two types of islands in the Chopaeng community:

### Sub Islands (Subscriber / VIP Islands — requires subscription or VIP role)
Alapaap, Aruga, Bahaghari, Bituin, Bonita, Dakila, Dalisay, Diwa, Gabay, Galak,
Hiraya, Kalangitan, Lakan, Likha, Malaya, Marahuyo, Pangarap, Tagumpay.

### Free Islands (Open to everyone)
Kakanggata, Kalawakan, Kundiman, Kilig, Bathala, Dalangin, Gunita, Kaulayaw,
Tala, Sinagtala, Tadhana, Maharlika, Pagsamo, Harana, Pagsuyo, Matahom,
Paraluman, Babaylan, Amihan, Silakbo, Dangal, Kariktan, Tinig, Banaag, Sinag,
Giting, Marilag.

## How to Get Items
1. Use `!find <item>` in Discord or Twitch chat to search for an item.
2. The bot shows which islands have the item right now.
3. Go to the island's channel and use `!senddodo` or `!sd` to get the Dodo code.
4. Fly over to collect your item!

## How to Visit an Island
- Type `!sd` or `!senddodo` in the island's Discord channel to receive the Dodo
  code (a 5-character code used to fly to a Nintendo Switch island in ACNH).
- Make sure you have the required role (Sub for sub islands, or no role needed
  for free islands).
- Once you land, pick up your items and leave politely.

## Villagers
Villagers are the animal residents that live on ACNH islands. You can search for
a specific villager using `!villager <name>`. Sub islands host curated villager
selections for subscribers.

## Commands
- `!find <item>` or `!locate <item>` — Find where an item is available.
- `!villager <name>` — Find a villager across islands.
- `!islandstatus` — See which islands are currently open/closed.
- `!random` — Get a random item suggestion.
- `!status` — Show bot health and last cache update time.
- `!ping` — Check the bot's response time.
- `!ask <question>` — Ask the Chopaeng AI anything about the community.
- `!help` — Show the full command list.

## Bot (Chobot)
Chobot is the custom bot built for the Chopaeng community. It syncs with a Google
Sheets database every hour to keep item and villager locations up to date. It runs
on both Twitch chat and Discord simultaneously.

## Tips
- Island names are mostly Filipino words (Tagalog / Filipino language).
- "Chopaeng" is a term of endearment from the Filipino word "paeng."
- Items rotate between islands regularly; always check `!find` before visiting.
- The Flight Logger feature automatically monitors who visits the islands and
  alerts staff if an unrecognised visitor arrives.
- If an island is offline (closed), `!senddodo` will let you know.

## Common Questions
Q: How do I get a Dodo code?
A: Go to the island's channel in Discord, type `!senddodo` or `!sd`, and the bot
   will DM you the code.

Q: What are sub islands?
A: Sub islands are premium islands available to Chopaeng subscribers or users with
   the VIP/sub role. They usually have rare items and a wider villager selection.

Q: What are free islands?
A: Free islands are open to everyone in the community, no subscription required.

Q: How often is the item list updated?
A: Chobot automatically refreshes its cache from Google Sheets every hour.

Q: What is ACNH?
A: Animal Crossing: New Horizons is a life-simulation game by Nintendo for the
   Nintendo Switch where players manage their own island, collect items, and
   visit friends' islands.
"""

# ---------------------------------------------------------------------------
# Keyword-based fallback (no API key needed)
# ---------------------------------------------------------------------------
_KB_CHUNKS = [line.strip() for line in CHOPAENG_KNOWLEDGE.splitlines() if line.strip()]


def _keyword_answer(question: str) -> str:
    """Return a best-effort answer by searching the knowledge base for relevant lines."""
    q_lower = question.lower()
    keywords = re.findall(r'\b\w{3,}\b', q_lower)

    scored: list[tuple[int, str]] = []
    for chunk in _KB_CHUNKS:
        chunk_lower = chunk.lower()
        score = sum(1 for kw in keywords if kw in chunk_lower)
        if score > 0:
            scored.append((score, chunk))

    if not scored:
        return (
            "I'm not sure about that. Try asking about islands, items, "
            "commands, or how the Chopaeng community works!"
        )

    scored.sort(key=lambda x: x[0], reverse=True)
    top_lines = [line for _, line in scored[:5]]
    answer = " ".join(top_lines)

    # Trim to a reasonable length
    if len(answer) > 400:
        answer = answer[:397] + "..."
    return answer


# ---------------------------------------------------------------------------
# Gemini-powered answer (optional – requires GEMINI_API_KEY)
# ---------------------------------------------------------------------------
async def get_ai_answer(question: str, gemini_api_key: Optional[str] = None) -> str:
    """
    Answer a question about Chopaeng.

    If *gemini_api_key* is provided, uses Google Gemini (free tier).
    Otherwise falls back to the built-in keyword search.
    """
    if not question or not question.strip():
        return "Please ask me something! e.g. `!ask how do I get items?`"

    if gemini_api_key:
        try:
            return await _gemini_answer(question.strip(), gemini_api_key)
        except Exception as e:
            logger.warning(f"[ChopaengAI] Gemini failed ({e}), using keyword fallback.")

    return _keyword_answer(question.strip())


async def _gemini_answer(question: str, api_key: str) -> str:
    """Call the Gemini API asynchronously and return the answer."""
    import google.generativeai as genai  # lazy import

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel("gemini-1.5-flash")

    prompt = (
        "You are a helpful assistant for the Chopaeng Animal Crossing community. "
        "Use ONLY the knowledge provided below to answer the user's question. "
        "Keep your answer concise (under 400 characters so it fits in Discord embeds and Twitch chat). "
        "If the answer is not in the knowledge base, say you don't know.\n\n"
        f"### Chopaeng Knowledge Base ###\n{CHOPAENG_KNOWLEDGE}\n\n"
        f"### User Question ###\n{question}"
    )

    # Gemini's generate_content is synchronous; run it in a thread to avoid blocking.
    import asyncio
    loop = asyncio.get_event_loop()
    response = await loop.run_in_executor(
        None, lambda: model.generate_content(prompt)
    )
    text = response.text.strip()
    return text if text else _keyword_answer(question)

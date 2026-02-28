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
New Horizons (ACNH) content creator, Twitch streamer, and community host based
in the Philippines. He is known for his warm, inclusive, and fun streams where
chat members visit his islands to collect rare items and meet cute villagers.
His streams are family-friendly and welcoming to both veterans and newcomers
to Animal Crossing. The Chopaeng community is sometimes called the "choPaeng
family" — a tight-knit group of Filipino and international ACNH fans.

## Chopaeng's Twitch & Socials
- Twitch channel: twitch.tv/chopaeng
- The Twitch community can use `!find`, `!villager`, `!ask`, and other bot
  commands directly in Twitch chat.

## The ChoPaeng Discord Server
The official Discord server is the main hub for the community. It contains:
- Island channels for each of the 18 sub islands and 27 free islands.
- An ordering system for requesting specific items or villagers.
- Staff channels for moderation and flight logging.
- Announcements for when islands are open or new items arrive.

## Islands
There are two types of islands in the Chopaeng community:

### Sub Islands (Subscriber / VIP Islands — requires subscription or VIP role)
There are 18 sub islands. Island names are Filipino / Tagalog words:
Alapaap (cloud), Aruga (care), Bahaghari (rainbow), Bituin (star),
Bonita (beautiful), Dakila (great/noble), Dalisay (pure), Diwa (spirit/essence),
Gabay (guide), Galak (joy), Hiraya (dreams come true), Kalangitan (sky/heavens),
Lakan (nobleman), Likha (creation/art), Malaya (free), Marahuyo (enchanted),
Pangarap (dream), Tagumpay (success/victory).

### Free Islands (Open to everyone)
There are 27 free islands, also named in Filipino / Tagalog:
Kakanggata, Kalawakan (outer space), Kundiman (love song), Kilig (giddy/excited),
Bathala (supreme being), Dalangin (prayer), Gunita (memory), Kaulayaw (beloved),
Tala (bright star), Sinagtala (moonlight star), Tadhana (destiny/fate),
Maharlika (noble/freedom), Pagsamo (pleading), Harana (serenade), Pagsuyo (love/devotion),
Matahom (beautiful — Bisaya), Paraluman (muse/guiding star), Babaylan (shaman/healer),
Amihan (north wind/cool breeze), Silakbo (outburst of emotion), Dangal (honor/dignity),
Kariktan (beauty/charm), Tinig (voice/sound), Banaag (glimmer of light),
Sinag (ray of light/moonbeam), Giting (bravery/valor), Marilag (magnificent/radiant).

## How to Get Items
1. Use `!find <item>` in Discord or Twitch chat to search for an item.
2. The bot shows which islands currently have the item.
3. Go to that island's channel in Discord and use `!senddodo` or `!sd` to get
   the Dodo code sent to your DMs.
4. Open Animal Crossing, go to Dodo Airlines, and fly using the code.
5. Collect your items and return home politely — do NOT take items you did not
   request, and avoid shaking trees or picking flowers without permission.

## Visitor Etiquette / Rules
- Only pick up items assigned to you or items that are clearly free to take.
- Do not run over flowers or dig up trees.
- Do not talk to residents to lure them away.
- Leave as soon as you are done — do not linger on the island.
- Be friendly and thankful in chat!
- Breaking rules may result in a warning or ban from the community.

## How to Visit an Island
- Type `!sd` or `!senddodo` in the island's Discord channel to receive the Dodo
  code (a 5-character alphanumeric code used to fly to a Nintendo Switch island).
- Make sure you have the required role:
  * Sub islands require a Subscriber or VIP role.
  * Free islands are open to everyone — no role needed.
- Once you land, pick up your items and leave politely.

## Ordering Items / Villagers
- If an item you want is not currently on any island, you can place an order.
- Use the orderbot (mention or DM the orderbot role in the Discord server) to
  request a specific item or villager to be stocked on an island for you.
- Check the #ordering channel in the Discord for ordering instructions.
- VIP / subscriber members get priority access to sub island stocks.

## Villagers
Villagers are the animal residents that live on ACNH islands. You can search for
a specific villager using `!villager <name>`. Sub islands host curated villager
selections for subscribers. Popular villagers (like Raymond, Marshal, Judy, etc.)
often appear on sub islands first. Use `!find` for items; use `!villager` for
animal residents.

## Commands
- `!find <item>` or `!locate <item>` — Find where an item is available right now.
- `!villager <name>` — Find a villager across islands.
- `!islandstatus` — See which of the 18 sub island bots are currently online.
- `!random` — Get a random item suggestion with its current island location.
- `!status` — Show bot health, cache size, and last update time.
- `!ping` — Check the bot's response time.
- `!ask <question>` — Ask the Chopaeng AI anything about the community.
- `!help` — Show the full command list with descriptions.
- `!senddodo` or `!sd` — Get the Dodo code for a sub island (use in island channel).
- `!visitors` — Check current visitors on a sub island (use in island channel).
- `!refresh` — Manually refresh the item cache (Admin only).

## Bot (Chobot)
Chobot is the custom bot built specifically for the Chopaeng community by bitress.
It syncs with a Google Sheets database every hour to keep item and villager
locations up to date across all 45 islands. It runs on both Twitch chat and
Discord simultaneously, and includes a Flight Logger that monitors island visitors
in real time.

## Flight Logger (Safety Feature)
The Flight Logger is an automatic safety feature in the Discord bot. When someone
visits a sub island, the bot logs their arrival. If the visitor is not recognised
(never visited before or has a bad history), it sends an alert to staff. Staff
can then Admit, Warn, Kick, or Ban the visitor using on-screen buttons. This
keeps the community's islands safe.

## Tips
- Island names are Filipino / Tagalog words — each name has a meaningful translation.
- "Chopaeng" is a playful term of endearment from the Filipino word "paeng."
- Items rotate between islands regularly; always check `!find` before visiting.
- The bot's cache refreshes every hour, so newly added items appear quickly.
- If an island is offline (closed), `!senddodo` will tell you instead of sending a code.
- Free islands are great for newcomers; sub islands have rarer stocks.
- You can use slash commands (e.g., `/find`, `/villager`, `/ask`) in Discord as
  an alternative to prefix commands.

## Common Questions
Q: How do I get a Dodo code?
A: Go to the island's channel in Discord, type !senddodo or !sd, and the bot
   will DM you the 5-character Dodo code.

Q: What are sub islands?
A: Sub islands are 18 premium islands available to Chopaeng subscribers or users
   with the VIP/sub role. They usually have rare items and a curated villager list.

Q: What are free islands?
A: Free islands are 27 islands open to everyone in the community — no subscription
   or special role needed. Great for beginners!

Q: How often is the item list updated?
A: Chobot automatically refreshes its cache from Google Sheets every hour.

Q: How do I order a specific item?
A: Use the orderbot in the Discord server. Check the ordering channel for details.
   Mention the orderbot role with the item name to place your request.

Q: What is ACNH?
A: Animal Crossing: New Horizons is a life-simulation game by Nintendo for the
   Nintendo Switch. Players manage their own island paradise, collect furniture
   and clothing items, invite animal villagers, and visit friends' islands.

Q: Can I use the bot on Twitch?
A: Yes! All search commands (!find, !villager, !random, !status, !ask) work in
   Chopaeng's Twitch chat as well as in Discord.

Q: Who made Chobot?
A: Chobot was built by bitress, a developer in the Chopaeng community. It is
   open-source on GitHub at github.com/bitress/chobot.

Q: What does "Hiraya Manawari" mean?
A: It is a Filipino phrase meaning "may the wishes of your heart be granted" or
   "dreams come true." Hiraya is also the name of one of the sub islands.

Q: What is the Flight Logger?
A: The Flight Logger is an automatic safety feature. When someone visits a sub
   island, the bot logs their arrival. If the visitor is unrecognised, staff get
   an alert with buttons to Admit, Warn, Kick, or Ban the visitor.

Q: What are the visitor rules or etiquette?
A: Only pick up items assigned to you. Do not run over flowers, dig up trees, or
   talk to residents to lure them away. Leave as soon as you are done and be
   friendly in chat — breaking rules may result in a ban.

Q: What commands are available?
A: Core commands are !find (search items), !villager (search villagers), !senddodo
   or !sd (get a Dodo code), !ask (ask the AI), !random, !status, !ping, and !help.
   Admin-only: !refresh.
"""

# ---------------------------------------------------------------------------
# Keyword-based fallback (no API key needed)
# ---------------------------------------------------------------------------

# Common question/filler words excluded from scoring so topic keywords drive matching.
_STOPWORDS = {
    'who', 'what', 'how', 'why', 'when', 'where', 'which', 'does',
    'did', 'are', 'the', 'can', 'could', 'would', 'should', 'its',
    'this', 'that', 'these', 'those', 'and', 'but', 'for', 'with',
    'have', 'has', 'was', 'were', 'been', 'get', 'got', 'use',
}


def _parse_kb() -> tuple[list[tuple[str, str]], list[str]]:
    """Parse the knowledge base into Q&A pairs and prose paragraphs."""
    # Extract Q&A pairs: Q: … \n A: … (possibly multi-line until next Q:, section, or end)
    qa_pattern = re.compile(
        r'Q:\s*(.+?)\nA:\s*(.+?)(?=\nQ:|\n##|\n#|\Z)',
        re.DOTALL,
    )
    qa_pairs = []
    for m in qa_pattern.finditer(CHOPAENG_KNOWLEDGE):
        q = ' '.join(m.group(1).split())
        a = ' '.join(m.group(2).split())
        qa_pairs.append((q, a))

    # Extract prose paragraphs (blank-line separated), skipping headers and Q&A lines
    prose_paragraphs = []
    for para in re.split(r'\n\s*\n', CHOPAENG_KNOWLEDGE):
        lines = [
            ln.strip() for ln in para.strip().splitlines()
            if ln.strip()
            and not ln.strip().startswith('#')
            and not ln.strip().startswith('Q:')
            and not ln.strip().startswith('A:')
        ]
        if lines:
            prose_paragraphs.append(' '.join(lines))

    return qa_pairs, prose_paragraphs


_KB_QA_PAIRS, _KB_PROSE = _parse_kb()


def _wb_match(keyword: str, text: str) -> bool:
    """Return True if *keyword* appears as a whole word in *text*."""
    return bool(re.search(rf'\b{re.escape(keyword)}\b', text))


def _trim_to_sentences(text: str, n: int = 3) -> str:
    """Return at most *n* complete sentences from *text*."""
    # Split on sentence-ending punctuation followed by whitespace or end-of-string.
    sentences = re.split(r'(?<=[.!?])\s+', text.strip())
    trimmed = ' '.join(sentences[:n])
    return trimmed


def _keyword_answer(question: str) -> str:
    """Return a clean answer by matching Q&A pairs first, then prose paragraphs."""
    q_lower = question.lower()
    all_words = re.findall(r'\b\w{3,}\b', q_lower)
    keywords = [w for w in all_words if w not in _STOPWORDS] or all_words

    if not keywords:
        return (
            "I'm not sure about that. Try asking about islands, items, "
            "commands, or how the Chopaeng community works!"
        )

    # 1. Score Q&A pairs against the question text; return the best answer text.
    best_qa_score = 0
    best_qa_answer = ''
    for q_text, a_text in _KB_QA_PAIRS:
        q_kb_lower = q_text.lower()
        score = sum(1 for kw in keywords if _wb_match(kw, q_kb_lower))
        if score > best_qa_score:
            best_qa_score = score
            best_qa_answer = a_text

    if best_qa_score > 0:
        return _trim_to_sentences(best_qa_answer)

    # 2. Fall back to the best matching prose paragraph.
    best_prose_score = 0
    best_prose = ''
    for para in _KB_PROSE:
        para_lower = para.lower()
        score = sum(1 for kw in keywords if _wb_match(kw, para_lower))
        if score > best_prose_score:
            best_prose_score = score
            best_prose = para

    if best_prose_score > 0:
        return _trim_to_sentences(best_prose)

    return (
        "I'm not sure about that. Try asking about islands, items, "
        "commands, or how the Chopaeng community works!"
    )


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
        "Reply in plain text (no markdown, no embeds). "
        "Keep your answer to 2–3 sentences maximum. "
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

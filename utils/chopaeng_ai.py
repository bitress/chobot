"""
Chopaeng AI Module
Answers questions about the Chopaeng community using internal reference guides and live APIs.
Uses OpenAI or Google Gemini when API keys are configured;
falls back to keyword-based matching when no key is present.
"""

import collections
import json
import logging
import os
import re
import threading
import time
from typing import Optional

logger = logging.getLogger("ChopaengAI")

# Path to the JSON file used to persist the rolling chat-log across restarts.
# Lives in the project root (same directory as chobot.db).
_CHAT_LOG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "chat_log.json",
)

# ---------------------------------------------------------------------------
# Live API endpoints + cache
# ---------------------------------------------------------------------------
_ISLANDS_API_URL   = "https://console.chopaeng.com/api/islands"
_VILLAGERS_API_URL = "https://console.chopaeng.com/api/villagers/list"
_FIND_ITEM_API_URL = "https://console.chopaeng.com/api/find"
_FIND_VILLAGER_API_URL = "https://console.chopaeng.com/api/villager"
_LIVE_CACHE_TTL    = 300  # seconds — refresh every 5 minutes
_REQUEST_HELP_CHANNEL = "782872507551055892"

_live_cache: dict = {
    "islands":    None,
    "villagers":  None,
    "fetched_at": 0.0,
}


async def _fetch_live_data() -> None:
    """Fetch island and villager data from the console API and update the in-memory cache."""
    import aiohttp
    import asyncio

    async def _get(session: "aiohttp.ClientSession", url: str) -> dict:
        timeout = aiohttp.ClientTimeout(total=10)
        async with session.get(url, timeout=timeout) as resp:
            resp.raise_for_status()
            return await resp.json()

    try:
        async with aiohttp.ClientSession() as session:
            islands_data, villagers_data = await asyncio.gather(
                _get(session, _ISLANDS_API_URL),
                _get(session, _VILLAGERS_API_URL),
            )
        _live_cache["islands"]    = islands_data
        _live_cache["villagers"]  = villagers_data
        _live_cache["fetched_at"] = time.time()
        logger.debug("[ChopaengAI] Live data refreshed from console API.")
    except Exception as exc:
        logger.warning(f"[ChopaengAI] Failed to fetch live data: {exc}")


def _build_live_context() -> str:
    """Format cached live API data into a compact text block for the LLM prompt."""
    islands_data   = _live_cache.get("islands")
    villagers_data = _live_cache.get("villagers")
    parts: list[str] = []

    # --- Island status section ---
    if islands_data and isinstance(islands_data.get("data"), list):
        lines = ["## Live Island Status"]
        for island in islands_data["data"]:
            name     = island.get("name", "")
            status   = island.get("status", "UNKNOWN")
            itype    = island.get("type", "")
            cat      = island.get("cat", "")
            visitors = island.get("visitors", 0)
            items    = island.get("items") or []
            bot_up   = island.get("discord_bot_online")

            # Skip internal/dummy entries
            if not name or name.upper().startswith("ZX"):
                continue

            items_preview = ", ".join(items[:6]) + ("…" if len(items) > 6 else "")
            vis_str  = f" | Visitors: {visitors}" if visitors else ""
            line = f"- {name} [{status}] ({itype or cat})"
            if items_preview:
                line += f" — {items_preview}"
            line += vis_str
            lines.append(line)
        parts.append("\n".join(lines))

    # --- Villager locations section (inverted: villager → islands) ---
    if villagers_data and isinstance(villagers_data.get("islands"), dict):
        villager_map: dict[str, list[str]] = {}
        for island_name, v_list in villagers_data["islands"].items():
            for v in (v_list or []):
                # Skip placeholder entries like "Non00" or "?Toile"
                if v and not v.startswith("Non") and not v.startswith("?"):
                    villager_map.setdefault(v, []).append(island_name)

        lines = ["## Live Villager Locations"]
        for villager, island_names in sorted(villager_map.items()):
            lines.append(f"- {villager}: {', '.join(island_names)}")
        parts.append("\n".join(lines))

    return "\n\n".join(parts)


def _extract_live_search_candidates(question: str) -> list[tuple[str, str]]:
    """Infer item/villager live-search queries from natural language prompts."""
    q = question.strip()
    lowered = q.lower().strip().rstrip("?!.,")
    candidates: list[tuple[str, str]] = []

    patterns: list[tuple[str, str, str]] = [
        ("villager", r"^!villager\s+(.+)$", "explicit villager command"),
        ("item", r"^!(?:find|locate)\s+(.+)$", "explicit item command"),
        ("villager", r"^(?:find|search)\s+villager\s+(.+)$", "villager search phrase"),
        ("item", r"^(?:find|search)\s+item\s+(.+)$", "item search phrase"),
        (
            "item",
            r"^(?:do\s+you\s+have|is\s+there\s+(?:any\s+)?)(?!a\s+way\s+to\b)(.+)$",
            "do you have item",
        ),
        ("item", r"^does\s+any\s+island\s+have\s+(.+)$", "does any island have item"),
        ("item", r"^does\s+any\s+island\s+stock\s+(.+)$", "does any island stock item"),
        ("item", r"^can\s+i\s+find\s+(.+?)\s+on\s+any\s+island$", "can I find item on any island"),
        ("item", r"^can\s+i\s+find\s+(.+)$", "can I find item"),
        ("item", r"^which\s+islands?\s+(?:has|have)\s+(.+)$", "which islands have item"),
        ("item", r"^which\s+islands?\s+(?:sell|stock)\s+(.+)$", "which islands stock item"),
        ("item", r"^who\s+has\s+(.+)$", "who has item"),
        ("item", r"^what\s+islands?\s+(?:has|have)\s+(.+)$", "what islands have item"),
        ("item", r"^where\s+can\s+i\s+find\s+(.+)$", "where can I find"),
        ("villager", r"^where\s+is\s+villager\s+(.+)$", "where is villager"),
        ("villager", r"^is\s+(.+)\s+(?:on\s+any\s+island|here)$", "is villager on any island"),
        ("villager", r"^villager\s+(.+)$", "short villager query"),
    ]

    for kind, pattern, _reason in patterns:
        match = re.match(pattern, lowered, re.IGNORECASE)
        if match:
            query = match.group(1).strip(" '")
            if query:
                candidates.append((kind, query))
            break

    if not candidates:
        match = re.match(r"^where\s+is\s+(.+)$", lowered, re.IGNORECASE)
        if match:
            query = match.group(1).strip(" '")
            if query and len(query.split()) <= 4:
                candidates.append(("villager", query))
                candidates.append(("item", query))

    if not candidates:
        match = re.match(r"^which\s+islands?\s+is\s+(.+)\s+on$", lowered, re.IGNORECASE)
        if match:
            query = match.group(1).strip(" '")
            if query and len(query.split()) <= 4:
                candidates.append(("villager", query))
                candidates.append(("item", query))

    if not candidates:
        match = re.match(r"^(?:find|search)\s+(.+)$", lowered, re.IGNORECASE)
        if match:
            query = match.group(1).strip(" '")
            if query and len(query.split()) <= 4 and "how to" not in lowered:
                candidates.append(("item", query))

    deduped: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for kind, query in candidates:
        key = (kind, query.lower())
        if key not in seen:
            deduped.append((kind, query))
            seen.add(key)
    return deduped


def _should_skip_live_search(question: str) -> bool:
    """True for ticket/support/meta questions that must not hit the item/villager search API.

    Prevents phrases like 'Is there a way to open a ticket?' from being parsed as
    an item lookup ('is there' + rest matched as catalog search).
    """
    lowered = question.lower().strip()

    # Support, tickets, staff — not item catalog.
    if re.search(
        r"\b(?:"
        r"open|create|submit|get|start"
        r")\s+(?:a\s+)?(?:support\s+)?ticket\b",
        lowered,
    ):
        return True
    if re.search(
        r"\bsupport\s+ticket\b|\bticket\b.*\b(?:help|question|assist)\b|"
        r"\b(?:need|want)\s+help\b.*\b(?:wrong|mistake|worried|unsure|rule)\b|"
        r"\b(?:don'?t|do\s+not)\s+(?:want\s+to\s+)?(?:do\s+)?(?:the\s+)?wrong\b|"
        r"\b(?:talk|speak)\s+to\s+(?:a\s+)?(?:mod|moderator|staff|admin)\b|"
        r"\bhow\s+(?:do|can)\s+i\s+(?:open|get|create|start)\s+(?:a\s+)?(?:support\s+)?ticket\b|"
        r"\b(?:who|where)\s+(?:do|can)\s+i\s+(?:ask|contact)\b",
        lowered,
    ):
        return True

    # "Is there a way to …" — usually meta (unless clearly about finding items).
    if re.search(r"\bis\s+there\s+a\s+way\s+to\b", lowered):
        if re.search(
            r"is\s+there\s+a\s+way\s+to\s+(?:find|get|obtain|buy|order|visit|craft|make|"
            r"locate|trade|bring|invite|catch)\b",
            lowered,
        ):
            return False
        return True

    return False


async def _search_live_api(kind: str, query: str) -> Optional[dict]:
    """Query the live item/villager search endpoint."""
    import aiohttp

    url = _FIND_VILLAGER_API_URL if kind == "villager" else _FIND_ITEM_API_URL

    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, params={"q": query}) as resp:
                resp.raise_for_status()
                return await resp.json()
    except Exception as exc:
        logger.warning(f"[ChopaengAI] Live {kind} search failed for '{query}': {exc}")
        return None


def _format_island_groups(free_islands: list[str], sub_islands: list[str]) -> str:
    """Return a compact island summary split by free and sub islands."""
    parts: list[str] = []
    if free_islands:
        label = "these Free Islands" if len(free_islands) > 1 else "this Free Island"
        parts.append(f"{label}: {' | '.join(name.upper() for name in free_islands)}")
    if sub_islands:
        label = "these Sub Islands" if len(sub_islands) > 1 else "this Sub Island"
        parts.append(f"{label}: {' | '.join(name.upper() for name in sub_islands)}")
    return " and on ".join(parts)


def _format_live_search_answer(kind: str, query: str, payload: dict) -> str:
    """Convert a live search API payload into a user-facing answer."""
    normalized_query = query.strip().upper()
    results = payload.get("results") or {}
    free_islands = results.get("free") or []
    sub_islands = results.get("sub") or []
    suggestions = payload.get("suggestions") or []

    if payload.get("found") and (free_islands or sub_islands):
        subject = "villager" if kind == "villager" else "item"
        island_summary = _format_island_groups(free_islands, sub_islands)
        return f"I found {subject} {normalized_query} on {island_summary}."

    if suggestions:
        return (
            f"I couldn't find {normalized_query} right now. "
            f"Did you mean: {', '.join(str(s) for s in suggestions)}?"
        )

    if kind == "item":
        return (
            f"I couldn't find item {normalized_query} right now. "
            f"If it's not stocked, you can use the Chorder Bot flow in <#1175672083183829075>."
        )

    return (
        f"I couldn't find villager {normalized_query} right now. "
        f"If you need request help, check <#782872507551055892> (subs) or <#1175704849409654804> (non-subs)."
    )


async def _try_live_search_answer(question: str) -> Optional[str]:
    """Return a direct live-search answer for item/villager lookup questions."""
    if _should_skip_live_search(question):
        return None

    last_payload: Optional[dict] = None
    last_kind: Optional[str] = None
    last_query: Optional[str] = None

    for kind, query in _extract_live_search_candidates(question):
        payload = await _search_live_api(kind, query)
        if not payload:
            continue

        last_payload = payload
        last_kind = kind
        last_query = query

        if payload.get("found"):
            return _format_live_search_answer(kind, query, payload)

        if payload.get("suggestions"):
            return _format_live_search_answer(kind, query, payload)

    if last_payload and last_kind and last_query:
        return _format_live_search_answer(last_kind, last_query, last_payload)

    return None

# ---------------------------------------------------------------------------
# Conversation history store
# ---------------------------------------------------------------------------
_MAX_HISTORY_TURNS = 5   # keep last 5 exchanges (10 messages) per conversation
_HISTORY_TTL       = 600  # seconds — reset after 10 minutes of inactivity


class ConversationStore:
    """
    In-memory per-user conversation history with TTL expiry.

    Keys are arbitrary strings (e.g. ``"guild:channel:user"``).
    Each value is a list of ``{"role": "user"|"assistant", "content": str}``
    dicts stored in chronological order, capped at *_MAX_HISTORY_TURNS*
    exchanges (2 x _MAX_HISTORY_TURNS messages).
    """

    def __init__(self):
        self._store: dict[str, dict] = {}

    def _is_expired(self, key: str) -> bool:
        entry = self._store.get(key)
        return entry is not None and time.time() - entry["last_active"] > _HISTORY_TTL

    def get(self, key: str) -> list[dict]:
        """Return conversation history for *key* (empty list if none / expired)."""
        if self._is_expired(key):
            del self._store[key]
        entry = self._store.get(key)
        return list(entry["turns"]) if entry else []

    def add(self, key: str, user_msg: str, bot_reply: str) -> None:
        """Append a user/assistant exchange and trim to *_MAX_HISTORY_TURNS*."""
        if self._is_expired(key):
            del self._store[key]
        if key not in self._store:
            self._store[key] = {"turns": [], "last_active": time.time()}
        turns = self._store[key]["turns"]
        turns.append({"role": "user",      "content": user_msg})
        turns.append({"role": "assistant", "content": bot_reply})
        max_msgs = _MAX_HISTORY_TURNS * 2
        if len(turns) > max_msgs:
            self._store[key]["turns"] = turns[-max_msgs:]
        self._store[key]["last_active"] = time.time()

    def clear(self, key: str) -> None:
        """Discard all history for *key*."""
        self._store.pop(key, None)


# Module-level singleton used by get_ai_answer and the bot modules.
conversation_store = ConversationStore()

# ---------------------------------------------------------------------------
# Rolling chat-log learned from a designated Discord channel
# ---------------------------------------------------------------------------
_CHAT_LOG_MAX = 50    # keep the most recent N messages
_CHAT_LOG_MAX_LEN = 500  # max characters per message stored

_chat_log_lock = threading.Lock()
_chat_log_last_save: float = 0.0   # Unix timestamp of last successful disk write
_CHAT_LOG_SAVE_MIN_INTERVAL = 1.0  # minimum seconds between disk writes


def _load_chat_log() -> collections.deque:
    """Load the persisted chat-log from disk, or return an empty deque on error."""
    try:
        with open(_CHAT_LOG_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, list):
            dq = collections.deque(maxlen=_CHAT_LOG_MAX)
            for entry in data[-_CHAT_LOG_MAX:]:
                if isinstance(entry, dict) and "author" in entry and "content" in entry:
                    dq.append(entry)
            logger.info(f"[ChopaengAI] Chat-log loaded from disk ({len(dq)} messages).")
            return dq
    except FileNotFoundError:
        pass
    except Exception as exc:
        logger.warning(f"[ChopaengAI] Could not load chat-log from {_CHAT_LOG_PATH}: {exc}")
    return collections.deque(maxlen=_CHAT_LOG_MAX)


def _save_chat_log(snapshot: list) -> None:
    """Atomically write *snapshot* to the chat-log JSON file."""
    tmp_path = _CHAT_LOG_PATH + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(snapshot, fh, ensure_ascii=False)
        os.replace(tmp_path, _CHAT_LOG_PATH)
    except Exception as exc:
        logger.warning(f"[ChopaengAI] Could not persist chat-log: {exc}")


# Initialise from disk at import time so the log survives bot restarts.
_chat_log: collections.deque = _load_chat_log()


def add_chat_message(author: str, content: str) -> None:
    """Append a message from the learn-channel to the rolling chat log and persist it.

    Disk writes are throttled to at most once per *_CHAT_LOG_SAVE_MIN_INTERVAL* seconds
    to avoid excessive I/O in high-traffic channels.
    """
    global _chat_log_last_save
    if not content or not content.strip():
        return
    safe_author = str(author)[:100].replace("\n", " ").replace("\r", " ")
    safe_content = content.strip()[:_CHAT_LOG_MAX_LEN].replace("\n", " ").replace("\r", " ")
    with _chat_log_lock:
        _chat_log.append({"author": safe_author, "content": safe_content})
        snapshot = list(_chat_log)
        now = time.monotonic()
        due_for_save = (now - _chat_log_last_save) >= _CHAT_LOG_SAVE_MIN_INTERVAL
        if due_for_save:
            _chat_log_last_save = now
    if due_for_save:
        _save_chat_log(snapshot)


def _build_chat_log_context() -> str:
    """Format the rolling chat log into a compact text block for the LLM prompt."""
    with _chat_log_lock:
        snapshot = list(_chat_log)
    if not snapshot:
        return ""
    lines = [f"{entry['author']}: {entry['content']}" for entry in snapshot]
    return "\n".join(lines)


_KB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "knowledge_base.md")

try:
    with open(_KB_FILE, encoding="utf-8") as _f:
        CHOPAENG_KNOWLEDGE = _f.read()
except OSError:
    logger.error(
        f"[ChopaengAI] knowledge_base.md not found at {_KB_FILE}. "
        "AI answers will lack community context."
    )
    CHOPAENG_KNOWLEDGE = ""


# ---------------------------------------------------------------------------
# Greeting detection helpers
# ---------------------------------------------------------------------------

_GREETINGS = {
    'hi', 'hello', 'hey', 'hiya', 'heya', 'sup', 'yo', 'howdy',
    'good morning', 'good afternoon', 'good evening', 'good night',
    'greetings', 'wassup', 'whats up', "what's up", 'helo', 'ello',
    'hoi', 'konnichiwa', 'mabuhay',
}

# Filler words that may follow a greeting and are still just a greeting.
_GREETING_FILLERS = {'there', 'everyone', 'all', 'guys', 'folks', 'friends', 'po', 'ate', 'kuya'}

_GREETING_RESPONSE = (
    "Hello! I am ChoBot! 🌟 "
    "How can I help you today? Are you looking for a specific item, "
    "or have a question about the islands?"
)


def _is_greeting(text: str) -> bool:
    """Return True if *text* is a greeting with no substantive question."""
    t = text.lower().strip().rstrip('!.,?')
    for g in _GREETINGS:
        if t == g or t.startswith(g + ' ') or t.startswith(g + '!'):
            # Check if the remainder is only emoji/punctuation or known filler words.
            remainder = t[len(g):].strip().strip('!.,?').strip()
            if not remainder:
                return True
            # All-emoji/symbol remainder
            if all(not c.isalpha() for c in remainder):
                return True
            # Remainder is one or more known filler words
            if all(w in _GREETING_FILLERS for w in remainder.split()):
                return True
    return False


# ---------------------------------------------------------------------------
# Vague request detection
# ---------------------------------------------------------------------------

_VAGUE_REQUESTS = {
    'help', 'help me', 'i need help', 'need help', 'can you help',
    'can you help me', 'i need assistance', 'assist me', 'assistance',
    'i have a question', 'question', 'support',
}

_VAGUE_RESPONSE = (
    "I'm here to help! What are you having trouble with? "
    "Let me know if you need help finding items, understanding the rules, or getting a Dodo code."
)


def _is_vague_request(text: str) -> bool:
    """Return True if *text* is a vague help request with no specific topic."""
    t = text.lower().strip().rstrip('!.,?')
    return t in _VAGUE_REQUESTS


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


def _parse_kb() -> list[tuple[str, str]]:
    """Parse the knowledge base into (heading, content) section pairs.

    Each section is keyed by its nearest Markdown heading.  Table rows and
    bullet points are included in the section text so the keyword scorer
    can match against them.
    """
    sections: list[tuple[str, str]] = []
    current_heading = "General"
    current_lines: list[str] = []

    for line in CHOPAENG_KNOWLEDGE.splitlines():
        stripped = line.strip()
        if stripped.startswith('#'):
            # Flush previous section
            if current_lines:
                sections.append((current_heading, ' '.join(current_lines)))
                current_lines = []
            current_heading = stripped.lstrip('#').strip()
        elif stripped and not re.match(r'^[\|\-\s:]+$', stripped):
            # Include table rows (strip leading |), bullets, and prose.
            # Skip table separator rows (e.g. |---|---|).
            clean = stripped.lstrip('|-').strip()
            if clean:
                current_lines.append(clean)

    if current_lines:
        sections.append((current_heading, ' '.join(current_lines)))

    return sections


_KB_SECTIONS = _parse_kb()


def _wb_match(keyword: str, text: str) -> bool:
    """Return True if *keyword* appears as a whole word in *text*."""
    return bool(re.search(rf'\b{re.escape(keyword)}\b', text))


def _trim_to_sentences(text: str, n: int = 3) -> str:
    """Return at most *n* complete sentences from *text*.

    Splits on sentence-ending punctuation followed by whitespace, but skips
    splits where the period is preceded by a digit (numbered list markers like
    ``1. ``, ``2. ``).
    """
    # Use a 2-char lookbehind: char before '.' must be a non-digit letter.
    sentences = re.split(r'(?<=[^\d\s][.!?])\s+', text.strip())
    trimmed = ' '.join(sentences[:n])
    return trimmed


def _auto_link_channels(text: str) -> str:
    """Automatically convert raw 17-20 digit Discord channel IDs into <#ID> links.
    
    Skips IDs that are already part of a mention (<#ID>, <@ID>, etc.) or look like
    part of a URL or path.
    """
    if not text:
        return text
    
    # Matches URLs, existing Discord tags <...>, or markdown links [text](url) to skip them.
    # Group 2 matches the raw 17-20 digit channel ID we want to replace.
    pattern = r'(https?://\S+|<[^>]+>|\[.*?\]\(.*?\))|(\b\d{17,20}\b)'
    
    def repl(m: re.Match) -> str:
        if m.group(1):
            return str(m.group(1))
        return f"<#{m.group(2)}>"
        
    return re.sub(pattern, repl, text)


def _keyword_answer(question: str, history: Optional[list[dict]] = None) -> str:
    """Return a clean answer by matching knowledge base sections.

    Scores each section by how many query keywords appear in both the heading
    and body text.  Heading matches are weighted 2× to prefer topically
    relevant sections.

    When *history* is provided and the question is short / vague (≤ 5 words),
    the last user message is prepended so the keyword scorer has more context.
    """
    # Augment a short follow-up with the most recent user turn for better matching.
    effective_question = question
    if history and len(question.split()) <= 5:
        last_user = next(
            (t["content"] for t in reversed(history) if t["role"] == "user"),
            None,
        )
        if last_user:
            effective_question = f"{last_user} {question}"

    q_lower = effective_question.lower()
    all_words = re.findall(r'\b\w{3,}\b', q_lower)
    keywords = [w for w in all_words if w not in _STOPWORDS] or all_words

    if not keywords:
        return (
            "I'm not sure about that. Try asking about islands, items, "
            "commands, or how the Chopaeng community works!"
        )

    # Score each section: heading matches count double.
    # On ties, prefer shorter (more focused) sections — keyword density breaks ties.
    best_score = 0
    best_density = 0.0
    best_text = ''
    for heading, body in _KB_SECTIONS:
        heading_lower = heading.lower()
        body_lower = body.lower()
        score = (
            sum(2 for kw in keywords if _wb_match(kw, heading_lower))
            + sum(1 for kw in keywords if _wb_match(kw, body_lower))
        )
        if score > 0:
            # Density = score / word-count; higher density means more relevant.
            word_count = max(len(body.split()), 1)
            density = score / word_count
            if score > best_score or (score == best_score and density > best_density):
                best_score = score
                best_density = density
                best_text = body

    if best_score > 0:
        return _trim_to_sentences(best_text)

    return (
        "I'm not sure about that. Try asking about islands, items, "
        "commands, or how the Chopaeng community works!"
    )


# ---------------------------------------------------------------------------
# LLM-powered answer (optional – requires provider API key)
# ---------------------------------------------------------------------------


_AI_SYSTEM_PROMPT = (
    "# ROLE\n"
    "You are Chobot, the official AI assistant for the Chopaeng Animal Crossing: "
    "New Horizons (ACNH) community. You help members on Discord and Twitch with "
    "islands, items, villagers, bot commands, and community rules. Your tone is "
    "warm, upbeat, and inclusive — reflecting the 'choPaeng' spirit.\n\n"

    "# INFORMATION SOURCES (internal — do not name these labels to users)\n"
    "1. **Live Data** — Real-time island statuses, item lists, visitor counts, and "
    "villager locations from the console API. Prefer this for current availability "
    "(e.g. 'where is Raymond?', 'which islands are online?', 'what items does Harana have?').\n"
    "2. **Community guides & rules** — The reference block in the user prompt: rules, "
    "commands, how-tos. Use for anything not covered by live data.\n"
    "3. **General ACNH knowledge** — Basic gameplay when not Chopaeng-specific. Never "
    "contradict community rules.\n\n"

    "# CORE DIRECTIVES\n"
    "1. **Cheerful and Concise.** Greet users warmly and answer directly with 5 sentences. Use 1-2 friendly emojis (like 🌟, 😊, or 🏝️) to keep an upbeat tone.\n"
    "2. **No Fillers or Reassurances.** Do not add explanations unless asked. "
    "Never end with 'let me know' or similar follow-up phrases. \n"
    "3. **Answer specifically.** Give only what was asked. Don't dump the full command "
    "list unless the user explicitly asks for all commands.\n"
    "4. **Use live data for availability.** When asked about an island's status, items, "
    "or villagers, check the Live Data section first and cite it (e.g. 'As of right now, "
    "Raymond is on Bathala and Giliw.').\n"
    "5. **Clarify vague requests.** If a user says 'help me' with no context, ask what "
    "they need: finding an item, getting a Dodo code, subscriber info, etc.\n"
    "6. **Format for mobile.** Use backticks for commands (`!senddodo`, `!find <item>`). "
    "Avoid Markdown tables — they render poorly in Discord mobile. "
    "Never print plain URLs; always wrap them in Markdown links (e.g., [Link Name](url)).\n"
    "7. **Handle request-help questions using the reference guides below.** If users ask "
    "how to request an item, villager, Sanrio villager, DIY, customization, max bells, "
    "schedules, or commands, follow the guides in the reference block first.\n"
    "7b. **Tickets & 'Am I doing the wrong thing?'** If they want to open a ticket, need "
    "staff/mod help, or are unsure about rules: answer calmly. Point to the support-ticket "
    "steps and channel <#943118146259284008>. Ordering/item requests belong in "
    "<#1175672083183829075> — not the same as a mod ticket.\n"
    "8. **Point users to the appropriate request-help channel when relevant.** For sub island commands "
    "like !drop or villager injections, point to <#782872507551055892>. For Chorder Bot ordering help "
    "(used by non-subs or for unstocked items), point to <#1175704849409654804>.\n"
    "9. **Admit unknowns honestly.** If you can't find the answer, say so and suggest "
    "contacting an Admin or Moderator on Discord.\n"
    "10. **Never tell users you are using a 'knowledge base', 'KB', or 'internal docs'.** "
    "Say things like: community guides, FAQs, the linked channels, or 'here`s how it works'.\n\n"
    "If you are unsure just check the knowledge base and answer based on that. If the question is vague, ask for clarification. "

    "# REQUEST-SPECIFIC BEHAVIOR\n"
    "- If the user asks how to get items:\n"
    "  * **For subscribers:** Explain using `!drop` on sub islands while on the island.\n"
    "  * **For non-subscribers or unstocked items:** Explain the Chorder Bot workflow: use "
    "`!order <item names>` in <#1175672083183829075>, check queue with `!position`, and follow "
    "instructions when their turn arrives. Emphasize that only the person who placed the order "
    "can visit the island.\n"
    "- If the user asks how to request a villager, explain `!injectvillager <house#> <name>` "
    "or `!mvi <name1> <name2> ...` as appropriate, remind them not to be on the island during "
    "injection, and point them to <#782872507551055892> for extra help.\n"
    "- If the user asks about Sanrio/in-boxes villagers, use the step-by-step guide in the "
    "reference block: inject a placeholder first (before flying in), then inject the target "
    "character once physically on the island.\n"
    "- If the user asks how to customize an item, explain: `!lookup <item>` → `!item <HEX>` → "
    "`!customize <HEX> <code>` → `!drop <customized code>` (subscribers only).\n"
    "- If the user asks for DIY recipes, explain: `!recipe <item>` → copy hex code → "
    "`!drop <hex code>` (subscribers only). For non-subscribers, direct them to Chorder Bot.\n"
    "- If the user asks for max bells, explain the turnip / Nook's Cranny method and use "
    "`!gt` to check shop hours.\n"
    "- If the user asks about villager schedules, provide the personality-based wake schedule "
    "from the reference guides. Use `ac!lookup villager <name>` to check personality.\n"
    "- If the user asks about free island Dodo codes, mention the Dodo Board in <#1500493205672825056> "
    "or direct them to use `!senddodo` in the island channel.\n"
    "- If the user asks for commands, give a concise grouped command list. For detailed help, "
    "subscribers use island channels; non-subscribers reference the Chorder Bot guides.\n\n"

    "# HARD RULES\n"
    "- Never reveal or guess Dodo codes; direct users to `!senddodo` in the island channel.\n"
    "- Never recommend violating community rules (sharing codes, littering, AFK, etc.).\n"
    "- Never fabricate island stock, villager locations, or visitor counts — only use "
    "data from the Live Data section and the community reference block below."
)


def _build_prompt(question: str, history: Optional[list[dict]] = None, channel_context: Optional[str] = None) -> str:
    """Build a provider-agnostic prompt for Gemini/OpenAI backends."""
    conversation_context = ""
    if history:
        lines = []
        for turn in history:
            role = "User" if turn["role"] == "user" else "Assistant"
            lines.append(f"{role}: {turn['content']}")
        conversation_context = (
            "\n### Previous Conversation ###\n"
            + "\n".join(lines)
            + "\n"
        )

    live_context = _build_live_context()
    live_section = f"\n### Live Island & Villager Data ###\n{live_context}\n" if live_context else ""

    chat_log_context = _build_chat_log_context()
    chat_log_section = (
        f"\n### Recent Community Chat ###\n{chat_log_context}\n"
        if chat_log_context else ""
    )

    channel_section = (
        f"\n### Channel Context ###\nThis question was asked in the Discord channel: #{channel_context}\n"
        if channel_context else ""
    )

    return (
        f"{_AI_SYSTEM_PROMPT}\n\n"
        "# EXAMPLES\n"
        "User: hi\n"
        "AI: Hello! Welcome to the Chopaeng community. How can I help you today? "
        "Are you looking for a specific item, or do you need help visiting an island?\n\n"
        "User: help me\n"
        "AI: I'm here to help! What are you having trouble with? Let me know if you need "
        "help finding items, understanding the rules, or getting a Dodo code.\n\n"
        "User: how to get dodo code\n"
        "AI: To get a Dodo code, go to the specific island's channel in our Discord "
        "server and type `!senddodo` or `!sd`. The bot will DM the code to you!\n\n"
        "User: how do I request an item\n"
        "AI: If the item isn't currently stocked on an island, use the ChoBot / ordering "
        "flow from the server's ordering instructions. For extra help with requests, check "
        "channel <#1175704849409654804>.\n\n"
        "User: how do I customize an item\n"
        "AI: Use `!lookup <item>` to find the HEX ID, `!item <HEX>` to see variants, then "
        "`!customize <HEX> <code>` to generate the customized code, and finally `!drop <code>` "
        "to drop it.\n\n"
        "User: how do I get a Sanrio villager\n"
        "AI: Follow the seven-step Sanrio process: inject a placeholder villager first, fly "
        "to the island, then inject your target Sanrio/Amiibo character while physically on "
        "the island. Check #guides for the full walkthrough or ask for specific steps.\n\n"
        "User: is there a way to open a ticket? I'm worried I'll do the wrong thing\n"
        "AI: Yes — you can open a support ticket in <#943118146259284008>. Choose **General "
        "Ticket** for general help or **Sub Ticket** for subscription issues. Read the FAQ "
        "first if you can (<#1086127868863578132>). For item orders use the ordering flow in "
        "<#782872507551055892> — that's separate from a mod ticket.\n\n"
        "User: where is Raymond?\n"
        "AI: Raymond is currently on Bathala and Giliw!\n\n"
        f"### Community guides & rules (internal reference — do not call this a 'knowledge base' to users) ###\n{CHOPAENG_KNOWLEDGE}\n"
        f"{live_section}"
        f"{chat_log_section}"
        f"{channel_section}"
        f"{conversation_context}"
        f"\n### Current Question ###\n{question}"
    )


async def get_ai_answer(
    question: str,
    gemini_api_key: Optional[str] = None,
    openai_api_key: Optional[str] = None,
    openai_base_url: Optional[str] = None,
    provider: Optional[str] = None,
    gemini_model: str = "gemini-1.5-flash",
    openai_model: str = "gpt-4o-mini",
    conversation_key: Optional[str] = None,
    channel_context: Optional[str] = None,
) -> str:
    """
    Answer a question about Chopaeng.

    If *conversation_key* is provided, past exchanges for that key are retrieved
    from the module-level ``conversation_store`` and passed as context, and the
    new exchange is stored back so future calls continue the conversation.

    *channel_context* is the Discord channel name where the question was asked.
    When provided it is injected into the prompt so the AI can tailor its answers
    to the topic of that channel (e.g. #free-islands vs #general-chat).

    Prefers provider selected by *provider* ("openai" or "gemini") when set.
    If selected provider fails or has no key, tries other configured providers,
    then falls back to the built-in keyword search.
    """
    if not question or not question.strip():
        return _GREETING_RESPONSE

    q = question.strip()

    # Respond to greetings warmly without hitting the KB or API.
    if _is_greeting(q):
        if conversation_key:
            conversation_store.add(conversation_key, q, _GREETING_RESPONSE)
        return _auto_link_channels(_GREETING_RESPONSE)

    # Respond to vague help requests with a clarifying question.
    if _is_vague_request(q):
        if conversation_key:
            conversation_store.add(conversation_key, q, _VAGUE_RESPONSE)
        return _auto_link_channels(_VAGUE_RESPONSE)

    history = conversation_store.get(conversation_key) if conversation_key else []

    # Refresh live island/villager data if the cache is stale.
    if time.time() - _live_cache["fetched_at"] > _LIVE_CACHE_TTL:
        await _fetch_live_data()

    live_search_answer = await _try_live_search_answer(q)
    if live_search_answer:
        if conversation_key:
            conversation_store.add(conversation_key, q, live_search_answer)
        return _auto_link_channels(live_search_answer)

    selected = (provider or "").strip().lower()
    providers_to_try: list[tuple[str, Optional[str]]] = []

    if selected == "openai":
        providers_to_try.append(("openai", openai_api_key))
        providers_to_try.append(("gemini", gemini_api_key))
    elif selected == "gemini":
        providers_to_try.append(("gemini", gemini_api_key))
        providers_to_try.append(("openai", openai_api_key))
    else:
        # Auto mode: prefer OpenAI when key is configured, else Gemini.
        providers_to_try.append(("openai", openai_api_key))
        providers_to_try.append(("gemini", gemini_api_key))

    for name, key in providers_to_try:
        if not key:
            continue
        try:
            if name == "openai":
                answer = await _openai_answer(
                    q,
                    key,
                    model=openai_model,
                    base_url=openai_base_url,
                    history=history,
                    channel_context=channel_context,
                )
            else:
                answer = await _gemini_answer(
                    q, key, model=gemini_model, history=history, channel_context=channel_context
                )

            if conversation_key:
                conversation_store.add(conversation_key, q, answer)
            return _auto_link_channels(answer)
        except Exception as e:
            logger.warning(f"[ChopaengAI] {name} failed ({e}), trying next fallback.")

    answer = _keyword_answer(q, history=history)
    if conversation_key:
        conversation_store.add(conversation_key, q, answer)
    return _auto_link_channels(answer)


async def _gemini_answer(
    question: str,
    api_key: str,
    model: str = "gemini-1.5-flash",
    history: Optional[list[dict]] = None,
    channel_context: Optional[str] = None,
) -> str:
    """Call the Gemini API asynchronously and return the answer."""
    import google.generativeai as genai  # lazy import

    genai.configure(api_key=api_key)
    gemini_model = genai.GenerativeModel(model)
    prompt = _build_prompt(question, history=history, channel_context=channel_context)

    # Gemini's generate_content is synchronous; run it in a thread to avoid blocking.
    import asyncio
    loop = asyncio.get_event_loop()
    response = await loop.run_in_executor(
        None, lambda: gemini_model.generate_content(prompt)
    )
    text = response.text.strip()
    return text if text else _keyword_answer(question)


async def _openai_answer(
    question: str,
    api_key: str,
    model: str = "gpt-4o-mini",
    base_url: Optional[str] = None,
    history: Optional[list[dict]] = None,
    channel_context: Optional[str] = None,
) -> str:
    """Call the OpenAI Chat Completions API asynchronously and return the answer."""
    from openai import OpenAI  # lazy import
    import asyncio

    client_kwargs = {"api_key": api_key}
    if base_url and base_url.strip():
        client_kwargs["base_url"] = base_url.strip()
    client = OpenAI(**client_kwargs)
    prompt = _build_prompt(question, history=history, channel_context=channel_context)

    loop = asyncio.get_event_loop()
    response = await loop.run_in_executor(
        None,
        lambda: client.chat.completions.create(
            model=model,
            temperature=0.4,
            messages=[
                {"role": "system", "content": _AI_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        ),
    )

    text = (response.choices[0].message.content or "").strip()
    return text if text else _keyword_answer(question)

"""
Chopaeng AI Module
Answers questions about the Chopaeng community using a built-in knowledge base.
Uses OpenAI or Google Gemini when API keys are configured;
falls back to keyword-based matching when no key is present.
"""

import logging
import re
import time
from typing import Optional

logger = logging.getLogger("ChopaengAI")

# ---------------------------------------------------------------------------
# Live API endpoints + cache
# ---------------------------------------------------------------------------
_ISLANDS_API_URL   = "https://console.chopaeng.com/api/islands"
_VILLAGERS_API_URL = "https://console.chopaeng.com/api/villagers/list"
_LIVE_CACHE_TTL    = 300  # seconds — refresh every 5 minutes

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
            bot_str  = " | Bot: ✓" if bot_up else (" | Bot: ✗" if bot_up is False else "")
            vis_str  = f" | Visitors: {visitors}" if visitors else ""
            line = f"- {name} [{status}] ({itype or cat})"
            if items_preview:
                line += f" — {items_preview}"
            line += vis_str + bot_str
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
    exchanges (2 × _MAX_HISTORY_TURNS messages).
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


CHOPAENG_KNOWLEDGE = """
# Chopaeng Community Knowledge Base

## Who Is Chopaeng?
Chopaeng (also known as Kuya Cho or ChoPaeng) is a Filipino Animal Crossing:
New Horizons (ACNH) content creator and Twitch streamer based in the Philippines.
He hosts 24/7 treasure islands where community members collect items and meet
villagers. ACNH is a life-simulation game by Nintendo for the Nintendo Switch
where players manage an island, collect furniture and clothing, invite animal
villagers, and visit other players' islands. The community (the "choPaeng
family") includes Filipino and international ACNH fans.

## Official Links
| Platform | URL |
|----------|-----|
| Website | chopaeng.com |
| Twitch | twitch.tv/chopaeng |
| YouTube | youtube.com/@chopaeng |
| Facebook | facebook.com/chopaenglive |
| TikTok | tiktok.com/@chopaeng |
| Discord | discord.gg/chopaeng |
| Patreon | patreon.com/cw/chopaeng/membership |

## Chobot
Chobot is the custom bot built by bitress (open-source at github.com/bitress/chobot).
It runs on both Discord and Twitch simultaneously. It syncs with a Google Sheets
database every hour to keep item and villager locations current across all 47 islands.
It includes the Flight Logger, which automatically logs sub-island visitors and
alerts staff about unrecognized users (staff can Admit, Warn, Kick, or Ban).
Slash commands (e.g. `/find`, `/villager`, `/ask`) work as alternatives to
prefix commands in Discord.

## Commands
| Command | Description | Where to use |
|---------|-------------|--------------|
| `!find <item>` or `!locate <item>` | Search which islands have an item | Anywhere |
| `!villager <name>` | Find a villager across islands | Anywhere |
| `!ask <question>` | Ask the AI about the community | Anywhere |
| `!random` | Random item suggestion with location | Anywhere |
| `!status` | Bot health, cache size, last update | Anywhere |
| `!ping` | Bot response time | Anywhere |
| `!help` | Full command list | Anywhere |
| `!islandstatus` | Which sub-island bots are online | Anywhere |
| `!senddodo` or `!sd` | Get the Dodo code (DM) | Island channel |
| `!visitors` | Current visitors on a sub island | Island channel |
| `!villagers` | Villagers currently on the island | Island channel |
| `!lookup <item>` or `!li <item>` | Look up an item's HEX ID | Island channel |
| `!drop <HEX or name>` | Drop up to 9 items (must be on island) | Island channel |
| `!recipe <item>` | Get DIY recipe order code for `!drop` | Island channel |
| `!item <HEX>` | See color/variant options for an item | Island channel |
| `!customize <HEX> <code>` | Generate customized item code | Island channel |
| `!injectvillager <house#> <name>` | Inject a villager (do BEFORE flying in) | Island channel |
| `!mvi <name1> <name2> ...` | Inject multiple villagers at once | Island channel |
| `!gt` | Current in-game time on the island | Island channel |
| `ac!lookup villager <name>` | Check villager personality | #villager-check |
| `!refresh` | Refresh item cache (Admin only) | Anywhere |

## Getting a Dodo Code (!senddodo)
A Dodo code is a 5-character code used in ACNH to visit a Chopaeng island via Dodo Airlines.
To get the Dodo code for an island, go to that island's channel in Discord and type `!senddodo`
or `!sd`. The bot will DM the code directly to you. Keep the code private — do not share it
with anyone, including friends or family. If an island is offline, `!senddodo` will tell you
instead of sending a code.

## Islands Overview
There are 47 islands total: 20 sub (subscriber/VIP) islands and 27 free islands.
All island names are Filipino/Tagalog words with meaningful translations.

### Sub Islands (20 — requires subscription or VIP role)
| Island | Meaning |
|--------|---------|
| Adhika | more/extra |
| Alapaap | cloud |
| Aruga | care |
| Bahaghari | rainbow |
| Bituin | star |
| Bonita | beautiful |
| Dakila | great/noble |
| Dalisay | pure |
| Diwa | spirit/essence |
| Gabay | guide |
| Galak | joy |
| Giliw | beloved |
| Hiraya | dreams come true |
| Kalangitan | sky/heavens |
| Lakan | nobleman |
| Likha | creation/art |
| Malaya | free |
| Marahuyo | enchanted |
| Pangarap | dream |
| Tagumpay | success/victory |

### Free Islands (27 — open to everyone)
| Island | Meaning | Specialty |
|--------|---------|-----------|
| Amihan | north wind/cool breeze | General |
| Babaylan | shaman/healer | General |
| Banaag | glimmer of light | Light/star themed furniture |
| Bathala | supreme being | Deity/mythical themed rare items |
| Dalangin | prayer | General |
| Dangal | honor/dignity | General |
| Giting | bravery/valor | General |
| Gunita | memory | General |
| Harana | serenade | Bugs, fish, sea creatures |
| Kakanggata | — | General |
| Kalawakan | outer space | Space/galaxy items, rare furniture, DIYs |
| Kariktan | beauty/charm | General |
| Kaulayaw | beloved | General |
| Kilig | giddy/excited | General |
| Kundiman | love song | Music-themed, romantic furniture |
| Maharlika | noble/freedom | General |
| Marilag | magnificent/radiant | General |
| Matahom | beautiful (Bisaya) | Clothing (tops, bottoms, accessories, shoes, hats) |
| Pagsamo | pleading | General |
| Pagsuyo | love/devotion | Bugs, fish, nature items |
| Paraluman | muse/guiding star | Clothing, seasonal/themed outfits |
| Silakbo | outburst of emotion | General |
| Sinag | ray of light/moonbeam | Light/star themed furniture |
| Sinagtala | moonlight star | Light/star themed furniture |
| Tadhana | destiny/fate | General |
| Tala | bright star | Light/star themed furniture |
| Tinig | voice/sound | General |

Stock rotates regularly across all islands. Use `!find <item>` to check current
availability.

## Subscriber / VIP Perks
Subscribe via Patreon (patreon.com/cw/chopaeng/membership) to unlock:
- Unlimited access to the 20 sub islands whenever they are open.
- Priority queue when islands are busy.
- Item/villager requests — ask for specific stock on a sub island.
- Exclusive stock: rarer items, full DIY sets, curated villager selections.
- Faster Dodo code delivery.

After subscribing, link your membership in Discord (see #set-nick or #get-roles)
to receive the sub role and unlock access.

## Community Rules (All Members)
The Discord is a family-friendly community. All members must follow these rules:
1. Be kind and respectful. No toxicity or hatred.
2. Keep private conversations in DMs.
3. No offensive, NSFW, racist, violent, or hateful content in messages,
   nicknames, bios, or posts.
4. No spam, swearing, external links, or self-promotion.
5. For questions, DM Chopaeng or any Admin/Moderator.

### Island-Specific Rules (All Visitors)
These apply on every treasure island (sub and free), in addition to the above:
1. **Dodo Code is confidential.** Do not share it with anyone — not other
   accounts, friends, or family. One character/island per membership.
2. **Set your server nickname** to `ACNH Character Name | Your ACNH Island Name`
   (e.g. `Kuya | Hiraya`) in the #set-nick channel.
3. **Leave via the airport.** Do not press "-" to close the game; this may
   cause lost items. No AFK on islands.
4. **Check internet before flying.** NAT Type A or B required. NAT Type C or D
   causes disconnections — do not join. Orderbot requires 15–25 Mbps minimum.
5. **Read pinned messages** in each island channel before asking questions.
6. **No littering.** Use the trash bins on every island. Litter blocks item
   refresh.
7. **Only use ChoBot commands while on the island.** Do not request items unless
   you can pick them up.
8. **Do not enter commands while someone is flying in.** The bot cannot process
   commands during the loading screen.
9. Only pick up items assigned to you or clearly free items.
10. Do not run over flowers, dig up trees, or talk to residents to lure them away.
11. Leave promptly when done. Be friendly in chat.

Breaking rules may result in a warning, mute, kick, or ban.

### Chorder Bot (Order Bot) Rules
Use Chorder Bot when an item is not on any island and you need to request it.
Check the #ordering channel for instructions. Subscribers get priority.
1. All Island-Specific Rules above apply.
2. The #chorder-bot channel is for orders only — no chatting or lookups.
3. The #chorder-item-lookup channel is for item/DIY code lookups only.
4. Use #chorder-bot-help for questions. Read #chorder-bot-how before ordering.
5. Order only what you need.

## How to Get Items (Step by Step)
1. Type `!find <item>` in Discord or Twitch to search.
2. The bot shows which islands currently have it.
3. Go to that island's Discord channel. Type `!senddodo` or `!sd` to get the
   Dodo code sent to your DMs.
4. In ACNH, go to Dodo Airlines and fly using the code.
5. Collect your items and leave via the airport.

## Dropping Items with !drop
You must be on the island before using drop commands.

**By HEX ID:** `!drop <HEX>` — e.g. `!drop 2656`. Use `!lookup <item>` to
find the HEX ID. Up to 9 items per line: `!drop 2656 0EE8 074E`.

**By name:** `!drop <item name>` — e.g. `!drop Pagoda` or `!drop Pagoda, Golden Axe, Harp`.

**Stacks:** Add a prefix before the HEX ID:
| Stack Size | Prefix | Example |
|------------|--------|---------|
| 10 | `090000` | `!drop 0900002656` |
| 30 | `1D0000` | `!drop 1D000009C6` |
| 50 | `310000` | `!drop 3100002656` |

## Getting DIY Recipes
1. `!recipe <item>` — get the recipe order code (e.g. `!recipe Golden Axe` → `297000016A2`).
2. `!drop <code>` — drop the recipe card. Up to 9 per line.

## Customizing Items
1. `!lookup <item>` — find the HEX ID.
2. `!item <HEX>` — see color/variant options (e.g. `!item 0EE8` → `0=Green, 1=Brown, 2=White, 3=Black`).
3. `!customize <HEX> <code>` — generate customized code (e.g. `!customize 0EE8 2` → `0000000200000EE8`).
4. `!drop <customized code>` — drop the item.

For items with both color AND design options, add both decimal codes together.
Example: mug with Pink (5) + Square Logo (32) = 37 → `!customize 074E 37`.

## Injecting Villagers
**Important: Do NOT be on the island when injecting. Fly in after confirmation.**
1. `!injectvillager <house#> <name>` — house 0 = 1st house, 1 = 2nd, ..., 9 = 10th.
2. Wait for confirmation: "Villager has been injected."
3. Fly in and visit the villager.

For multiple villagers: `!mvi Judy Marshal Raymond`.

### Getting a Sanrio / In-Boxes Villager
1. Be on the island. Check the first house for a villager "in boxes."
2. Leave the house. Inject a Sanrio villager: `!injectvillager Marty`.
3. Enter the home and invite the Sanrio villager.
4. Leave the island. Time-travel one day forward to complete the move.

## Max Bells (Bell Glitch)
1. Get 1 stack of turnips on a Chopaeng island.
2. Sell at Nook's Cranny on the same island (price shows -64,000,000 bells — proceed).
3. Check your ABD on your own island afterward — bells are updated there.

Nook's Cranny hours: 8 AM – 10 PM island time. Use `!gt` to check.

## Villager Wake Schedules
Use `!gt` to check island time, then refer to these schedules:
| Personality | Awake |
|-------------|-------|
| Normal | 6:00 AM – 12:00 AM |
| Jock | 6:30 AM – 12:30 AM |
| Smug | 7:00 AM – 2:00 AM |
| Peppy | 7:00 AM – 1:20 AM |
| Lazy | 8:00 AM – 11:00 PM |
| Snooty | 8:30 AM – 2:30 AM |
| Cranky | 9:00 AM – 3:30 AM |
| Sisterly | 9:30 AM – 3:00 AM |

Check a villager's personality: `ac!lookup villager <name>` in #villager-check.

## Support & Donations
Donations fund server hosting (47 islands), stream upgrades, and giveaways.
Ways to support: subscribe on Twitch, donate via chopaeng.com, or cheer with
Twitch Bits.

## Giveaways
Regular community giveaways include rare ACNH items, DIY recipes, real-life
prizes, and special island visits. Announced on Discord and Twitch.
Check chopaeng.com for the latest info.

## Tips
- "Chopaeng" is a playful Filipino term of endearment from "paeng."
- "Hiraya Manawari" means "may the wishes of your heart be granted."
- Items rotate regularly — always use `!find` before visiting.
- The bot cache refreshes hourly; new items appear quickly.
- If an island is offline, `!senddodo` tells you instead of sending a code.
- Free islands are ideal for newcomers; sub islands have rarer stock.
- Use `!villager` for animal residents; use `!find` for items.
- Popular villagers (Raymond, Marshal, Judy, etc.) often appear on sub islands first.

## Troubleshooting
- **Disconnected while visiting?** Your items may not have saved. Fly back in
  and re-collect them. Always check your internet and NAT type before visiting.
- **Bot not responding to commands?** Someone may be flying in (loading screen).
  Wait until they land, then try again.
- **Nook's Cranny closed?** Check island time with `!gt`. Hours are 8 AM – 10 PM.
  If it should be open but is not, contact a moderator.
- **Villager not appearing after injection?** Make sure you were NOT on the island
  when you ran `!injectvillager`. Leave and re-enter if needed.
- **Item not found with !find?** The item may not be stocked on any island right
  now. Use the orderbot to request it via the #ordering channel.
- **Cannot pick up dropped item?** Your pockets may be full. Drop or store
  unwanted items first.
- **Internet dropped during a transaction?** Reconnect and fly back in using a
  fresh Dodo code (`!sd`). Items you already picked up before the drop may need
  to be re-collected.
"""

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
    "Hello! Welcome to the Chopaeng community! 🌟 "
    "How can I help you today? Are you looking for a specific item, need a Dodo code, "
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
    "warm, upbeat, and inclusive — reflecting the 'choPaeng family' spirit.\n\n"

    "# KNOWLEDGE SOURCES (in priority order)\n"
    "1. **Live Data** — Real-time island statuses, item lists, visitor counts, and "
    "villager locations fetched from the console API. Always prefer this for current "
    "availability questions (e.g. 'where is Raymond?', 'which islands are online?', "
    "'what items does Harana have?').\n"
    "2. **Chopaeng Knowledge Base** — Community rules, island descriptions, commands, "
    "how-to guides, and background info. Use this for anything not covered by live data.\n"
    "3. **General ACNH knowledge** — For basic gameplay questions not specific to "
    "Chopaeng. Never contradict the KB with general ACNH info.\n\n"

    "# CORE DIRECTIVES\n"
    "1. **Be conversational.** Greet users warmly and invite them to ask their question.\n"
    "2. **Be concise.** Chat context — aim for 1–4 sentences. Use bullet points only "
    "when listing 3+ items.\n"
    "3. **Answer specifically.** Give only what was asked. Don't dump the full command "
    "list unless the user explicitly asks for all commands.\n"
    "4. **Use live data for availability.** When asked about an island's status, items, "
    "or villagers, check the Live Data section first and cite it (e.g. 'As of right now, "
    "Raymond is on Bathala and Giliw.').\n"
    "5. **Clarify vague requests.** If a user says 'help me' with no context, ask what "
    "they need: finding an item, getting a Dodo code, subscriber info, etc.\n"
    "6. **Format for mobile.** Use backticks for commands (`!senddodo`, `!find <item>`). "
    "Avoid Markdown tables — they render poorly in Discord mobile.\n"
    "7. **Admit unknowns honestly.** If you can't find the answer, say so and suggest "
    "contacting an Admin or Moderator on Discord.\n\n"

    "# HARD RULES\n"
    "- Never reveal or guess Dodo codes; direct users to `!senddodo` in the island channel.\n"
    "- Never recommend violating community rules (sharing codes, littering, AFK, etc.).\n"
    "- Never fabricate island stock, villager locations, or visitor counts — only use "
    "data present in the Live Data or Knowledge Base sections."
)


def _build_prompt(question: str, history: Optional[list[dict]] = None) -> str:
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
        "User: where is Raymond?\n"
        "AI: Raymond is currently on Bathala and Giliw! (based on live data)\n\n"
        f"### Chopaeng Knowledge Base ###\n{CHOPAENG_KNOWLEDGE}\n"
        f"{live_section}"
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
) -> str:
    """
    Answer a question about Chopaeng.

    If *conversation_key* is provided, past exchanges for that key are retrieved
    from the module-level ``conversation_store`` and passed as context, and the
    new exchange is stored back so future calls continue the conversation.

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
        return _GREETING_RESPONSE

    # Respond to vague help requests with a clarifying question.
    if _is_vague_request(q):
        if conversation_key:
            conversation_store.add(conversation_key, q, _VAGUE_RESPONSE)
        return _VAGUE_RESPONSE

    history = conversation_store.get(conversation_key) if conversation_key else []

    # Refresh live island/villager data if the cache is stale.
    if time.time() - _live_cache["fetched_at"] > _LIVE_CACHE_TTL:
        await _fetch_live_data()

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
                )
            else:
                answer = await _gemini_answer(q, key, model=gemini_model, history=history)

            if conversation_key:
                conversation_store.add(conversation_key, q, answer)
            return answer
        except Exception as e:
            logger.warning(f"[ChopaengAI] {name} failed ({e}), trying next fallback.")

    answer = _keyword_answer(q, history=history)
    if conversation_key:
        conversation_store.add(conversation_key, q, answer)
    return answer


async def _gemini_answer(
    question: str,
    api_key: str,
    model: str = "gemini-1.5-flash",
    history: Optional[list[dict]] = None,
) -> str:
    """Call the Gemini API asynchronously and return the answer."""
    import google.generativeai as genai  # lazy import

    genai.configure(api_key=api_key)
    gemini_model = genai.GenerativeModel(model)
    prompt = _build_prompt(question, history=history)

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
) -> str:
    """Call the OpenAI Chat Completions API asynchronously and return the answer."""
    from openai import OpenAI  # lazy import
    import asyncio

    client_kwargs = {"api_key": api_key}
    if base_url and base_url.strip():
        client_kwargs["base_url"] = base_url.strip()
    client = OpenAI(**client_kwargs)
    prompt = _build_prompt(question, history=history)

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

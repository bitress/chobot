# Chobot System

Simple bot for Animal Crossing. It works for Discord, Twitch, and has API for web.

## Description

Chobot is a unified system to help manage Animal Crossing communities. It watches island visitors to keep them safe, helps users find items and villagers, and connects Twitch chat with discord data. It uses Google Sheets to keep all information up to date.

### Features

* **Flight Logger (Security)**
    * Watch people who visit islands in real-time.
    * Send alert to staff if person is unknown.
    * Interactive moderation buttons: **Admit**, **Warn**, **Kick**, **Ban**, **Dismiss**, **Investigate**.
    * Add investigation notes with the **Note** action.
    * Remove island access role automatically if someone is warned.
    * Warnings expire automatically after 3 days.
    * Track all visits in a local SQLite database.
    * Send detailed moderation log to a dedicated Discord channel.
    * Guild-specific configuration for multi-server support.
    * Slash commands:
        * `/flight_status` — Display current Flight Logger statistics.
        * `/recover_flights` — Recover any missing flight records.
        * `/unwarn <user>` — Remove an active warning from a user.
        * `/warnings <user>` — View the full warning history for a user.
        * `/flight_history <user>` — View a user's complete island visit history.

* **Discord Bot Commands**
    * `!find <item>` (alias `!f`) — Search for an item across all islands.
    * `!villager <name>` — Find which island a villager is on.
    * `!random` — Get a random item suggestion.
    * `!islandstatus` — Check if an island is ONLINE, OFFLINE, or FULL.
    * `!dodo <island>` — Request a dodo code privately via DM.
    * `!visitors` — List all current visitors on an island.
    * `!ask <question>` — Ask the Chopaeng AI a question about the community.
    * `!ping` — Check the bot's current response time.
    * `!status` — View bot health, uptime, and service information.
    * `!help` — Display the full help menu.
    * `!refresh` *(admin only)* — Force an immediate cache refresh from Google Sheets.
    * `!update` *(admin only)* — Pull the latest code and restart the bot.

* **Twitch Bot Commands**
    * `!find <item>` (aliases: `!locate`, `!where`, `!lookup`, `!lp`, `!search`) — Search for an item.
    * `!villager <name>` — Find a villager's location.
    * `!random` — Get a random item suggestion.
    * `!ask <question>` — Ask the Chopaeng AI.
    * `!status` — View bot status.
    * `!help` — Display available commands.

* **Smart Fuzzy Search**
    * Multi-strategy search pipeline: exact match → prefix → contains → token overlap → fuzzy → plural fallback.
    * Context-aware fuzzy thresholds (97 for short queries, down to 80 for longer ones).
    * Full Unicode support including CJK (Chinese, Japanese, Korean) characters.
    * Returns close suggestions when an exact match is not found.

* **Island Status & Dodo Codes**
    * Reads `Dodo.txt` and `Visitors.txt` files to show real-time island status.
    * Reports island state as **ONLINE**, **OFFLINE**, or **FULL**.
    * Supports 18 subscriber islands and 27 free islands.
    * Sends dodo codes securely via DM on request.

* **Chopaeng AI Knowledge Base**
    * Built-in keyword-based knowledge about the community, guidelines, islands, and VIP info — no paid API required.
    * Optional upgrade with free **Google Gemini** AI for richer answers.
    * Per-user conversation history (5-turn memory with a 10-minute expiry).
    * Available on both Discord (`!ask`) and Twitch (`!ask`).

* **REST API**
    * `GET /health` or `GET /api/health` — Health check (returns JSON status).
    * `GET /find?item=<name>` — Search for an item (HTML response).
    * `GET /api/find` — Search for an item (JSON response).
    * `GET /api/villager` — Find a villager (JSON response).
    * `GET /api/villagers/list` — List villagers grouped by island (JSON response).
    * `GET /api/islands` — Island status, visitors, and dodo codes (JSON response).
    * `GET /api/patreon/posts` — List cached Patreon posts.
    * `GET /api/patreon/posts/<id>` — Get a single Patreon post by ID.
    * `POST /api/refresh` — Trigger a manual cache refresh.

* **Web Dashboard** *(mod-only)*
    * Secure login with mod-only access.
    * Island management interface.
    * Analytics and reporting overview.
    * Activity logs and visitor tracking.

* **Patreon Integration**
    * Fetch and cache patron posts via the Patreon API.
    * Extract and serve post images.
    * Per-post metadata available through the REST API.

* **Data Management**
    * Auto-sync with Google Sheets every hour.
    * Fast local cache in `cache_dump.json` for instant startup.
    * Thread-safe access shared across all services.

* **Multi-Service Deployment**
    * Run all services together or independently with 7 launch modes:
        ```bash
        python main.py                   # All services
        python main.py flask             # API only
        python main.py discord           # Discord bot (all features)
        python main.py discord-find      # Discord (search commands only)
        python main.py flight-logger     # Discord (Flight Logger only)
        python main.py twitch            # Twitch bot (full)
        python main.py twitch-find       # Twitch (find commands only)
        ```
    * Graceful coordinated shutdown on SIGINT/SIGTERM.


## Getting Started

### Dependencies

* Python 3.9 or newer.
* Discord Bot Token (with intents).
* Twitch Bot Token.
* Google Sheets Service Account.

### Installing

1. Download or clone this project.
2. Put your secrets inside a file named `.env` in the root folder:

```env
# --- BOT TOKENS ---
DISCORD_TOKEN=your_discord_token
TWITCH_TOKEN=your_twitch_token
PATREON_TOKEN=your_patreon_token

# --- DISCORD CONFIG ---
GUILD_ID=729590421478703135
SUB_CATEGORY_ID=821474059018829854
CHANNEL_ID=1450554092626903232
ISLAND_ACCESS_ROLE=1077997850165772398

# --- FLIGHT LOGGER ---
FLIGHT_LISTEN_CHANNEL_ID=809295405128089611
FREE_ISLAND_FLIGHT_LISTEN_CHANNEL_ID=876490101595721748
FLIGHT_LOG_CHANNEL_ID=1451990354634080446
IGNORE_CHANNEL_ID=809295405128089611
SUB_MOD_CHANNEL_ID=1077960085826961439

# --- OTHER ---
TWITCH_CHANNEL=chopaeng
WORKBOOK_NAME=ChoPaeng_Database
IS_PRODUCTION=false

# --- GEMINI AI (optional, free tier) ---
# Get a free key at https://aistudio.google.com/
# Leave blank to use the built-in keyword fallback.
GEMINI_API_KEY=
```

### Executing program

* How to run the bot:
1. Open terminal in project folder.
2. Type this command:
```bash
python main.py
```

## Help

If bot not start, check if you put correct tokens in .env file.
Make sure your Python is version 3.9 or higher.

## Authors

bitress
[@bitress](https://github.com/bitress)

## Version History

* 0.1
    * Initial Release
    * Add Flight Logger, Discord Bot, Twitch Bot, and Patreon API.

## License

This project is licensed under the MIT License - see the LICENSE.md file for details

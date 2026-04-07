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
    * Secure login with mod-only access (secret key **or** Discord OAuth).
    * Island management interface.
    * Analytics and reporting overview.
    * Activity logs and visitor tracking.
    * Role-based access: Admin/Senior Mod and Discord server administrators get full dashboard access.

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
        python main.py migrate-mariadb   # One-time SQLite -> MariaDB migration
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

# --- AI PROVIDERS (optional) ---
# Provider selection: auto | openai | gemini
# auto = tries OPENAI_API_KEY first, then GEMINI_API_KEY.
AI_PROVIDER=auto

# OpenAI (optional)
# Get an API key at https://platform.openai.com/
OPENAI_API_KEY=
# Optional custom endpoint/base URL (leave blank for default OpenAI endpoint)
# Default endpoint is https://api.openai.com/v1
OPENAI_BASE_URL=
OPENAI_MODEL=gpt-4o-mini

# Gemini (optional, free tier)
# Get a free key at https://aistudio.google.com/
GEMINI_API_KEY=
GEMINI_MODEL=gemini-1.5-flash

# --- MARIADB MIGRATION (optional) ---
# Used by: python main.py migrate-mariadb
MARIADB_HOST=
MARIADB_PORT=3306
MARIADB_USER=
MARIADB_PASSWORD=
MARIADB_DATABASE=chobot
# true = TRUNCATE target tables before import; false = append rows
MARIADB_TRUNCATE_BEFORE_IMPORT=true
```

### Migrating SQLite data to MariaDB

1. Set the `MARIADB_*` values in your `.env` file.
2. Run:
    ```bash
    python main.py migrate-mariadb
    ```
3. Check logs for per-table row counts and final success summary.

This migrates all user tables from `chobot.db` into MariaDB. By default it truncates target tables first (`MARIADB_TRUNCATE_BEFORE_IMPORT=true`).

### Setting up Discord OAuth login for the Dashboard

The dashboard supports two login methods:

1. **Secret key** — set `DASHBOARD_SECRET` in your `.env` file and enter it on the login page.
2. **Log in with Discord** *(recommended)* — lets mods authenticate with their Discord account. To enable it:
   1. Go to [Discord Developer Portal](https://discord.com/developers/applications) and create (or select) an application.
   2. Under **OAuth2 → Redirects**, add your callback URL:
      - Production: `https://your-domain/dashboard/oauth2/callback`
      - Local dev: `http://localhost:5000/dashboard/oauth2/callback`
   3. Copy the **Client ID** and **Client Secret** from the OAuth2 page.
   4. Add them to your `.env` file:
      ```env
      DISCORD_CLIENT_ID=your_client_id
      DISCORD_CLIENT_SECRET=your_client_secret
      ```
   5. Set `ADMIN_ROLE_ID` to your server's Senior Mod role ID. Any member with the Discord Administrator permission is also granted access automatically.

Once `DISCORD_CLIENT_ID` is set, the **Log in with Discord** button will appear on the login page automatically.

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

* **1.0** *(2026-03-13)*
    * Add action type filter buttons/pills to XLog Warning Logs dashboard.
    * Log all XLog button actions (WARN/KICK/BAN/DISMISS/NOTE/ADMIT) to database.
    * Use client browser timezone for timestamp display; keep UTC+8 for analytics aggregations and CSV export.
    * Fix `/update` command: move `os.execv()` to main thread to prevent multiple bot instances.
    * Replace 4-status system with 3 statuses derived from `discord_bot_online` and `dodo_code`.
    * Add simple-datatables pagination to all dashboard tables; fix Island Status data source; improve analytics stats.

* **0.9** *(2026-03-12)*
    * Fix duplicate command responses with SQLite-based message deduplication.
    * Fix log_result double-sending embed by moving `_creating_alerts` guard before `record_island_visit`.
    * Fix duplicate traveller alert embeds via race-condition guard.
    * Add API refresh button to dashboard top navbar with tooltip and toast feedback.
    * Fix OTA update restart: Flask port retry loop and git pull exit code check.
    * Hide `dodo_code` and visitors when discord bot is not confirmed online.
    * Fix OTA update: schedule restart in thread so it survives event-loop teardown.
    * Add slash commands: `/flight_status`, `/recover_flights`, `/unwarn`.
    * Fix missing commands in `!help` (islands args, `!update` admin cmd).
    * Fix OTA update to restart bot process instead of reloading cogs in-place.
    * Add copyable CDN link input on island detail page.

* **0.8** *(2026-03-11 – 2026-03-12)*
    * Convert dashboard timestamps to client local timezone via JS.
    * Improve topbar: add Discord avatar and username card.
    * Remove baby_mod role; grant full access to admins and senior mods only.
    * Fix Discord OAuth 1010: add User-Agent to all Discord API requests.
    * Fix Discord login documentation in `.env.example` and README.
    * Fix `/api/islands` to return only canonical island fields.
    * Fix dashboard/status: correct stat tiles, percentage rounding, and Capacity Overview labels.
    * Fix: downgrade Discord user lookup HTTP 403 log from WARNING to DEBUG.
    * Resolve Discord user IDs to usernames in warnings views.
    * Remove `_SAMPLE_ISLANDS` seed data; read islands from DB only.

* **0.7** *(2026-03-11)*
    * Merge `!islands` + `!freeislands` into single `/islands [sub|free]` command.
    * Add `!freeislands` Discord command for free island status.
    * Add free-island flight checker for website-only visit tracking.
    * Add dedicated Island Status Breakdown page to dashboard.
    * Remove Dodo Code column from dashboard islands table.
    * Remove Traveler Blocklist / Allowlist.
    * Auto-derive OAuth2 redirect URI from request.
    * Add Discord OAuth2, role-based access, island status breakdown, IGN search, sparkline chart, day-of-week analytics, new/returning travelers.
    * Add island type filter to analytics; add `discord_bot_online` to dashboard API.
    * Wire Discord island status to DB and expose via public and dashboard APIs.
    * Use `discord_bot_online` for island status (Online/Offline only).

* **0.6** *(2026-03-11)*
    * Bundle Chart.js locally to eliminate SRI hash mismatch that broke charts.
    * Add collapsible sidebar and responsive dashboard layout.
    * Add empty states, new analytics features (visits today/week, hourly chart, top warned users, 30-day toggle).

* **0.5** *(2026-02-20)*
    * Add `!islandstatus` command to check all 18 subscriber island orderbots.
    * Use shared role + Chobot naming convention to identify island bots.
    * Add support for fancy Unicode bot names in island status matching.
    * Add automatic warning expiration after 7 days.
    * Fix expire vs duration inconsistency in warning embeds.
    * Redesign flight logger embed: add Dismiss and Note buttons, remove buttons on close.
    * Improve Status field styling: replace code blocks with emoji indicators.
    * Make flightlogger commands hybrid (prefix + slash).
    * Improve `!help` command to include Flight Logger and Tips sections.

* **0.4** *(2026-02-19)*
    * Add `!help`, `!ping` commands and uptime tracking.
    * Add `!random` item command.
    * Fix `!help` command conflict by disabling default help.
    * Add `!flighttest` command to FlightLoggerCog.
    * Add `FIND_BOT_CHANNEL_ID` restriction for commands; send DM for blocked commands.
    * Fix Visitors.txt and Villagers.txt file-lock contention with caching and retry.
    * Fix island matching logic and autocomplete timeout errors.
    * Fix Twitch villager find.

* **0.3** *(2026-02-18 – 2026-02-19)*
    * Add slash commands on Discord bot.
    * Add Admit confirmation, Investigate button, and unwarn/warnings commands.
    * Add recover flights command; correct date when recovering flights.
    * Add unwarn and investigation features; implement `remove_all_warnings`.
    * Remove island access role automatically when a user is warned.
    * Fix singular/plural grammar in DM notification messages.
    * Refactor: extract color constants for better maintainability.

* **0.2** *(2026-02-17 – 2026-02-18)*
    * Improve flight logger embed and alert design.
    * Add SQLite database for flight/visit tracking.
    * Add DM to user when kicked or banned.
    * Add flight logger ID tracking.
    * Fix flight log channel handling and reason fields.

* **0.1** *(2026-02-10)*
    * Initial Release.
    * Add Flight Logger, Discord Bot, Twitch Bot, and Patreon API.

## License

This project is licensed under the MIT License - see the LICENSE.md file for details

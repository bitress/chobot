# Chobot System

Simple bot for Animal Crossing. It works for Discord, Twitch, and has API for web.

## Description

Chobot is a unified system to help manage Animal Crossing communities. It watches island visitors to keep them safe, helps users find items and villagers, and connects Twitch chat with discord data. It uses Google Sheets to keep all information up to date.

### Features

| # | Feature | Platform | Description |
|---|---------|----------|-------------|
| 1 | **Item Search** (`!find`) | Discord, Twitch, API | Search for any ACNH item across all islands. Supports fuzzy matching and autocomplete. Aliases: `!locate`, `!where`, `!lookup`, `!lp`, `!search` |
| 2 | **Villager Search** (`!villager`) | Discord, Twitch, API | Search for a villager by name across all islands. Fuzzy matching included. |
| 3 | **Fuzzy Suggestions** | Discord, Twitch, API | If a search term doesn't match exactly, the bot suggests the closest item or villager names with an interactive dropdown (Discord) or text list (Twitch/API). |
| 4 | **Random Item** (`!random`) | Discord, Twitch | Returns a random item from the current island inventory. |
| 5 | **Bot Status** (`!status`) | Discord, Twitch | Shows cache item count, linked island count, last update time, and bot uptime. |
| 6 | **Ping** (`!ping`) | Discord | Shows bot latency in milliseconds. |
| 7 | **Help** (`!help`) | Discord, Twitch | Lists all available commands. |
| 8 | **Island Status** (`!islandstatus`) | Discord | Checks all 18 sub island bots — reports each as online ✅ or offline ❌ using bot presence and recent message scanning. Aliases: `!islands`, `!checkislands` |
| 9 | **Cache Refresh** (`!refresh`) | Discord | Admin-only command to manually pull fresh data from Google Sheets. |
| 10 | **Flight Logger** | Discord (automatic) | Monitors island visitor arrivals in real time. Alerts staff when an unknown traveler is detected. Staff can Admit, Warn, Kick, or Ban via buttons. Tracks warnings and moderation history per user in a local database. |
| 11 | **Island Offline Notifications** ⭐ New | Discord (automatic) | Background task (every 5 min) that monitors each sub island. Posts `🔴 {island} island is currently down.` to the island's own channel when it goes offline, and `🟢 {island} island is back online!` when it recovers. |
| 12 | **GET /api/islands** ⭐ New | API | Returns real-time status of all free and VIP islands. Uses `FREE_ISLANDS` config as the authoritative list so every island appears even when its directory is missing. Each entry includes `name`, `dodo`, `status`, `visitors`, and a human-readable `message` field (`"This island is currently down."` when OFFLINE, `"This island is currently refreshing."` when REFRESHING). |
| 13 | **GET /api/find** | API | JSON item search endpoint with `found`, `query`, `results` (free/sub split), and `suggestions`. |
| 14 | **GET /api/villager** | API | JSON villager search endpoint. |
| 15 | **GET /api/villagers/list** | API | Lists all villagers grouped by island. |
| 16 | **GET /api/patreon/posts** | API | Fetches and caches the 10 most recent Patreon posts (15-minute cache). |
| 17 | **GET /api/patreon/posts/\<id\>** | API | Fetches a single Patreon post by ID (15-minute cache). |
| 18 | **GET /health** | API | Health-check endpoint — returns `healthy` (200) or `degraded` (503) with cache stats. |
| 19 | **POST /api/refresh** | API | Manually triggers a Google Sheets cache refresh in a background thread. |
| 20 | **Google Sheets Sync** | Background | Automatically re-fetches item and villager data from Google Sheets every hour. |
| 21 | **Local Cache** | Background | Persists the Google Sheets data to `cache_dump.json` so the bot starts instantly on restart. |
| 22 | **Patreon Integration** | API | Proxies Patreon post data for use on external websites, with automatic image extraction and caching. |
| 23 | **Autocomplete** | Discord (slash commands) | `/find` supports Discord native autocomplete with fuzzy matching against the full item catalogue. |
| 24 | **FIND_BOT_CHANNEL restriction** | Discord | Limits which prefix/slash commands can be used in a designated find-bot channel; silently blocks others and notifies the user via DM. |


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
FLIGHT_LOG_CHANNEL_ID=1451990354634080446
IGNORE_CHANNEL_ID=809295405128089611
SUB_MOD_CHANNEL_ID=1077960085826961439

# --- OTHER ---
TWITCH_CHANNEL=chopaeng
WORKBOOK_NAME=ChoPaeng_Database
IS_PRODUCTION=false
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

# Chobot System

Simple bot for Animal Crossing. It works for Discord, Twitch, and has API for web.

## Description

Chobot is a unified system to help manage Animal Crossing communities. It watches island visitors to keep them safe, helps users find items and villagers, and connects Twitch chat with discord data. It uses Google Sheets to keep all information up to date.

### Features

* **Flight Logger (Security)**
    * Watch people who visit islands.
    * Send alert to staff if person is unknown.
    * Buttons for Admit, Warn, Kick, Ban.
    * Remove access role automatically if someone is warned.
    * Send moderation log to channel.

* **Discord & Twitch Utility**
    * Find items and villagers across sub-islands (!find, !villager).
    * Smart fuzzy search suggests correct names if you type wrong.
    * Check bot health with !status command.

* **Island Status (Dodo Codes)**
    * API reads Dodo.txt and Visitors.txt to show real-time island status.
    * Show if island is ONLINE, OFFLINE, or FULL.

* **Patreon Integration**
    * API can fetch and cache your Patreon posts for your website.

* **Data Management**
    * Auto-sync with Google Sheets every hour.
    * Fast local cache in cache_dump.json.

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

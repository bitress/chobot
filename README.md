# Chobot System

Chobot is bot for Animal Crossing. It works for Discord, Twitch, and has API.

## What it does

### Flight Logger
Watch island traffic and keep sub-islands safe.
- Track people arrive.
- Tell staff if traveler is unknown.
- Buttons for Admit, Warn, Kick, Ban.
- If warn user, it remove role automatic (ISLAND_ACCESS_ROLE).
- Send log to sapphire channel.

### Discord Bot
- !find <item>: help find items in sub islands.
- !villager <name>: help find where villager live.
- If item name wrong, it try suggest correct one.

### Twitch Bot
Talk to twitch chat and use same item data.

### API
Flask server for other apps to get island data.

### Data Manager
Pull data from Google Sheets and save to local file for speed.

## How set up

### Need
- Python 3.9+
- Discord bot token
- Twitch bot token
- Google sheet access

### .env file
Put tokens here:
```env
DISCORD_TOKEN=token_here
TWITCH_TOKEN=token_here
GUILD_ID=id_here
SUB_CATEGORY_ID=id_here
WORKBOOK_NAME=sheet_name
IS_PRODUCTION=true
```

### Run bot
```bash
python main.py
```

## Moderation
When bot see unknown person:
1. Show alert in mod channel.
2. Mod click button (Warn, Kick, Ban).
3. If Warn: remove role, save to db, send DM, log to channel.
4. If Kick/Ban: remove from server, send DM.

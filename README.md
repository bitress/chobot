# Chobot System

Chobot is bot for Animal Crossing. It works for Discord, Twitch, and has API for web.
No emojis here, just simple English.

## What it does

### Flight Logger (Security)
Watch people who visit islands and keep everything safe.
- **Track People**: When someone join island, bot check if it know them.
- **Alert Staff**: If bot not know person, it send message to staff channel.
- **Easy Buttons**: Staff click buttons like Admit, Warn, Kick, or Ban directly on alert.
- **Auto Remove Role**: If staff Warn someone, bot remove their access role (ISLAND_ACCESS_ROLE) automatic so they cannot enter again.
- **History**: All actions save to database and moderation log.

### Discord Bot (Utility)
- **!find <item>**: Find any item in sub islands. It look deep in database.
- **!villager <name>**: Find which island a villager live on.
- **Smart Search**: If you type name wrong, bot suggest correct names.
- **Status**: Type !status to see if bot is update and healthy.

### Twitch Bot
Streamer can use bot in Twitch chat. It use same data as Discord.

### API & Data
- **Flask Server**: Other apps can ask bot for island data.
- **Google Sheets**: Bot get all island data from Google Sheets workbook.
- **Local Cache**: Bot save data to file so it is very fast.

## How to use

### Things you need
- Python 3.9 or newer.
- Token for Discord bot.
- Token for Twitch bot.
- Access to Google Sheets.

### Setup .env
Make file named `.env` and put this:
```env
DISCORD_TOKEN=your_token
TWITCH_TOKEN=your_token
GUILD_ID=your_server_id
SUB_CATEGORY_ID=island_category_id
WORKBOOK_NAME=spreadsheet_name
IS_PRODUCTION=true
```

### Start it
```bash
python main.py
```

## How Warn/Kick/Ban works
When bot see unknown person:
1. It post alert in mod channel.
2. Mod click button.
3. **Warn**: Bot remove player roles, save warn to db, send DM to player, and send moderation log.
4. **Kick/Ban**: Bot kick or ban player from server and send them DM.

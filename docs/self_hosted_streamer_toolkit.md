# ChoBot Self-Hosted Streamer Toolkit

ChoBot is designed to run on the ACNH treasure island streamer's own PC or VPS.
It connects to the streamer's Discord server, watches configured Berichan/OrderBot
channels, captures Dodo/status updates, powers the local dashboard, tracks flight
logs, and optionally serves Twitch chat commands.

## First Run

1. Run `scripts\install.bat`.
2. Edit `.env`, or start the dashboard and use `/dashboard/setup/wizard`.
3. Run `scripts\start_all.bat`.
4. Open `http://localhost:8100/dashboard`.
5. Log in with `DASHBOARD_SECRET`.
6. Open `/dashboard/setup` to verify configuration.

The setup wizard writes `.env.local`, which overrides `.env` and is ignored by
git. Existing `.env.local` files are backed up before being replaced.
The wizard is step-based and can scan Discord channels to populate category,
OrderBot, Dodo board, and flight-log fields.

## Required Discord Settings

- `DISCORD_TOKEN`: token for the streamer's Discord bot.
- `GUILD_ID`: the streamer's Discord server ID.
- `SUB_CATEGORY_ID`: member/VIP island channel category.
- `FREE_CATEGORY_ID`: free island channel category, if used.
- `ORDERBOT_CHANNEL_IDS`: comma-separated channels where Berichan/OrderBot posts Dodo messages.
- `FLIGHT_LISTEN_CHANNEL_ID`: channel where flight arrival logs are posted.
- `FLIGHT_LOG_CHANNEL_ID`: channel where ChoBot posts moderation/XLog messages.

Optional:

- `ORDERBOT_AUTHOR_IDS`: restrict Dodo capture to specific bot user IDs.
- `TWITCH_TOKEN` and `TWITCH_CHANNEL`: enable Twitch commands.
- `FREE_DODO_BOARD_CHANNEL_ID`: channel where ChoBot publishes public Dodo board embeds.
- `DODO_STALE_MINUTES`: stale-code threshold for automatic cleanup.

## OrderBot/Berichan Capture

ChoBot reads configured OrderBot channels and parses:

- normal message content
- Discord embed titles
- embed descriptions
- embed fields
- embed footers

Capture statuses:

- `ONLINE`: Dodo code found.
- `REFRESHING`: gate/code refresh detected.
- `OFFLINE`: island closed/down/unavailable.
- `ORDER_STARTING`: order is starting but no final code yet.

Each capture updates the matching island row and is stored in recent capture
history for `/dashboard/setup`.

While the Discord bot is running, ChoBot clears stale locally captured Dodo codes
after `DODO_STALE_MINUTES`.

## Manual Operations

From `/dashboard/setup`, streamers can:

- open the setup wizard and save local configuration
- scan Discord channels and auto-fill likely category/log/channel IDs
- paste a sample OrderBot message and preview parser output
- manually set an island online/offline/refreshing
- enter or clear a Dodo code
- view recent Dodo captures
- edit Free Dodo Board embed templates

## Embed Customization

The Free Dodo Board embed template supports:

- title
- description
- footer
- banner/image URL
- online/refreshing/offline colors

Available placeholders:

- `{island}`
- `{island_raw}`
- `{dodo_code}`
- `{status}`
- `{visitors}`
- `{description}`
- `{island_url}`
- `{map_url}`

## Flight Logs

Flight logging remains local to the streamer's Discord server. Configure:

- `FLIGHT_LISTEN_CHANNEL_ID`
- `FREE_ISLAND_FLIGHT_LISTEN_CHANNEL_ID`
- `FLIGHT_LOG_CHANNEL_ID`
- `SUB_MOD_CHANNEL_ID`
- mod and island-access roles

The dashboard keeps XLog reports and analytics based on local flight tables.

## Twitch

Twitch is optional. If configured, ChoBot can answer item/villager search
commands in the streamer's Twitch chat using the same local item cache.

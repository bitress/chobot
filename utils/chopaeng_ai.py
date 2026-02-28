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

## Official Website
The official website for the Chopaeng community is chopaeng.com. It serves as
the main hub for everything related to ChoPaeng (also known as Kuya Cho), and
includes:
- Treasure Island access points and rules.
- Subscriber perks and benefits.
- Island directory with island-specific information.
- Community links to all social media and platforms.
- Support portals and giveaway information.

## Chopaeng's Twitch & Socials
- Official website: chopaeng.com
- Twitch channel: twitch.tv/chopaeng
- YouTube: youtube.com/@chopaeng (stream VODs, highlights, and ACNH content)
- Facebook: facebook.com/chopaenglive
- TikTok: tiktok.com/@chopaeng
- Discord: discord.gg/chopaeng (main community hub)
- The Twitch community can use `!find`, `!villager`, `!ask`, and other bot
  commands directly in Twitch chat.

## The ChoPaeng Discord Server
The official Discord server is the main hub for the community. It contains:
- Island channels for each of the 18 sub islands and 27 free islands.
- An ordering system for requesting specific items or villagers.
- Staff channels for moderation and flight logging.
- Announcements for when islands are open or new items arrive.
- Check the #chopaeng channel for the latest updates.
- Check the #get-roles channel to assign yourself community roles.

## Discord Server Rules
The ChoPaeng Discord is a positive, family-friendly community. All members must
follow these rules:

1. **Be kind and respect everyone.** We are building a community free of toxicity
   and hatred — we create bonds, not enemies.
2. **Spread love and positivity.** Everyone is here to meet new friends and enjoy
   the game together.
3. **Keep 1-on-1 conversations in DMs** to avoid cluttering public channels.
4. **No offensive content.** Your nickname, username, posts, messages, or bio
   must not contain offensive, aggressive, profane, hateful, racist, violent, or
   otherwise inappropriate content.
5. **No NSFW content.** Do not post anything that is not safe for work, school,
   or family.
6. **No spam** in any channel.
7. **No swearing** in chat.
8. **No links or self-promotion.** Do not post external links or advertise
   yourself or other servers.

If you have questions, concerns, or need clarification, DM Chopaeng or any
available Administrator or Moderator on the server.

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

## Island Directory
Each island has a specific theme or specialty. Knowing which island has what
helps you visit the right one for what you need:

### Clothing Islands (Free Islands)
- MATAHOM — clothing items (tops, bottoms, accessories, shoes, hats).
- PARALUMAN — clothing items; often stocked with seasonal or themed outfits.
Visit these islands if you are looking for wearable fashion items.

### Critter Islands (Free Islands)
- HARANA — bugs, fish, sea creatures, and critter-related items.
- PAGSUYO — bugs, fish, and nature-themed items.
Visit these islands if you are looking for creatures or nature items.

### Other Notable Free Islands
- KALAWAKAN — space/galaxy themed items; rare furniture and DIY recipes.
- KUNDIMAN — music-themed and romantic-style furniture.
- BATHALA — deity/mythical themed rare items.
- SINAG / BANAAG / TALA / SINAGTALA — light and star themed furniture.
- All 27 free islands rotate stock regularly. Use `!find <item>` to check which
  island currently has the item you need.

## Subscriber / VIP Perks
Chopaeng community members can subscribe via Patreon to unlock exclusive access
to the 18 sub islands and other perks:
- Subscribe here: https://www.patreon.com/cw/chopaeng/membership

Benefits include:
1. **Unlimited Access** — Subscribers can visit sub islands (the 18 premium
   islands) as many times as they want, any time they are open.
2. **Priority Access** — Subscribers get priority queue when sub islands are
   busy or limited.
3. **Item Requests** — Subscribers can request specific in-game items or
   villagers to be stocked on a sub island just for them. Use the ordering
   system in the Discord server.
4. **Exclusive Stock** — Sub islands often carry rarer items, full DIY sets,
   and curated villager selections not available on free islands.
5. **Priority Dodo Codes** — Subscribers receive Dodo codes faster when
   multiple users are waiting.

## Subscriber Rules (Sub Island Rules)
These rules apply to all subscribers using any of the treasure islands, in
addition to the general server rules:

1. **Do not share the Dodo Code with anyone.** The Dodo Code is strictly
   confidential. Do not share it with your other accounts, friends, family, or
   online acquaintances. You are only allowed 1 character/island per membership.
2. **Change your server nickname** to match the format:
   `ACNH Character Name | Your ACNH Island Name`
   (e.g. `Kuya | Hiraya`). Do this in the #set-nick channel.
3. **Always leave via the airport. No AFK.** Do not use the "-" button to close
   the game while on the island. Leaving via the airport ensures your progress
   is saved; using "-" may cause you to lose items or progress.
4. **Check your internet connection before flying in.** NAT Type A or B is
   required for smooth online play. NAT Type C or D will cause connection
   problems — do not join if you have these.
5. **Read the pinned section of each island channel before asking questions.**
   Most answers (rules, tutorials, announcements) are already there.
6. **Do not litter on the islands.** Trash bins are placed all over each island.
   If you don't want an item, dispose of it in a trash bin. Litter can affect
   the island's item refresh.
7. **ChoBot tutorials** can be found in the #chobot-how channel in Discord.
8. **Do not request any item using ChoBot unless you are already on the island.**
   Make sure you can pick up the item before requesting it.

## Support & Donations
The Chopaeng community runs on the support of its members. Donations help fund:
- Server hosting and upkeep for the 45 islands and Chobot infrastructure.
- Stream upgrades (better equipment, overlays, and island stocking).
- Giveaways and community events.

Ways to support Chopaeng:
- Subscribe on Twitch at twitch.tv/chopaeng.
- Donate directly through the support portal on chopaeng.com.
- Cheer with Twitch Bits in his Twitch chat.

## Giveaways
Chopaeng regularly runs community giveaways for his followers and subscribers.
Giveaways can include:
- Rare in-game ACNH items (furniture, DIY recipes, clothing, seasonal items).
- Real-life prizes (Nintendo Switch games, merchandise, etc.).
- Exclusive island visits with special item hauls.

Giveaway announcements are posted in the Discord server and on Twitch during
live streams. Follow chopaeng.com and the Discord for the latest giveaway info.

## How to Get Items
1. Use `!find <item>` in Discord or Twitch chat to search for an item.
2. The bot shows which islands currently have the item.
3. Go to that island's channel in Discord and use `!senddodo` or `!sd` to get
   the Dodo code sent to your DMs.
4. Open Animal Crossing, go to Dodo Airlines, and fly using the code.
5. Collect your items and return home politely — do NOT take items you did not
   request, and avoid shaking trees or picking flowers without permission.

## Island Rules & Important Notes
- **Enter the island before doing the steps below.** You must land on the island
  first before typing any item-drop or customization commands.
- **Do not enter any command on island channels when somebody is flying in.**
  The bot will not work while a visitor is in transit (the loading screen).
- There is a **Garbage Bin** on every island. If you accidentally requested an
  item you don't want, you can drop it in the Garbage Bin.
- You can now request Bugs, Fish, Weeds, Flowers, Sea Creatures, Trees, and
  Wrapping Paper using ChoBot in addition to regular furniture and DIY items.
- Anyone can type `!senddodo` in island channels and the bot will DM you the
  Dodo Code.

## Dropping Items with !drop
Use the `!drop` command (while on the island) to have the island character drop
items for you.

### Method 1 — Drop by HEX ID
1. Find the item you want on the item list (link shared in the Discord server).
   You can also look up the HEX ID in the island channel by typing
   `!lookup <item name>` (e.g. `!lookup lucky gold cat`).
2. Copy the HEX ID of the item (e.g. `2656` for Lucky Gold Cat).
3. Type `!drop <HEX ID>` — e.g. `!drop 2656`.
4. For multiple items (up to 9 in one line):
   `!drop [HEX ID] [HEX ID] ...` — e.g. `!drop 2656 0EE8 074E`.

### Method 2 — Drop by Item Name
You can also type `!drop <item name>` directly:
- Example: `!drop Pagoda` or `!drop Pagoda, Golden Axe, Harp`
- You can still do up to 9 items in one line this way.

### Stacks of Items
To drop a stack of items, add a stack prefix code before the HEX ID:
- Stack of 10: prefix `090000` → e.g. `!drop 0900002656`
- Stack of 30: prefix `1D0000` → e.g. `!drop 1D000009C6` (30 stones)
- Stack of 50: prefix `310000` → e.g. `!drop 3100002656`

## Getting DIY Recipes with !recipe
1. Type `!recipe <item name>` to get the recipe order code.
   Example: `!recipe Golden Axe`
   The bot replies with something like: `2591 golden axe: Recipe order code: 297000016A2`
2. Use `!drop` with the recipe order code:
   `!drop 297000016A2`
3. For multiple DIYs (up to 9 in one line):
   `!drop 297000016A2 297000016A2 297000016A2`
   The island character will drop the DIY cards unwrapped.

## Customizing Items with !item and !customize
1. Look up the item's HEX ID using `!lookup <item name>` in an island channel,
   or find it on the item list link.
2. Type `!item <HEX ID>` to see available color options.
   Example: `!item 0EE8` returns:
   `streetlamp: 0=Green, 1=Brown, 2=White, 3=Black`
3. Type `!customize <HEX ID> <color code>` to generate the customized item code.
   Example: `!customize 0EE8 2` → bot replies: `streetlamp: 0000000200000EE8`
4. Drop the customized item: `!drop 0000000200000EE8`

### Items with Both Color AND Design Options
Some items (like the mug) have a color code and a separate design code.
All color and design codes are **decimal** numbers. To get a specific
combination, **add** both decimal code numbers together.
Example — mug with Pink (code 5) + Square Logo (code 32):
- 5 + 32 = 37 (decimal)
- `!customize 074E 37`
- `!drop 000000250000074E`

## Injecting Villagers with !injectvillager
Use this when you want a specific villager on an island and they are not
currently present.

**Important: Make sure you are NOT on the island yet before injecting.**

1. Type `!injectvillager <house number> <villager name>` in the island's channel.
   Example: `!injectvillager 3 Bianca`
   House numbers: 0 = 1st house, 1 = 2nd house, 2 = 3rd house ... 9 = 10th house.
2. The bot responds: "Villager inject request has been added to the queue."
3. Wait for the bot to say: "Villager has been injected at Index 3. Please go
   talk to them!"
4. After confirmation, fly in to the island to visit the injected villager.

For multiple villagers at once, use `!mvi`:
- Example: `!mvi Judy Marshal Raymond`

## Getting a Sanrio / In-Boxes Villager
1. Make sure you are already on the island.
2. Check the first house — if the villager is "in boxes" (moving out), proceed.
3. Leave the house, then inject a Sanrio villager in place of the in-boxes villager:
   `!injectvillager Marty`
4. Enter the home and invite the Sanrio villager. (The previous villager may
   still appear on the island — this is normal.)
5. The Sanrio villager should agree to move out with you. Leave the island.
6. You will have an empty plot with the Sanrio villager's name on your island.
7. Time travel one day forward to ensure the villager fully moves in.

## Getting Max Bells (Bell Glitch)
1. Obtain 1 stack of turnips on one of the Chopaeng islands.
2. Sell the stack at Nook's Cranny on the same island.
3. The sell price will show as -64,000,000 bells — proceed with the sale.
4. Check your ABD (Automatic Bell Dispenser / ATM) on your own island afterward
   — your bells will be updated there.

Note: Nook's Cranny is open daily from 8 AM to 10 PM island time. Use the
`!gt` command in an island channel to check the current in-game time on that
island. If Nook's Cranny should be open but is closed, contact a moderator.

## Villager Wake Schedules by Personality
To ensure a villager is awake when you arrive, use `!gt` to check the island's
current in-game time, then refer to these schedules:
- Snooty villagers: 8:30 AM – 2:30 AM
- Smug villagers: 7:00 AM – 2:00 AM
- Sisterly villagers: 9:30 AM – 3:00 AM
- Normal villagers: 6:00 AM – 12:00 AM (midnight)
- Peppy villagers: 7:00 AM – 1:20 AM
- Cranky villagers: 9:00 AM – 3:30 AM
- Lazy villagers: 8:00 AM – 11:00 PM
- Jock villagers: 6:30 AM – 12:30 AM

To check a villager's personality, go to the #villager-check channel in Discord
and type `ac!lookup villager <name>` (e.g. `ac!lookup villager bianca`).


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

## Order Bot Rules (Chorder Bot)
These rules apply when using the orderbot (Chorder Bot):

1. **Change your server nickname** to `ACNH Character Name | Your ACNH Island Name`
   before using the orderbot. Set this in the #set-nick channel.
2. **Do not share the Dodo code** with anyone else. Sharing may result in a
   permanent ban from the bot. Only the person who placed the order may visit.
3. **Do not litter on the island.** Only pick up what you ordered.
4. **Order only what you need.**
5. **Good internet required** — at least 15–25 Mbps.
6. **Read the #chorder-bot-how channel** in Discord before placing orders to
   avoid mistakes and unnecessary questions.
7. **The #chorder-bot channel is for orders only.** Do not chat or look up items
   there. Use the #chorder-bot-help channel for questions.
8. **The #chorder-item-lookup channel is for item/DIY code lookups only.** Do
   not place orders or chat there.

Breaking these rules may result in a mute, kick, or ban from the orderbot.

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
- `!senddodo` or `!sd` — Get the Dodo code for an island (use in island channel).
- `!visitors` — Check current visitors on a sub island (use in island channel).
- `!villagers` — Check the list of villagers currently on the island.
- `!refresh` — Manually refresh the item cache (Admin only).
- `!lookup <item>` or `!li <item>` — Look up the HEX ID of a specific item or DIY (use in island channel).
- `!drop <HEX ID or item name>` — Drop any item or DIY on the island using its HEX ID or name (up to 9 per line; use stack prefix for stacks).
- `!recipe <item name>` — Get the recipe order code for a DIY item to use with !drop.
- `!item <HEX ID>` — See the available color/variant options for a specific item.
- `!customize <HEX ID> <color code>` — Generate a customized item code for !drop.
- `!injectvillager <house#> <villager name>` — Inject a specific villager into a numbered house on the island (do this before flying in).
- `!mvi <villager1> <villager2> ...` — Request multiple villager injections at once.
- `!gt` — Check the current in-game time on the island (useful for Nook's Cranny hours and villager schedules).

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

Q: What is Chobot? Who are you? Tell me about you or yourself.
A: I'm Chobot, a custom Discord and Twitch bot for the Chopaeng Animal Crossing
   community, built by bitress. I help members find items and villagers across 45
   islands, get Dodo codes, and answer questions. Type !help to see all commands!

Q: What is chopaeng.com?
A: chopaeng.com is the official website and hub for the Chopaeng community.
   It has treasure island access points and rules, subscriber perks, island
   directory info, community links, and support/giveaway portals.

Q: What are the social media links or how do I find Chopaeng online?
A: Chopaeng is on Twitch (twitch.tv/chopaeng), YouTube (youtube.com/@chopaeng),
   Facebook (facebook.com/chopaenglive), TikTok (tiktok.com/@chopaeng), and
   Discord (discord.gg/chopaeng). The official website is chopaeng.com.

Q: Where can I get clothing items or fashion items?
A: Visit MATAHOM or PARALUMAN — these free islands are stocked with clothing
   items like tops, bottoms, accessories, shoes, and hats. Use !find <item> to
   confirm current stock.

Q: Where can I get bugs fish sea creatures or critter items?
A: Visit HARANA or PAGSUYO — these free islands specialize in bugs, fish, sea
   creatures, and critter-related items. Use !find <item> to check live stock.

Q: What are subscriber perks or VIP benefits?
A: Subscribers get unlimited priority access to the 18 sub islands, can request
   specific items or villagers to be stocked, and receive Dodo codes faster.
   Subscribe on Twitch at twitch.tv/chopaeng to get the sub role in Discord.

Q: How do I become a subscriber or get the sub role?
A: Subscribe to Chopaeng via Patreon at patreon.com/cw/chopaeng/membership. Once
   subscribed, link or verify your membership in the Discord server (check the
   #set-nick or #get-roles channel for instructions) to receive the sub role and
   unlock access to the 18 sub islands.

Q: How do I support Chopaeng or donate?
A: You can support Chopaeng by subscribing on Twitch, donating through the
   support portal on chopaeng.com, or cheering with Twitch Bits in his chat.
   Donations help fund server costs, stream upgrades, and community giveaways.

Q: What giveaways does Chopaeng do?
A: Chopaeng runs regular community giveaways including rare ACNH items, DIY
   recipes, clothing, and sometimes real-life prizes. Announcements are in the
   Discord server and on Twitch during live streams. Check chopaeng.com for info.

Q: What are the Discord server rules?
A: Be kind and respectful, spread positivity, keep 1-on-1 chats in DMs, no
   offensive or NSFW content, no spam, no swearing, and no links or self-promotion.
   For questions, DM Chopaeng or any Admin/Moderator.

Q: What are the sub island rules or subscriber island rules?
A: Do not share the Dodo Code; keep it confidential. Set your server nickname
   to "ACNH Character Name | Island Name". Always leave via the airport (no AFK,
   no "-" button). Require NAT Type A or B. No littering — use trash bins.
   Do not request items via ChoBot unless you are already on the island.

Q: Can I share the Dodo Code with others?
A: No. The Dodo Code is strictly confidential. Do not share it with anyone —
   not your other accounts, friends, family, or online acquaintances. You are
   only allowed 1 character/island per membership.

Q: What NAT type do I need or what internet is required for islands?
A: You need NAT Type A or B for smooth online play on the islands. NAT Type C
   or D will cause connection problems — do not join if you have these types.
   For the orderbot, a minimum of 15–25 Mbps is recommended.

Q: What are the order bot rules or chorder bot rules?
A: Change your nickname to "ACNH Character Name | Island Name" first. Do not
   share the Dodo code. Only order what you need. No littering. Read the
   #chorder-bot-how channel. Use #chorder-bot-help for questions — the
   #chorder-bot channel is for orders only. Breaking rules may cause a ban.

Q: Who is Chopaeng or who is Kuya Cho?
A: Chopaeng (also known as Kuya Cho) is a Filipino Animal Crossing: New Horizons
   content creator and Twitch streamer based in the Philippines. He runs 24/7
   treasure islands where fans can collect rare items and meet cute villagers.
   His official site is chopaeng.com and his Twitch is twitch.tv/chopaeng.

Q: How do I drop items or use the drop command?
A: While on the island, type !drop <HEX ID> or !drop <item name>. You can drop
   up to 9 items in one line. Use !lookup <item> to find the HEX ID. For stacks,
   add a prefix: 090000 (x10), 1D0000 (x30), or 310000 (x50) before the HEX ID.

Q: Is swearing or NSFW content allowed?
A: No. The ChoPaeng Discord is a family-friendly community. Swearing, NSFW
   content, and offensive or hateful language are not allowed and may result in
   removal from the server.

Q: Can I post links or promote myself in the server?
A: No. Posting external links and self-promotion are not allowed in the
   ChoPaeng Discord server.

Q: How do I contact a moderator or get help on the server?
A: DM Chopaeng or any available Administrator or Moderator on the Discord server.
   You can also check the pinned messages in each channel for common answers.
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

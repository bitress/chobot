"""
Configuration Module
Loads and validates all environment variables
"""

import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    """Application configuration"""

    # Twitch
    TWITCH_TOKEN = os.getenv('TWITCH_TOKEN')
    TWITCH_CHANNEL = os.getenv('TWITCH_CHANNEL')

    # Discord
    DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
    GUILD_ID = int(os.getenv('GUILD_ID')) if os.getenv('GUILD_ID') else None
    CATEGORY_ID = int(os.getenv('SUB_CATEGORY_ID')) if os.getenv('SUB_CATEGORY_ID') else None
    LOG_CHANNEL_ID = int(os.getenv('CHANNEL_ID')) if os.getenv('CHANNEL_ID') else None
    FLIGHT_LISTEN_CHANNEL_ID = 809295405128089611
    FLIGHT_LOG_CHANNEL_ID = 1451990354634080446
    IGNORE_CHANNEL_ID = 809295405128089611

    # Patreon
    PATREON_TOKEN = os.getenv("PATREON_TOKEN")
    PATREON_CAMPAIGN_ID = os.getenv("PATREON_CAMPAIGN_ID")

    # Google Sheets
    WORKBOOK_NAME = os.getenv('WORKBOOK_NAME')
    JSON_KEYFILE = 'service_account.json'
    CACHE_REFRESH_HOURS = 1

    # Villagers & Dodo
    VILLAGERS_DIR = os.getenv('VILLAGERS_DIR')
    TWITCH_VILLAGERS_DIR = os.getenv('TWITCH_VILLAGERS_DIR')
    DIR_FREE = os.getenv('TWITCH_VILLAGERS_DIR')
    DIR_VIP = os.getenv('VILLAGERS_DIR')

    # Island Lists
    SUB_ISLANDS = [
        "Alapaap", "Aruga", "Bahaghari", "Bituin", "Bonita", "Dakila",
        "Dalisay", "Diwa", "Gabay", "Galak", "Hiraya", "Kalangitan",
        "Lakan", "Likha", "Malaya", "Marahuyo", "Pangarap", "Tagumpay"
    ]

    TWITCH_SUB_ISLANDS = SUB_ISLANDS  # Same list — single source of truth

    FREE_ISLANDS = [
        "Kakanggata", "Kalawakan", "Kundiman", "Kilig", "Bathala", "Dalangin",
        "Gunita", "Kaulayaw", "Tala", "Sinagtala", "Tadhana", "Maharlika",
        "Pagsamo", "Harana", "Pagsuyo", "Matahom", "Paraluman", "Babaylan",
        "Amihan", "Silakbo", "Dangal", "Kariktan", "Tinig", "Banaag",
        "Sinag", "Giting", "Marilag"
    ]

    # Discord Embed Assets
    EMOJI_SEARCH = "<a:heartside:784055539881214002>"
    EMOJI_FAIL = "<a:CampWarning:1172346431542140961>"
    STAR_PINK = "<a:starpink:784055540321091584>"
    FOOTER_LINE = "https://i.ibb.co/wybN7Xn/lg4jVMT.gif"
    INDENT = "<a:starsparkle1:766724172474220574>"
    DROPBOT_INFO = "Try using <@&807096897453031425> to drop the specific item.\nCheck <#782872507551055892> for help."
    DEFAULT_PFP = "https://static-cdn.jtvnw.net/jtv_user_pictures/cf6b6d6c-f9b6-4bad-b034-391d7d32b9c3-profile_image-70x70.png"

    @classmethod
    def validate(cls):
        """Validate required environment variables"""
        required_vars = [
            'TWITCH_TOKEN', 'TWITCH_CHANNEL', 'DISCORD_TOKEN',
            'WORKBOOK_NAME', 'GUILD_ID', 'CATEGORY_ID',
            'PATREON_TOKEN', 'PATREON_CAMPAIGN_ID'
        ]

        missing = []
        for var in required_vars:
            if var in ['GUILD_ID', 'CATEGORY_ID']:
                if getattr(cls, var) is None:
                    missing.append(var)
            elif not os.getenv(var):
                missing.append(var)

        if missing:
            raise ValueError(f"Missing required environment variables: {', '.join(missing)}")

        return True
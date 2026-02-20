"""
Configuration Module
Loads and validates all environment variables
"""

import os
import logging
from dotenv import load_dotenv

load_dotenv()

if not os.getenv('TWITCH_TOKEN') and os.getenv('\ufeffTWITCH_TOKEN'):
    for key in list(os.environ.keys()):
        if key.startswith('\ufeff'):
            clean_key = key.lstrip('\ufeff')
            os.environ[clean_key] = os.environ.pop(key)

class Config:
    """Application configuration"""

    @staticmethod
    def _get_int(key, default=None):
        """Helper to safely fetch and convert env vars to int"""
        val = os.getenv(key)
        if val and val.strip().isdigit():
            return int(val)
        return default

    # General Config
    IS_PRODUCTION = os.getenv('IS_PRODUCTION', 'true').lower() == 'true'

    # Auth Tokens
    TWITCH_TOKEN = os.getenv('TWITCH_TOKEN')
    TWITCH_CHANNEL = os.getenv('TWITCH_CHANNEL')
    DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')

    # Discord IDs (Safe Integer Casting)
    GUILD_ID = _get_int('GUILD_ID')
    # Note: Using SUB_CATEGORY_ID env var for CATEGORY_ID per your snippet
    CATEGORY_ID = _get_int('SUB_CATEGORY_ID')
    # Note: Using CHANNEL_ID env var for LOG_CHANNEL_ID per your snippet
    LOG_CHANNEL_ID = _get_int('CHANNEL_ID') 
    ISLAND_ACCESS_ROLE = _get_int('ISLAND_ACCESS_ROLE')

    # Environment Specific Channels
    if IS_PRODUCTION:
        FLIGHT_LISTEN_CHANNEL_ID = _get_int('FLIGHT_LISTEN_CHANNEL_ID')
        FLIGHT_LOG_CHANNEL_ID = _get_int('FLIGHT_LOG_CHANNEL_ID')
        IGNORE_CHANNEL_ID = _get_int('IGNORE_CHANNEL_ID')
        SUB_MOD_CHANNEL_ID = _get_int('SUB_MOD_CHANNEL_ID')
    else:
        # Development / Fallback IDs
        FLIGHT_LISTEN_CHANNEL_ID = 1473286697461616732
        FLIGHT_LOG_CHANNEL_ID = 1473286727224524915
        IGNORE_CHANNEL_ID = 809295405128089611
        SUB_MOD_CHANNEL_ID = 1473286794995830845

    # Patreon
    PATREON_TOKEN = os.getenv("PATREON_TOKEN")
    PATREON_CAMPAIGN_ID = os.getenv("PATREON_CAMPAIGN_ID")

    # Nookipedia
    NOOKIPEDIA_KEY = os.getenv("NOOKIPEDIA_KEY")

    # Google Sheets
    WORKBOOK_NAME = os.getenv('WORKBOOK_NAME')
    JSON_KEYFILE = 'service_account.json'
    CACHE_REFRESH_HOURS = 1

    # Villagers & Dodo Directories
    VILLAGERS_DIR = os.getenv('VILLAGERS_DIR')
    TWITCH_VILLAGERS_DIR = os.getenv('TWITCH_VILLAGERS_DIR')

    # Logic: Free users access Twitch dir, VIPs access standard dir
    DIR_FREE = TWITCH_VILLAGERS_DIR
    DIR_VIP = VILLAGERS_DIR

    # Island Lists
    SUB_ISLANDS = [
        "Alapaap", "Aruga", "Bahaghari", "Bituin", "Bonita", "Dakila",
        "Dalisay", "Diwa", "Gabay", "Galak", "Hiraya", "Kalangitan",
        "Lakan", "Likha", "Malaya", "Marahuyo", "Pangarap", "Tagumpay"
    ]

    TWITCH_SUB_ISLANDS = SUB_ISLANDS  

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
        """Validate required environment variables exist and are not empty"""
        required_vars = [
            'TWITCH_TOKEN', 'TWITCH_CHANNEL', 'DISCORD_TOKEN',
            'WORKBOOK_NAME', 'GUILD_ID', 'CATEGORY_ID',
            'PATREON_TOKEN', 'PATREON_CAMPAIGN_ID'
        ]

        missing = []
        for var in required_vars:
            val = getattr(cls, var, None)
            
            # Check for None (Missing)
            if val is None:
                missing.append(var)
            # Check for Empty Strings (if it's a string)
            elif isinstance(val, str) and not val.strip():
                missing.append(var)

        if missing:
            raise ValueError(f"Missing required environment variables: {', '.join(missing)}")

        return True

    @classmethod
    def get_island_bot_ids(cls) -> dict:
        """Return a dict mapping each sub island name to its bot user ID (int), if configured.

        Per-island bot user IDs are optional. Set one env var per sub island:
            ISLAND_BOT_<UPPERCASED_ISLAND_NAME>=<discord_user_id>
        Example:
            ISLAND_BOT_ALAPAAP=123456789012345678
            ISLAND_BOT_ARUGA=234567890123456789
        Islands with no env var will fall back to scanning any bot message in the channel.
        """
        result = {}
        for island in cls.SUB_ISLANDS:
            env_key = f"ISLAND_BOT_{island.upper()}"
            val = os.getenv(env_key)
            if val and val.strip().isdigit():
                result[island] = int(val)
        return result
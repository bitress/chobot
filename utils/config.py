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
    CATEGORY_ID = _get_int('SUB_CATEGORY_ID')
    FREE_CATEGORY_ID = _get_int('FREE_CATEGORY_ID')
    LOG_CHANNEL_ID = _get_int('CHANNEL_ID')
    ISLAND_ACCESS_ROLE = _get_int('ISLAND_ACCESS_ROLE', 788749941949464577)
    FIND_BOT_CHANNEL_ID = _get_int('FIND_BOT_CHANNEL_ID')
    AI_LEARN_CHANNEL_ID = _get_int('AI_LEARN_CHANNEL_ID', 907642922906845264)

    # Environment Specific Channels
    if IS_PRODUCTION:
        FLIGHT_LISTEN_CHANNEL_ID = _get_int('FLIGHT_LISTEN_CHANNEL_ID')
        FREE_ISLAND_FLIGHT_LISTEN_CHANNEL_ID = _get_int('FREE_ISLAND_FLIGHT_LISTEN_CHANNEL_ID')
        FLIGHT_LOG_CHANNEL_ID = _get_int('FLIGHT_LOG_CHANNEL_ID')
        IGNORE_CHANNEL_ID = _get_int('IGNORE_CHANNEL_ID')
        SUB_MOD_CHANNEL_ID = _get_int('SUB_MOD_CHANNEL_ID')
        XLOG_VERBOSE_CHANNEL_ID = _get_int('XLOG_VERBOSE_CHANNEL_ID', 1486899475631968367)
    else:
        # Development / Fallback IDs
        FLIGHT_LISTEN_CHANNEL_ID = 1473286697461616732
        FREE_ISLAND_FLIGHT_LISTEN_CHANNEL_ID = None
        FLIGHT_LOG_CHANNEL_ID = 1473286727224524915
        IGNORE_CHANNEL_ID = 809295405128089611
        SUB_MOD_CHANNEL_ID = 1473286794995830845
        XLOG_VERBOSE_CHANNEL_ID = 1491101080430579964

    # Patreon
    PATREON_TOKEN = os.getenv("PATREON_TOKEN")
    PATREON_CAMPAIGN_ID = os.getenv("PATREON_CAMPAIGN_ID")

    # Nookipedia
    NOOKIPEDIA_KEY = os.getenv("NOOKIPEDIA_KEY")

    # AI providers (optional)
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
    OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "").strip()
    AI_PROVIDER = os.getenv("AI_PROVIDER", "auto").strip().lower()
    GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
    OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

    # Web Dashboard (mod-only)
    DASHBOARD_SECRET = os.getenv("DASHBOARD_SECRET", "")

    # Flask session signing key.
    # If unset, a cryptographically random key is generated each process startup
    # (browser sessions will be lost on restart).
    # Set FLASK_SECRET_KEY explicitly in .env for persistent sessions.
    FLASK_SECRET_KEY: str = os.getenv("FLASK_SECRET_KEY") or __import__("secrets").token_hex(32)

    # Discord OAuth2 — for dashboard login via Discord.
    # Register an application at https://discord.com/developers/applications,
    # set OAuth2 → Redirects to your dashboard's callback URL:
    #   https://your-domain/dashboard/oauth2/callback
    # Only DISCORD_CLIENT_ID and DISCORD_CLIENT_SECRET need to be set here;
    # the redirect URI is derived automatically from the incoming request.
    DISCORD_CLIENT_ID     = os.getenv("DISCORD_CLIENT_ID", "")
    DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "")

    # Senior Mod role ID for dashboard access via Discord OAuth.
    # Any member holding this role (or having the Discord Administrator permission)
    # gets full access.  Must be set in your .env — role IDs are unique per server.
    ADMIN_ROLE_ID = _get_int("ADMIN_ROLE_ID")

    # Cloudflare R2 (S3-compatible) — for island map uploads
    # Endpoint format: https://<account_id>.r2.cloudflarestorage.com
    R2_ACCOUNT_ID       = os.getenv("R2_ACCOUNT_ID", "")
    R2_ACCESS_KEY_ID    = os.getenv("R2_ACCESS_KEY_ID", "")
    R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY", "")
    R2_BUCKET_NAME      = os.getenv("R2_BUCKET_NAME", "chobot-maps")
    # Public base URL for uploaded files (e.g. https://pub-xxx.r2.dev or custom domain)
    R2_PUBLIC_URL       = os.getenv("R2_PUBLIC_URL", "")

    # MariaDB migration (optional)
    MARIADB_HOST = os.getenv("MARIADB_HOST", "")
    MARIADB_PORT = _get_int("MARIADB_PORT", 3306)
    MARIADB_USER = os.getenv("MARIADB_USER", "")
    MARIADB_PASSWORD = os.getenv("MARIADB_PASSWORD", "")
    MARIADB_DATABASE = os.getenv("MARIADB_DATABASE", "chobot")
    MARIADB_TRUNCATE_BEFORE_IMPORT = os.getenv("MARIADB_TRUNCATE_BEFORE_IMPORT", "true").strip().lower() == "true"

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

    # Island Lists (fallback defaults; dynamically updated at runtime from Discord sub-category)
    SUB_ISLANDS = [
        "Adhika", "Alapaap", "Aruga", "Bahaghari", "Bituin", "Bonita", "Dakila",
        "Dalisay", "Diwa", "Gabay", "Galak", "Giliw", "Hiraya", "Kalangitan",
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

    ISLAND_BOT_ROLE_ID = _get_int('ISLAND_BOT_ROLE_ID')

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


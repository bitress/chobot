"""
Main Entry Point
Unified Bot Application Runner

Starts all services:
- Flask API Server (thread)
- Twitch Bot (thread w/ its own asyncio loop)
- Discord Command Bot (main asyncio loop)
"""

import os
import sys
import asyncio
import threading
import logging
import traceback
import signal
from typing import Optional

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from utils import Config, DataManager
from bots import TwitchBot, DiscordCommandBot
from bots.flight_logger import FlightLoggerCog
from api import run_flask_app, set_data_manager

# ============================================================================
# LOGGING
# ============================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("Main")

# ============================================================================
# SHARED STOP FLAG
# ============================================================================
STOP_EVENT = threading.Event()


# ============================================================================
# THREAD RUNNERS
# ============================================================================
def run_flask(data_manager: DataManager):
    """Run Flask API server in a thread"""
    try:
        logger.info("[FLASK] Starting Flask API...")
        set_data_manager(data_manager)
        run_flask_app(host="0.0.0.0", port=8100)
    except Exception as e:
        logger.error(f"[FLASK] Critical error: {e}")
        logger.error(traceback.format_exc())
        STOP_EVENT.set()


def run_twitch(data_manager: DataManager):
    """Run Twitch bot in a thread with its own event loop"""
    loop: Optional[asyncio.AbstractEventLoop] = None
    try:
        logger.info("[TWITCH] Starting Twitch bot...")

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        twitch_bot = TwitchBot(data_manager)

        loop.run_until_complete(twitch_bot.run())

    except Exception as e:
        logger.error(f"[TWITCH] Critical error: {e}")
        logger.error(traceback.format_exc())
        STOP_EVENT.set()
    finally:
        try:
            if loop and not loop.is_closed():
                loop.stop()
                loop.close()
        except Exception:
            pass


# ============================================================================
# DISCORD (MAIN LOOP)
# ============================================================================
async def run_discord(data_manager: DataManager):
    discord_bot: Optional[DiscordCommandBot] = None
    try:
        logger.info("[DISCORD] Starting Discord bot...")
        discord_bot = DiscordCommandBot(data_manager)

        await discord_bot.add_cog(FlightLoggerCog(discord_bot))

        async def stop_watcher():
            while not STOP_EVENT.is_set():
                await asyncio.sleep(0.5)
            logger.warning("[DISCORD] Stop signal received, closing bot...")
            await discord_bot.close()

        watcher_task = asyncio.create_task(stop_watcher())

        await discord_bot.start(Config.DISCORD_TOKEN)

        watcher_task.cancel()

    except Exception as e:
        logger.error(f"[DISCORD] Critical error: {e}")
        logger.error(traceback.format_exc())
        STOP_EVENT.set()
        if discord_bot:
            try:
                await discord_bot.close()
            except Exception:
                pass


# ============================================================================
# MAIN
# ============================================================================
def main():
    logger.info("=" * 70)
    logger.info("UNIFIED BOT APPLICATION STARTING")
    logger.info("=" * 70)

    # Validate configuration
    try:
        Config.validate()
        logger.info("[CONFIG] All environment variables validated ✓")
    except ValueError as e:
        logger.critical(f"[CONFIG] Configuration error: {e}")
        sys.exit(1)

    # Init shared data manager
    logger.info("[DATA] Initializing data manager...")
    data_manager = DataManager(
        workbook_name=Config.WORKBOOK_NAME,
        json_keyfile=Config.JSON_KEYFILE,
        cache_refresh_hours=Config.CACHE_REFRESH_HOURS,
    )

    # Initial cache update
    logger.info("[DATA] Loading initial cache...")
    if not data_manager.cache:
        logger.info("[DATA] No local cache found. Fetching from Google Sheets...")
        data_manager.update_cache()
    else:
        logger.info(f"[DATA] Local cache loaded successfully ({len(data_manager.cache)} items). Skipping initial fetch.")

    logger.info(f"[DATA] Cache status: {len(data_manager.cache)} items ready ✓")

    # Handle SIGTERM/SIGINT for graceful shutdown (works on Linux/macOS; Windows SIGTERM is limited)
    def _handle_signal(signum, frame):
        logger.warning(f"[MAIN] Signal {signum} received. Shutting down...")
        STOP_EVENT.set()

    try:
        signal.signal(signal.SIGINT, _handle_signal)
        signal.signal(signal.SIGTERM, _handle_signal)
    except Exception:
        pass

    # Start Flask thread (non-daemon so we can join cleanly)
    flask_thread = threading.Thread(target=run_flask, args=(data_manager,), name="FlaskThread")
    flask_thread.start()
    logger.info("[MAIN] Flask API thread started ✓")

    # Start Twitch thread (non-daemon so we can join cleanly)
    twitch_thread = threading.Thread(target=run_twitch, args=(data_manager,), name="TwitchThread")
    twitch_thread.start()
    logger.info("[MAIN] Twitch bot thread started ✓")

    # Run Discord in main thread (async)
    try:
        asyncio.run(run_discord(data_manager))
    except KeyboardInterrupt:
        logger.info("[MAIN] Shutdown signal received (Ctrl+C)")
        STOP_EVENT.set()
    except Exception as e:
        logger.critical(f"[MAIN] Critical error: {e}")
        logger.critical(traceback.format_exc())
        STOP_EVENT.set()
    finally:
        # Give threads a moment to stop if they can
        STOP_EVENT.set()

        # Best-effort join (don’t hang forever)
        for t in (twitch_thread, flask_thread):
            try:
                t.join(timeout=5)
            except Exception:
                pass

        logger.info("=" * 70)
        logger.info("APPLICATION SHUTDOWN COMPLETE")
        logger.info("=" * 70)


if __name__ == "__main__":
    main()

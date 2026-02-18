"""
Main Entry Point
Unified Bot Application Runner

Starts selected services:
- Flask API Server (thread)
- Twitch Bot (thread w/ its own asyncio loop)
- Discord Command Bot (main asyncio loop)
- Flight Logger only (Discord bot with FlightLoggerCog only)
- Twitch Find only (Twitch bot in lightweight/find mode)

Usage:
    python main.py                      # Run ALL services
    python main.py all                  # Run ALL services
    python main.py flask                # Flask API only
    python main.py twitch               # Twitch bot (full)
    python main.py twitch-find          # Twitch bot (find commands only)
    python main.py discord              # Discord bot (with all cogs)
    python main.py discord-find         # Discord bot (find/search cogs only)
    python main.py flight-logger        # Discord bot with FlightLoggerCog only
    python main.py flask twitch-find    # Flask + Twitch find only
    ... any combination
"""

import os
import sys
import asyncio
import threading
import logging
import traceback
import signal
from typing import Optional, Set

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from utils import Config, DataManager
from bots import TwitchBot, DiscordCommandBot
from bots.flight_logger import FlightLoggerCog
from api import run_flask_app, set_data_manager

# ============================================================================
# CONSTANTS
# ============================================================================
VALID_SERVICES = {
    "all", "flask",
    "twitch", "twitch-find",
    "discord", "discord-find", "flight-logger",
}

SERVICE_DESCRIPTIONS = {
    "all":            "All services (Flask + Twitch + Discord w/ all cogs)",
    "flask":          "Flask API server",
    "twitch":         "Twitch bot (full, all commands)",
    "twitch-find":    "Twitch bot (find/search commands only)",
    "discord":        "Discord bot (all cogs including FlightLogger)",
    "discord-find":   "Discord bot (find/search cogs only)",
    "flight-logger":  "Discord bot with FlightLoggerCog only",
}

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
# CLI PARSING
# ============================================================================
def parse_services(args: list[str]) -> Set[str]:
    """Parse CLI arguments into a set of services to run."""
    if len(args) <= 1:
        return {"all"}

    requested = set()
    for arg in args[1:]:
        service = arg.lower().strip()
        if service in ("--help", "-h"):
            print_help()
            sys.exit(0)
        if service not in VALID_SERVICES:
            print(f"✗ Unknown service: '{service}'")
            print(f"  Valid options: {', '.join(sorted(VALID_SERVICES))}")
            print(f"  Run with --help for details.")
            sys.exit(1)
        requested.add(service)

    # "all" overrides everything
    if "all" in requested:
        return {"all"}

    # Resolve conflicts: full mode supersedes limited mode
    if "discord" in requested:
        for subset in ("flight-logger", "discord-find"):
            if subset in requested:
                logger.info(f"[CLI] 'discord' already includes '{subset}'; ignoring.")
                requested.discard(subset)

    if "twitch" in requested and "twitch-find" in requested:
        logger.info("[CLI] 'twitch' already includes 'twitch-find'; ignoring.")
        requested.discard("twitch-find")

    return requested


def print_help():
    print(__doc__)
    print("Available services:")
    for name, desc in SERVICE_DESCRIPTIONS.items():
        print(f"  {name:<18} {desc}")
    print()
    print("Examples:")
    print("  python main.py                        # all services")
    print("  python main.py flask twitch-find       # Flask API + Twitch find only")
    print("  python main.py flight-logger           # Discord with FlightLogger only")
    print("  python main.py discord-find twitch-find # Both bots in find-only mode")


def expand_services(requested: Set[str]) -> dict:
    """Expand service set into structured flags."""
    if "all" in requested:
        return {
            "flask": True,
            "twitch": True,
            "twitch_find_only": False,
            "discord": True,
            "discord_find_only": False,
            "flight_logger_only": False,
        }

    return {
        "flask": "flask" in requested,
        "twitch": "twitch" in requested,
        "twitch_find_only": "twitch-find" in requested,
        "discord": "discord" in requested,
        "discord_find_only": "discord-find" in requested,
        "flight_logger_only": "flight-logger" in requested,
    }


# ============================================================================
# THREAD RUNNERS
# ============================================================================
def run_flask(data_manager: DataManager):
    """Run Flask API server in a thread."""
    try:
        logger.info("[FLASK] Starting Flask API...")
        set_data_manager(data_manager)
        run_flask_app(host="0.0.0.0", port=8100)
    except Exception as e:
        logger.error(f"[FLASK] Critical error: {e}")
        logger.error(traceback.format_exc())
        STOP_EVENT.set()


def run_twitch(data_manager: DataManager, find_only: bool = False):
    """Run Twitch bot in a thread with its own event loop."""
    loop: Optional[asyncio.AbstractEventLoop] = None
    try:
        mode = "find-only" if find_only else "full"
        logger.info(f"[TWITCH] Starting Twitch bot ({mode} mode)...")

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
# DISCORD RUNNERS
# ============================================================================
async def run_discord(
        data_manager: DataManager,
        flight_logger_only: bool = False,
        find_only: bool = False,
):
    """Run Discord bot on the main asyncio loop."""
    discord_bot: Optional[DiscordCommandBot] = None
    try:
        if flight_logger_only:
            mode = "FlightLogger-only"
        elif find_only:
            mode = "find-only"
        else:
            mode = "full"
        logger.info(f"[DISCORD] Starting Discord bot ({mode} mode)...")

        discord_bot = DiscordCommandBot(data_manager)

        if flight_logger_only:
            await discord_bot.add_cog(FlightLoggerCog(discord_bot))
            logger.info("[DISCORD] Loaded cog: FlightLoggerCog (only)")
        elif find_only:
            # Load only find/search-related cogs here
            # await discord_bot.add_cog(FindCog(discord_bot))
            logger.info("[DISCORD] Loaded find/search cogs only")
        else:
            # Full bot — load all cogs
            await discord_bot.add_cog(FlightLoggerCog(discord_bot))
            # await discord_bot.add_cog(FindCog(discord_bot))
            # await discord_bot.add_cog(SomeOtherCog(discord_bot))
            logger.info("[DISCORD] Loaded all cogs ✓")

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
    # ---- Parse CLI ---------------------------------------------------------
    services = parse_services(sys.argv)
    flags = expand_services(services)

    needs_discord = flags["discord"] or flags["discord_find_only"] or flags["flight_logger_only"]
    needs_twitch = flags["twitch"] or flags["twitch_find_only"]

    logger.info("=" * 70)
    logger.info("CHOBOT STARTING")
    logger.info(f"  Services: {', '.join(sorted(services))}")
    logger.info("=" * 70)

    # ---- Validate config ---------------------------------------------------
    try:
        Config.validate()
        logger.info("[CONFIG] All environment variables validated ✓")
    except ValueError as e:
        logger.critical(f"[CONFIG] Configuration error: {e}")
        sys.exit(1)

    # ---- Init shared data manager ------------------------------------------
    logger.info("[DATA] Initializing data manager...")
    data_manager = DataManager(
        workbook_name=Config.WORKBOOK_NAME,
        json_keyfile=Config.JSON_KEYFILE,
        cache_refresh_hours=Config.CACHE_REFRESH_HOURS,
    )

    logger.info("[DATA] Loading initial cache...")
    if not data_manager.cache:
        logger.info("[DATA] No local cache found. Fetching from Google Sheets...")
        data_manager.update_cache()
    else:
        logger.info(
            f"[DATA] Local cache loaded successfully "
            f"({len(data_manager.cache)} items). Skipping initial fetch."
        )
    logger.info(f"[DATA] Cache status: {len(data_manager.cache)} items ready ✓")

    # ---- Signal handling ---------------------------------------------------
    def _handle_signal(signum, frame):
        logger.warning(f"[MAIN] Signal {signum} received. Shutting down...")
        STOP_EVENT.set()

    try:
        signal.signal(signal.SIGINT, _handle_signal)
        signal.signal(signal.SIGTERM, _handle_signal)
    except Exception:
        pass

    # ---- Start requested services ------------------------------------------
    threads: list[threading.Thread] = []

    if flags["flask"]:
        flask_thread = threading.Thread(
            target=run_flask, args=(data_manager,), name="FlaskThread"
        )
        flask_thread.start()
        threads.append(flask_thread)
        logger.info("[MAIN] Flask API thread started ✓")

    if needs_twitch:
        twitch_find_only = flags["twitch_find_only"]
        twitch_thread = threading.Thread(
            target=run_twitch,
            args=(data_manager, twitch_find_only),
            name="TwitchThread",
        )
        twitch_thread.start()
        threads.append(twitch_thread)
        mode = "find-only" if twitch_find_only else "full"
        logger.info(f"[MAIN] Twitch bot thread started ({mode}) ✓")

    # ---- Discord / Flight-Logger (runs on main asyncio loop) ---------------
    if needs_discord:
        try:
            asyncio.run(
                run_discord(
                    data_manager,
                    flight_logger_only=flags["flight_logger_only"],
                    find_only=flags["discord_find_only"],
                )
            )
        except KeyboardInterrupt:
            logger.info("[MAIN] Shutdown signal received (Ctrl+C)")
            STOP_EVENT.set()
        except Exception as e:
            logger.critical(f"[MAIN] Critical error: {e}")
            logger.critical(traceback.format_exc())
            STOP_EVENT.set()
    else:
        # No Discord — keep main thread alive until stop signal or Ctrl+C
        logger.info("[MAIN] No Discord service selected. Main thread waiting...")
        try:
            while not STOP_EVENT.is_set():
                STOP_EVENT.wait(timeout=1.0)
        except KeyboardInterrupt:
            logger.info("[MAIN] Shutdown signal received (Ctrl+C)")
            STOP_EVENT.set()

    # ---- Shutdown ----------------------------------------------------------
    STOP_EVENT.set()

    for t in threads:
        try:
            t.join(timeout=5)
        except Exception:
            pass

    logger.info("=" * 70)
    logger.info("APPLICATION SHUTDOWN COMPLETE")
    logger.info("=" * 70)


if __name__ == "__main__":
    main()
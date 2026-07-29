import asyncio
import discord

class SharedCogState:
    def __init__(self):
        self.cooldowns = {}
        self.sub_island_lookup = {}
        self.free_island_lookup = {}
        self.order_island_lookup = {}
        self.free_dodo_board_messages: list[discord.Message] = []
        self.free_dodo_board_fingerprints: list[str] = []
        self.free_dodo_board_startup_cleanup_done = False
        self.island_status_sticky_startup_cleanup_done = False
        self._revive_lock = asyncio.Lock()
        self._island_sticky_lock = asyncio.Lock()
        self.island_status_sticky_message: discord.Message | None = None

        # island_clean -> True (down) / False (up); None = not yet initialized
        self.island_down_states: dict[str, bool | None] = {}
        # island_clean -> discord.Message of the sticky "island is down" embed
        self.island_down_messages: dict[str, discord.Message] = {}

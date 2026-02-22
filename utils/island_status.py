"""
Shared Island Online/Offline Status Tracker
Provides a central source of truth for island bot status across Discord and Flask API
"""

import logging
import threading

logger = logging.getLogger("IslandStatus")


class IslandStatusTracker:
    """
    Centralized tracker for island bot online/offline status.
    
    This is shared between Discord bot (which monitors Discord presence)
    and Flask API (which serves status to web clients).
    """
    
    def __init__(self):
        self._status = {}  # island_name -> True (online) / False (offline) / None (unknown)
        self._lock = threading.Lock()
    
    def set_status(self, island_name: str, is_online: bool):
        """
        Update the online status for an island.
        
        Args:
            island_name: Name of the island (e.g., "Alapaap")
            is_online: True if online, False if offline
        """
        with self._lock:
            self._status[island_name] = is_online
            logger.debug(f"Island status updated: {island_name} -> {'ONLINE' if is_online else 'OFFLINE'}")
    
    def get_status(self, island_name: str):
        """
        Get the online status for an island.
        
        Args:
            island_name: Name of the island (e.g., "Alapaap")
        
        Returns:
            True if online, False if offline, None if status unknown
        """
        with self._lock:
            return self._status.get(island_name)
    
    def is_online(self, island_name: str) -> bool:
        """
        Check if an island is online.
        
        Args:
            island_name: Name of the island
        
        Returns:
            True if confirmed online, False otherwise (offline or unknown)
        """
        status = self.get_status(island_name)
        return status is True
    
    def is_tracked(self, island_name: str) -> bool:
        """
        Check if an island is being tracked by Discord bot.
        
        Args:
            island_name: Name of the island
        
        Returns:
            True if the island has been registered in the tracker (regardless of status),
            False if the island has never been added to the tracker
        """
        with self._lock:
            return island_name in self._status
    
    def get_all_status(self) -> dict:
        """
        Get all island statuses.
        
        Returns:
            Dictionary mapping island names to their online status
        """
        with self._lock:
            return self._status.copy()


# Global singleton instance
_island_status_tracker = IslandStatusTracker()


def get_island_status_tracker() -> IslandStatusTracker:
    """Get the global island status tracker instance"""
    return _island_status_tracker

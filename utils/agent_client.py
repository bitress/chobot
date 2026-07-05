import logging
import requests
import time
import threading
from utils.config import Config

logger = logging.getLogger("AgentClient")

class SysbotAgentClient:
    def __init__(self):
        self.url = Config.SYSBOT_AGENT_URL.rstrip('/') if hasattr(Config, 'SYSBOT_AGENT_URL') and Config.SYSBOT_AGENT_URL else None
        self.secret = Config.SYSBOT_AGENT_SECRET if hasattr(Config, 'SYSBOT_AGENT_SECRET') else ""
        self.enabled = bool(self.url)
        
        self._status_cache = {}
        self._status_cache_time = 0
        self._status_cache_lock = threading.Lock()
        self._CACHE_TTL = 3  # Cache short-lived data to avoid spamming the local agent
        
    def _headers(self):
        return {"Authorization": f"Bearer {self.secret}"}
        
    def get_villagers(self):
        """Fetch villager map from the local agent."""
        if not self.enabled:
            return None
        try:
            resp = requests.get(f"{self.url}/api/villagers", headers=self._headers(), timeout=5)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"Failed to fetch villagers from Agent: {e}")
            return None

    def get_all_status(self):
        """Fetch aggregated island status (Dodo, Visitors, etc.) from the local agent."""
        if not self.enabled:
            return None
            
        now = time.monotonic()
        with self._status_cache_lock:
            if self._status_cache and (now - self._status_cache_time < self._CACHE_TTL):
                return self._status_cache
                
        try:
            resp = requests.get(f"{self.url}/api/islands/status", headers=self._headers(), timeout=5)
            resp.raise_for_status()
            data = resp.json()
            
            with self._status_cache_lock:
                self._status_cache = data
                self._status_cache_time = time.monotonic()
                
            return data
        except Exception as e:
            logger.error(f"Failed to fetch island status from Agent: {e}")
            # Return stale cache if available
            with self._status_cache_lock:
                return self._status_cache if self._status_cache else None

agent_client = SysbotAgentClient()

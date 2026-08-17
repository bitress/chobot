
import asyncio
import logging
import time
from typing import Any, Dict, Optional

import aiohttp
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from utils.config import Config

logger = logging.getLogger("NookipediaClient")


class NookipediaClient:
    BASE_URL = "https://api.nookipedia.com/villagers"

    # In-memory cache for villager data (static game data rarely changes)
    _cache: Dict[str, Dict[str, Any]] = {}
    # Negative cache (404s) with timestamp to avoid spamming the API for invalid names
    _negative_cache: Dict[str, float] = {}
    _NEGATIVE_CACHE_TTL = 300  # 5 minutes

    # Timeouts (connect_timeout, read_timeout)
    CONNECT_TIMEOUT = 3.0
    READ_TIMEOUT = 5.0
    TOTAL_TIMEOUT = 8.0

    # Reusable requests session with retry strategy for sync calls
    _session: Optional[requests.Session] = None

    @classmethod
    def _get_session(cls) -> requests.Session:
        if cls._session is None:
            session = requests.Session()
            retries = Retry(
                total=2,
                connect=2,
                read=2,
                backoff_factor=0.5,
                status_forcelist=[500, 502, 503, 504],
                raise_on_status=False,
            )
            adapter = HTTPAdapter(max_retries=retries)
            session.mount("https://", adapter)
            session.mount("http://", adapter)
            cls._session = session
        return cls._session

    @classmethod
    def _get_from_cache(cls, key: str) -> Optional[Any]:
        if key in cls._cache:
            return cls._cache[key]
        if key in cls._negative_cache:
            if time.time() - cls._negative_cache[key] < cls._NEGATIVE_CACHE_TTL:
                return None
            else:
                del cls._negative_cache[key]
        return ...

    @classmethod
    def _store_cache(cls, key: str, data: Optional[Dict[str, Any]]) -> None:
        if data is not None:
            cls._cache[key] = data
        else:
            cls._negative_cache[key] = time.time()

    @staticmethod
    def _normalize_name(name: str) -> str:
        return name.strip().lower()

    @classmethod
    async def get_villager_info(cls, name: str) -> Optional[Dict[str, Any]]:
        """Fetch villager data asynchronously from Nookipedia API with caching and timeout handling."""
        if not name:
            return None

        cache_key = cls._normalize_name(name)
        cached = cls._get_from_cache(cache_key)
        if cached is not ...:
            return cached

        if not Config.NOOKIPEDIA_KEY:
            logger.warning("NOOKIPEDIA_KEY is not set.")
            return None

        headers = {
            "X-API-KEY": Config.NOOKIPEDIA_KEY,
            "Accept-Version": "1.0.0"
        }
        params = {
            "name": name,
            "nhdetails": "true"
        }

        timeout = aiohttp.ClientTimeout(total=cls.TOTAL_TIMEOUT, sock_connect=cls.CONNECT_TIMEOUT)

        for attempt in range(1, 3):
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(cls.BASE_URL, headers=headers, params=params) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            result = data[0] if isinstance(data, list) and len(data) > 0 else data
                            cls._store_cache(cache_key, result)
                            return result
                        elif resp.status == 404:
                            logger.info(f"Villager '{name}' not found on Nookipedia.")
                            cls._store_cache(cache_key, None)
                            return None
                        elif resp.status in (500, 502, 503, 504) and attempt < 2:
                            await asyncio.sleep(0.5)
                            continue
                        else:
                            error_text = await resp.text()
                            logger.error(f"Nookipedia API Error: {resp.status} - {error_text}")
                            return None
            except (asyncio.TimeoutError, aiohttp.ServerTimeoutError, aiohttp.ClientConnectorError) as e:
                if attempt < 2:
                    await asyncio.sleep(0.5)
                    continue
                logger.warning(f"Nookipedia API connection timed out for '{name}': {e}")
                return None
            except Exception as e:
                logger.error(f"Failed to fetch from Nookipedia: {e}")
                return None

        return None

    @classmethod
    def get_villager_info_sync(cls, name: str) -> Optional[Dict[str, Any]]:
        """Fetch villager data synchronously from Nookipedia API with caching and retries."""
        if not name:
            return None

        cache_key = cls._normalize_name(name)
        cached = cls._get_from_cache(cache_key)
        if cached is not ...:
            return cached

        if not Config.NOOKIPEDIA_KEY:
            logger.warning("NOOKIPEDIA_KEY is not set.")
            return None

        headers = {
            "X-API-KEY": Config.NOOKIPEDIA_KEY,
            "Accept-Version": "1.0.0"
        }
        params = {
            "name": name,
            "nhdetails": "true"
        }

        session = cls._get_session()
        try:
            resp = session.get(
                cls.BASE_URL,
                headers=headers,
                params=params,
                timeout=(cls.CONNECT_TIMEOUT, cls.READ_TIMEOUT),
            )
            if resp.status_code == 200:
                data = resp.json()
                result = data[0] if isinstance(data, list) and len(data) > 0 else data
                cls._store_cache(cache_key, result)
                return result
            elif resp.status_code == 404:
                logger.info(f"Villager '{name}' not found on Nookipedia.")
                cls._store_cache(cache_key, None)
                return None
            else:
                logger.error(f"Nookipedia API Error: {resp.status_code} - {resp.text}")
                return None
        except (requests.exceptions.ConnectTimeout, requests.exceptions.ReadTimeout, requests.exceptions.Timeout) as e:
            logger.warning(f"Nookipedia API timed out (sync) for '{name}': {e}")
            return None
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to fetch from Nookipedia (sync): {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error fetching from Nookipedia (sync): {e}")
            return None


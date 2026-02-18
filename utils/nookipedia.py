
import aiohttp
import logging
from utils.config import Config

logger = logging.getLogger("NookipediaClient")

class NookipediaClient:
    BASE_URL = "https://api.nookipedia.com/villagers"

    @staticmethod
    async def get_villager_info(name: str):
        """Fetch villager data from Nookipedia API"""
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

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(NookipediaClient.BASE_URL, headers=headers, params=params) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if isinstance(data, list) and len(data) > 0:
                            # Nookipedia can return multiple villagers if names clash
                            # We'll take the first one or logic to find best match if needed
                            return data[0]
                        return data
                    elif resp.status == 404:
                        logger.info(f"Villager {name} not found on Nookipedia.")
                        return None
                    else:
                        logger.error(f"Nookipedia API Error: {resp.status} - {await resp.text()}")
                        return None
        except Exception as e:
            logger.error(f"Failed to fetch from Nookipedia: {e}")
            return None

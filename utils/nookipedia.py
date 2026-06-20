
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
            return None

    @staticmethod
    async def get_item_info(name: str):
        """Fetch item data from Nookipedia API"""
        if not Config.NOOKIPEDIA_KEY:
            logger.warning("NOOKIPEDIA_KEY is not set.")
            return None

        headers = {
            "X-API-KEY": Config.NOOKIPEDIA_KEY,
            "Accept-Version": "1.0.0"
        }
        
        # We can search both items and furniture. Nookipedia /nh/items handles mostly everything.
        url = "https://api.nookipedia.com/nh/items"
        
        try:
            async with aiohttp.ClientSession() as session:
                # We'll use a search query or fetch exact
                async with session.get(url, headers=headers) as resp: # Nookipedia might need pagination or specific endpoints. 
                    pass # We will instead use the search endpoint if needed, but let's just query /nh/items?name=...
        except Exception as e:
            pass
            
        # Actually, let's look up /nh/items endpoint
        url = f"https://api.nookipedia.com/nh/items/{name}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    elif resp.status == 404:
                        return None
                    else:
                        logger.error(f"Nookipedia API Error: {resp.status}")
                        return None
        except Exception as e:
            logger.error(f"Failed to fetch item from Nookipedia: {e}")
            return None

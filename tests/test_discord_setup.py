import unittest

from utils.discord_setup import summarize_guild_channels


class DiscordSetupTests(unittest.TestCase):
    def test_suggests_setup_ids_from_channel_names(self):
        summary = summarize_guild_channels([
            {"id": "10", "name": "VIP Islands", "type": 4},
            {"id": "11", "name": "free islands", "type": 4},
            {"id": "20", "name": "berichan-orderbot", "type": 0, "parent_id": "10"},
            {"id": "21", "name": "dodo-board", "type": 0, "parent_id": "11"},
            {"id": "22", "name": "flight-arrivals", "type": 0, "parent_id": "10"},
            {"id": "23", "name": "flight-log", "type": 0, "parent_id": "10"},
        ])

        self.assertEqual(summary["suggestions"]["SUB_CATEGORY_ID"], "10")
        self.assertEqual(summary["suggestions"]["FREE_CATEGORY_ID"], "11")
        self.assertEqual(summary["suggestions"]["ORDERBOT_CHANNEL_IDS"], "20,21")
        self.assertEqual(summary["suggestions"]["FLIGHT_LISTEN_CHANNEL_ID"], "22")
        self.assertEqual(summary["suggestions"]["FLIGHT_LOG_CHANNEL_ID"], "23")
        self.assertEqual(summary["suggestions"]["FREE_DODO_BOARD_CHANNEL_ID"], "21")


if __name__ == "__main__":
    unittest.main()

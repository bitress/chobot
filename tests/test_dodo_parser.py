import unittest

from utils.dodo_parser import parse_dodo_message


class DodoParserTests(unittest.TestCase):
    def test_parses_inline_island_and_code(self):
        parsed = parse_dodo_message("The Dodo code for Tala is ABC12")

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.island_name, "TALA")
        self.assertEqual(parsed.dodo_code, "ABC12")

    def test_parses_multiline_island_and_code(self):
        parsed = parse_dodo_message("Island: Hiraya\nDodo Code: 9ZX4Q")

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.island_name, "HIRAYA")
        self.assertEqual(parsed.dodo_code, "9ZX4Q")

    def test_uses_channel_name_when_message_has_no_island(self):
        parsed = parse_dodo_message("Dodo Code: J7K2L", channel_name="free-tala-dodo")

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.island_name, "TALA")
        self.assertEqual(parsed.dodo_code, "J7K2L")

    def test_ignores_messages_without_valid_dodo(self):
        self.assertIsNone(parse_dodo_message("Tala is open but code is 00000", channel_name="tala"))

    def test_parses_embed_fields(self):
        parsed = parse_dodo_message(
            "",
            embeds=[{
                "title": "OrderBot Update",
                "fields": [
                    {"name": "Island", "value": "Tala"},
                    {"name": "Dodo Code", "value": "LMN45"},
                ],
            }],
        )

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.island_name, "TALA")
        self.assertEqual(parsed.dodo_code, "LMN45")
        self.assertEqual(parsed.status, "ONLINE")

    def test_parses_refreshing_status_without_code(self):
        parsed = parse_dodo_message("Island: Tala\nGate is refreshing", channel_name="tala")

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.island_name, "TALA")
        self.assertEqual(parsed.dodo_code, "")
        self.assertEqual(parsed.status, "REFRESHING")

    def test_parses_offline_status_without_code(self):
        parsed = parse_dodo_message("Island: Tala\nIsland is offline")

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.status, "OFFLINE")


if __name__ == "__main__":
    unittest.main()

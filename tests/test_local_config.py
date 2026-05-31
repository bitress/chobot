import tempfile
import unittest
from pathlib import Path
from unittest import mock

from utils import local_config


class LocalConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tmpdir.name) / ".env.local"
        self.patch = mock.patch.object(local_config, "LOCAL_ENV_PATH", self.path)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmpdir.cleanup()

    def test_write_and_mask_local_config(self):
        result = local_config.write_local_setup_config({
            "DISCORD_TOKEN": "abcdefghijklmnopqrstuvwxyz",
            "GUILD_ID": "123",
            "ORDERBOT_CHANNEL_IDS": "111, 222",
            "TWITCH_CHANNEL": "streamer",
        })

        self.assertEqual(result.backup_path, None)
        self.assertTrue(self.path.exists())
        masked = local_config.read_local_setup_config(mask_secrets=True)
        raw = local_config.read_local_setup_config(mask_secrets=False)

        self.assertEqual(masked["DISCORD_TOKEN"], "abcd...wxyz")
        self.assertEqual(raw["ORDERBOT_CHANNEL_IDS"], "111,222")
        self.assertEqual(raw["TWITCH_CHANNEL"], "streamer")

    def test_invalid_discord_id_is_rejected(self):
        with self.assertRaises(ValueError):
            local_config.write_local_setup_config({"GUILD_ID": "not-a-number"})

    def test_backup_created_on_second_write(self):
        local_config.write_local_setup_config({"GUILD_ID": "123"})
        result = local_config.write_local_setup_config({"GUILD_ID": "456"})

        self.assertIsNotNone(result.backup_path)
        self.assertTrue(Path(result.backup_path).exists())
        raw = local_config.read_local_setup_config(mask_secrets=False)
        self.assertEqual(raw["GUILD_ID"], "456")

    def test_masked_secret_does_not_overwrite_existing_secret(self):
        local_config.write_local_setup_config({"DISCORD_TOKEN": "abcdefghijklmnopqrstuvwxyz"})
        local_config.write_local_setup_config({"DISCORD_TOKEN": "abcd...wxyz", "GUILD_ID": "123"})

        raw = local_config.read_local_setup_config(mask_secrets=False)
        self.assertEqual(raw["DISCORD_TOKEN"], "abcdefghijklmnopqrstuvwxyz")
        self.assertEqual(raw["GUILD_ID"], "123")


if __name__ == "__main__":
    unittest.main()

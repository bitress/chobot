import os
import tempfile
import unittest

os.environ["DB_BACKEND"] = "sqlite"
os.environ["SQLITE_DB_PATH"] = os.path.join(tempfile.gettempdir(), "chobot_embed_template_tests.db")

try:
    os.remove(os.environ["SQLITE_DB_PATH"])
except FileNotFoundError:
    pass

from utils import database  # noqa: E402
from utils.config import Config  # noqa: E402
from utils.embed_templates import (  # noqa: E402
    load_free_dodo_embed_template,
    render_template_string,
    save_free_dodo_embed_template,
)

Config.DB_BACKEND = "sqlite"
Config.SQLITE_DB_PATH = os.environ["SQLITE_DB_PATH"]
database.get_engine.cache_clear()
database.get_session_factory.cache_clear()
database._schema_ready = False


class EmbedTemplateTests(unittest.TestCase):
    def test_save_and_load_free_dodo_template(self):
        saved = save_free_dodo_embed_template({
            "title": "{island} is {status}",
            "online_color": "#00ff00",
        })

        self.assertEqual(saved["title"], "{island} is {status}")
        loaded = load_free_dodo_embed_template()
        self.assertEqual(loaded["title"], "{island} is {status}")
        self.assertEqual(loaded["online_color"], "#00ff00")

    def test_render_template_ignores_unknown_placeholders(self):
        rendered = render_template_string("{island} {unknown}", {"island": "Tala"})

        self.assertEqual(rendered, "Tala")


if __name__ == "__main__":
    unittest.main()

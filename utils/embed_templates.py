"""Editable Discord embed templates for self-hosted streamer installs."""

from __future__ import annotations

from string import Formatter

from utils.config import Config
from utils.database import connect_db


FREE_DODO_TEMPLATE = "free_dodo_board"
_PREFIX = "embed.free_dodo_board."

DEFAULT_FREE_DODO_TEMPLATE = {
    "title": "{island}",
    "description": "{description}\n\n[View Island]({island_url})",
    "footer": "ChoBot Dodo Board - {island}",
    "image_url": Config.FOOTER_LINE,
    "online_color": "2ecc71",
    "refreshing_color": "f1c40f",
    "offline_color": "e74c3c",
}


def load_free_dodo_embed_template() -> dict[str, str]:
    template = dict(DEFAULT_FREE_DODO_TEMPLATE)
    with connect_db() as db:
        rows = db.execute(
            "SELECT key, value FROM settings WHERE key LIKE ?",
            (_PREFIX + "%",),
        ).fetchall()
    for row in rows:
        name = str(row["key"])[len(_PREFIX):]
        if name in template:
            template[name] = row["value"]
    return template


def save_free_dodo_embed_template(values: dict) -> dict[str, str]:
    template = dict(DEFAULT_FREE_DODO_TEMPLATE)
    for key in template:
        if key in values:
            template[key] = str(values.get(key) or "").strip()
    with connect_db() as db:
        for key, value in template.items():
            db.execute(
                """
                INSERT INTO settings (key, value)
                VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (_PREFIX + key, value),
            )
    return template


def render_template_string(template: str, values: dict[str, object]) -> str:
    safe_values = {key: "" if value is None else str(value) for key, value in values.items()}
    allowed = set(safe_values)
    for _, field_name, _, _ in Formatter().parse(template or ""):
        if field_name and field_name not in allowed:
            safe_values[field_name] = ""
    return (template or "").format_map(_SafeDict(safe_values)).strip()


def parse_hex_color(value: str, fallback: int) -> int:
    raw = (value or "").strip().lstrip("#")
    if len(raw) == 3:
        raw = "".join(ch * 2 for ch in raw)
    try:
        return int(raw, 16)
    except ValueError:
        return fallback


class _SafeDict(dict):
    def __missing__(self, key):
        return ""

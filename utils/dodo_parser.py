"""Parse Dodo-code announcements from Berichan/OrderBot style messages."""

from __future__ import annotations

from dataclasses import dataclass
import re


DODO_CODE_RE = re.compile(r"\b[A-HJ-NP-Z0-9]{5}\b", re.IGNORECASE)
INVALID_DODO_CODES = {"00000", "-----", "GETTI", "WAIT.", "....."}

_ISLAND_PATTERNS = [
    re.compile(r"\bdodo\s+code\s+for\s+(?P<island>[A-Za-z0-9 _'-]{2,40})\b", re.IGNORECASE),
    re.compile(r"\bfor\s+island\s+(?P<island>[A-Za-z0-9 _'-]{2,40})\b", re.IGNORECASE),
    re.compile(r"\bisland\s*[:=-]\s*(?P<island>[A-Za-z0-9 _'-]{2,40})\b", re.IGNORECASE),
    re.compile(r"^\s*(?P<island>[A-Za-z0-9 _'-]{2,40})\s*(?:[:|/-])\s*(?:dodo\s*)?(?:code\s*)?$", re.IGNORECASE),
]


@dataclass(frozen=True)
class DodoParseResult:
    island_name: str
    dodo_code: str = ""
    status: str = "ONLINE"
    source: str = "orderbot"
    raw_text: str = ""


def parse_dodo_message(
    content: str,
    channel_name: str | None = None,
    embeds: list | tuple | None = None,
) -> DodoParseResult | None:
    """Extract an island name and Dodo code from an OrderBot/Berichan message.

    ``channel_name`` is used as the island fallback because many treasure island
    setups post the code inside the island's own Discord channel.
    """
    text = _clean_discord_markup(_message_text(content, embeds))
    code = _extract_dodo_code(text)
    status = _extract_status(text, has_code=bool(code))

    island = _extract_island_name(text) or _island_from_channel(channel_name)
    if not island:
        return None
    if not code and status == "UNKNOWN":
        return None

    return DodoParseResult(island_name=island, dodo_code=code or "", status=status, raw_text=text[:1000])


def _message_text(content: str, embeds: list | tuple | None) -> str:
    parts = [content or ""]
    for embed in embeds or []:
        if isinstance(embed, dict):
            parts.extend(_dict_embed_parts(embed))
        else:
            parts.extend(_object_embed_parts(embed))
    return "\n".join(part for part in parts if part)


def _dict_embed_parts(embed: dict) -> list[str]:
    parts = [
        str(embed.get("title") or ""),
        str(embed.get("description") or ""),
        str(embed.get("footer", {}).get("text") if isinstance(embed.get("footer"), dict) else ""),
    ]
    for field in embed.get("fields") or []:
        if isinstance(field, dict):
            name = str(field.get("name") or "")
            value = str(field.get("value") or "")
            parts.append(f"{name}: {value}" if name and value else name or value)
    return parts


def _object_embed_parts(embed) -> list[str]:
    parts = [
        str(getattr(embed, "title", "") or ""),
        str(getattr(embed, "description", "") or ""),
    ]
    footer = getattr(embed, "footer", None)
    if footer:
        parts.append(str(getattr(footer, "text", "") or ""))
    for field in getattr(embed, "fields", []) or []:
        name = str(getattr(field, "name", "") or "")
        value = str(getattr(field, "value", "") or "")
        parts.append(f"{name}: {value}" if name and value else name or value)
    return parts


def _extract_dodo_code(text: str) -> str | None:
    for match in DODO_CODE_RE.finditer(text):
        code = match.group(0).upper()
        if code not in INVALID_DODO_CODES:
            return code
    return None


def _extract_status(text: str, has_code: bool) -> str:
    normalized = text.lower()
    if has_code:
        return "ONLINE"
    if re.search(r"\b(refreshing|refresh|getting|generating|new\s+dodo|gate\s+reset)\b", normalized):
        return "REFRESHING"
    if re.search(r"\b(offline|closed|down|unavailable|not\s+available|no\s+dodo)\b", normalized):
        return "OFFLINE"
    if re.search(r"\b(order\s+starting|order\s+is\s+starting|starting)\b", normalized):
        return "ORDER_STARTING"
    return "UNKNOWN"


def _extract_island_name(text: str) -> str:
    compact_lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in compact_lines:
        for pattern in _ISLAND_PATTERNS:
            match = pattern.search(line)
            if match:
                island = _normalize_island_name(match.group("island"))
                if island:
                    return island
    return ""


def _island_from_channel(channel_name: str | None) -> str:
    if not channel_name:
        return ""
    name = re.sub(r"^[#\s]+", "", channel_name.strip())
    name = re.sub(r"^(?:free|vip|sub|member)[-_ ]+", "", name, flags=re.IGNORECASE)
    name = re.sub(r"[-_ ]+(?:chat|orders?|dodo|code|codes?)$", "", name, flags=re.IGNORECASE)
    return _normalize_island_name(name)


def _normalize_island_name(value: str) -> str:
    value = re.sub(r"[`*_~>\[\](){}]", " ", value)
    value = re.sub(r"\b(?:is|has|updated|new|the|code|dodo)\b.*$", "", value, flags=re.IGNORECASE)
    value = re.sub(r"[^A-Za-z0-9 _'-]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip(" -_:")
    return value.upper()


def _clean_discord_markup(content: str) -> str:
    text = content or ""
    text = re.sub(r"<#(\d+)>", " ", text)
    text = re.sub(r"<@&?(\d+)>", " ", text)
    return text.replace("\r\n", "\n")

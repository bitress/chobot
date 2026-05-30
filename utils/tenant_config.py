"""Tenant-aware runtime configuration helpers.

This module is the bridge between the SaaS control-plane tables and the
legacy bot runtime.  The first production slice still runs one bot process, but
it can now be pointed at a tenant record instead of reading every community
setting directly from environment variables.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from utils.config import Config
from utils.database import connect_db, get_default_tenant_id


def _str_or_empty(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _int_or_none(value: Any) -> int | None:
    value = _str_or_empty(value)
    if not value:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _enabled(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off", ""}
    return bool(value)


@dataclass(frozen=True)
class TenantRuntimeConfig:
    tenant_id: str
    name: str
    slug: str
    status: str = "active"
    plan: str = "legacy"
    settings: dict[str, str] = field(default_factory=dict)

    guild_id: int | None = None
    member_category_id: int | None = None
    free_category_id: int | None = None
    log_channel_id: int | None = None
    flight_listen_channel_id: int | None = None
    free_flight_listen_channel_id: int | None = None
    flight_log_channel_id: int | None = None
    mod_role_id: int | None = None
    island_access_role_id: int | None = None
    discord_bot_enabled: bool = True

    twitch_channel: str = ""
    twitch_bot_enabled: bool = False

    member_islands: list[str] = field(default_factory=list)
    free_islands: list[str] = field(default_factory=list)

    @property
    def all_islands(self) -> list[str]:
        return [*self.member_islands, *self.free_islands]

    @property
    def logo_url(self) -> str:
        return self.settings.get("brand.logo_url", "")

    @property
    def theme_color(self) -> str:
        return self.settings.get("brand.theme_color", "teal")

    @property
    def onboarding_complete(self) -> bool:
        return bool(self.settings.get("onboarding.completed_at")) or self.tenant_id == get_default_tenant_id()


def load_tenant_runtime_config(tenant_id: str | None = None) -> TenantRuntimeConfig:
    """Load a tenant's bot-facing runtime config.

    The default tenant keeps backward compatibility with the original
    environment-driven deployment: blank DB fields fall back to ``Config``.
    Customer tenants use only their saved DB configuration.
    """
    tenant_id = (tenant_id or get_default_tenant_id()).strip() or get_default_tenant_id()
    is_default = tenant_id == get_default_tenant_id()

    with connect_db() as db:
        tenant = db.execute("SELECT * FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
        if tenant is None:
            if not is_default:
                raise KeyError(f'Tenant "{tenant_id}" not found')
            tenant_map = {
                "id": get_default_tenant_id(),
                "name": Config.DEFAULT_TENANT_NAME,
                "slug": Config.DEFAULT_TENANT_SLUG,
                "status": "active",
                "plan": "legacy",
            }
        else:
            tenant_map = dict(tenant.items())

        discord = db.execute(
            "SELECT * FROM tenant_discord_configs WHERE tenant_id = ?",
            (tenant_id,),
        ).fetchone()
        twitch = db.execute(
            "SELECT * FROM tenant_twitch_configs WHERE tenant_id = ?",
            (tenant_id,),
        ).fetchone()
        settings = {
            row["key"]: row["value"]
            for row in db.execute(
                "SELECT key, value FROM tenant_settings WHERE tenant_id = ?",
                (tenant_id,),
            ).fetchall()
        }
        islands = db.execute(
            "SELECT name, type, cat FROM islands WHERE tenant_id = ? ORDER BY name",
            (tenant_id,),
        ).fetchall()

    discord_map = dict(discord.items()) if discord else {}
    twitch_map = dict(twitch.items()) if twitch else {}
    member_islands, free_islands = _split_islands(islands)

    if is_default:
        member_islands = member_islands or list(Config.SUB_ISLANDS)
        free_islands = free_islands or list(Config.FREE_ISLANDS)

    return TenantRuntimeConfig(
        tenant_id=tenant_map["id"],
        name=tenant_map.get("name") or Config.DEFAULT_TENANT_NAME,
        slug=tenant_map.get("slug") or tenant_map["id"],
        status=tenant_map.get("status") or "active",
        plan=tenant_map.get("plan") or "legacy",
        settings=settings,
        guild_id=_tenant_int(discord_map, "guild_id", Config.GUILD_ID if is_default else None),
        member_category_id=_tenant_int(discord_map, "member_category_id", Config.CATEGORY_ID if is_default else None),
        free_category_id=_tenant_int(discord_map, "free_category_id", Config.FREE_CATEGORY_ID if is_default else None),
        log_channel_id=_tenant_int(discord_map, "log_channel_id", Config.LOG_CHANNEL_ID if is_default else None),
        flight_listen_channel_id=_tenant_int(
            discord_map,
            "flight_listen_channel_id",
            Config.FLIGHT_LISTEN_CHANNEL_ID if is_default else None,
        ),
        free_flight_listen_channel_id=_tenant_int(
            discord_map,
            "free_flight_listen_channel_id",
            Config.FREE_ISLAND_FLIGHT_LISTEN_CHANNEL_ID if is_default else None,
        ),
        flight_log_channel_id=_tenant_int(
            discord_map,
            "flight_log_channel_id",
            Config.FLIGHT_LOG_CHANNEL_ID if is_default else None,
        ),
        mod_role_id=_tenant_int(discord_map, "mod_role_id", Config.ADMIN_ROLE_ID if is_default else None),
        island_access_role_id=_tenant_int(
            discord_map,
            "island_access_role_id",
            Config.ISLAND_ACCESS_ROLE if is_default else None,
        ),
        discord_bot_enabled=_enabled(discord_map.get("bot_enabled"), default=True),
        twitch_channel=_tenant_str(twitch_map, "channel_name", Config.TWITCH_CHANNEL if is_default else ""),
        twitch_bot_enabled=_enabled(twitch_map.get("bot_enabled"), default=False) and bool(
            _tenant_str(twitch_map, "channel_name", Config.TWITCH_CHANNEL if is_default else "")
        ),
        member_islands=member_islands,
        free_islands=free_islands,
    )


def load_enabled_tenant_runtime_configs() -> list[TenantRuntimeConfig]:
    """Return active tenants with at least one bot integration enabled."""
    with connect_db() as db:
        rows = db.execute("SELECT id FROM tenants WHERE status = 'active' ORDER BY name").fetchall()

    configs: list[TenantRuntimeConfig] = []
    for row in rows:
        config = load_tenant_runtime_config(row["id"])
        if config.discord_bot_enabled or config.twitch_bot_enabled:
            configs.append(config)
    return configs


def _tenant_int(row: dict[str, Any], key: str, fallback: int | None) -> int | None:
    return _int_or_none(row.get(key)) if _str_or_empty(row.get(key)) else fallback


def _tenant_str(row: dict[str, Any], key: str, fallback: str | None) -> str:
    return _str_or_empty(row.get(key)) or _str_or_empty(fallback)


def _split_islands(rows) -> tuple[list[str], list[str]]:
    member: list[str] = []
    free: list[str] = []
    for row in rows:
        name = _str_or_empty(row["name"])
        if not name:
            continue
        cat = _str_or_empty(row["cat"]).lower()
        island_type = _str_or_empty(row["type"]).lower()
        if cat in {"member", "sub", "vip"} or island_type in {"vip", "member", "sub"}:
            member.append(name)
        else:
            free.append(name)
    return member, free

"""SQLAlchemy models for ChoBot's application database."""

from __future__ import annotations

from sqlalchemy import BigInteger, Float, Integer, String, Text, Index
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    status: Mapped[str] = mapped_column(String(64), nullable=False, default="active", server_default="active")
    plan: Mapped[str] = mapped_column(String(64), nullable=False, default="legacy", server_default="legacy")
    created_at: Mapped[int | None] = mapped_column(BigInteger)
    updated_at: Mapped[int | None] = mapped_column(BigInteger)


class TenantSetting(Base):
    __tablename__ = "tenant_settings"

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)


class TenantDiscordConfig(Base):
    __tablename__ = "tenant_discord_configs"

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    guild_id: Mapped[str | None] = mapped_column(String(64), index=True)
    member_category_id: Mapped[str | None] = mapped_column(String(64))
    free_category_id: Mapped[str | None] = mapped_column(String(64))
    log_channel_id: Mapped[str | None] = mapped_column(String(64))
    flight_listen_channel_id: Mapped[str | None] = mapped_column(String(64))
    free_flight_listen_channel_id: Mapped[str | None] = mapped_column(String(64))
    flight_log_channel_id: Mapped[str | None] = mapped_column(String(64))
    mod_role_id: Mapped[str | None] = mapped_column(String(64))
    island_access_role_id: Mapped[str | None] = mapped_column(String(64))
    bot_enabled: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    updated_at: Mapped[int | None] = mapped_column(BigInteger)


class TenantTwitchConfig(Base):
    __tablename__ = "tenant_twitch_configs"

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    channel_name: Mapped[str | None] = mapped_column(String(255), index=True)
    bot_enabled: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    updated_at: Mapped[int | None] = mapped_column(BigInteger)


class TenantUser(Base):
    __tablename__ = "tenant_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    discord_user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(64), nullable=False, default="member", server_default="member")
    created_at: Mapped[int | None] = mapped_column(BigInteger)
    updated_at: Mapped[int | None] = mapped_column(BigInteger)

    __table_args__ = (
        Index("ix_tenant_users_tenant_discord", "tenant_id", "discord_user_id"),
    )


class TenantAuditLog(Base):
    __tablename__ = "tenant_audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    actor_user_id: Mapped[str | None] = mapped_column(String(64), index=True)
    action: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    target_type: Mapped[str | None] = mapped_column(String(128))
    target_id: Mapped[str | None] = mapped_column(String(255))
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}", server_default="{}")
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)


class CommandClaim(Base):
    __tablename__ = "command_claims"

    message_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, default="chopaeng", server_default="chopaeng", index=True)
    claimed_at: Mapped[float] = mapped_column(Float, nullable=False)


class IslandSubscription(Base):
    __tablename__ = "island_subscriptions"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    island_clean: Mapped[str] = mapped_column(String(255), primary_key=True)
    kind: Mapped[str] = mapped_column(String(64), primary_key=True, default="sub")
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, default="chopaeng", server_default="chopaeng", index=True)
    has_island_access: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column("key", String(255), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, default="chopaeng", server_default="chopaeng", index=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)


class Island(Base):
    __tablename__ = "islands"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, default="chopaeng", server_default="chopaeng", index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    type: Mapped[str] = mapped_column(String(255), nullable=False, default="", server_default="")
    items: Mapped[str] = mapped_column(Text, nullable=False, default="[]", server_default="[]")
    theme: Mapped[str] = mapped_column(String(64), nullable=False, default="teal", server_default="teal")
    cat: Mapped[str] = mapped_column(String(64), nullable=False, default="public", server_default="public")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    seasonal: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    status: Mapped[str] = mapped_column(String(64), nullable=False, default="OFFLINE", server_default="OFFLINE")
    visitors: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    dodo_code: Mapped[str | None] = mapped_column(String(32))
    map_url: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[str | None] = mapped_column(String(64))
    required_roles: Mapped[str] = mapped_column(Text, nullable=False, default="[]", server_default="[]")
    channel_id: Mapped[str | None] = mapped_column(String(64))


class IslandBotStatus(Base):
    __tablename__ = "island_bot_status"

    island_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, default="chopaeng", server_default="chopaeng", index=True)
    island_name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_online: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    updated_at: Mapped[str | None] = mapped_column(String(64))


class IslandMetadata(Base):
    __tablename__ = "island_metadata"

    name: Mapped[str] = mapped_column(String(255), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, default="chopaeng", server_default="chopaeng", index=True)
    category: Mapped[str] = mapped_column(String(64), nullable=False, default="public", server_default="public")
    theme: Mapped[str] = mapped_column(String(64), nullable=False, default="teal", server_default="teal")
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    updated_at: Mapped[str | None] = mapped_column(String(64))


class IslandVisit(Base):
    __tablename__ = "island_visits"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, default="chopaeng", server_default="chopaeng", index=True)
    ign: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    origin_island: Mapped[str] = mapped_column(String(255), nullable=False)
    destination: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    guild_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    authorized: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    timestamp: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    island_type: Mapped[str] = mapped_column(String(64), nullable=False, default="sub", server_default="sub", index=True)
    has_island_access: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    __table_args__ = (
        Index("ix_island_visits_user_guild_ts", "user_id", "guild_id", "timestamp"),
        Index("ix_island_visits_ign_ts", "ign", "timestamp"),
    )


class Warning(Base):
    __tablename__ = "warnings"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, default="chopaeng", server_default="chopaeng", index=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    guild_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    reason: Mapped[str | None] = mapped_column(Text)
    mod_id: Mapped[int | None] = mapped_column(BigInteger)
    timestamp: Mapped[int | None] = mapped_column(BigInteger, index=True)
    visit_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    action_type: Mapped[str] = mapped_column(String(64), nullable=False, default="WARN", server_default="WARN", index=True)

    __table_args__ = (
        Index("ix_warnings_user_guild_ts", "user_id", "guild_id", "timestamp"),
    )


class DodoRevealMessage(Base):
    __tablename__ = "dodo_reveal_messages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, default="chopaeng", server_default="chopaeng", index=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    island_clean: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    channel_id: Mapped[str | None] = mapped_column(String(64))
    message_url: Mapped[str] = mapped_column(Text, nullable=False)
    username: Mapped[str | None] = mapped_column(String(255))
    nickname: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)

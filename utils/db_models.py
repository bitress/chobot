"""SQLAlchemy models for ChoBot's application database."""

from __future__ import annotations

from sqlalchemy import BigInteger, Float, Integer, String, Text, Index
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class CommandClaim(Base):
    __tablename__ = "command_claims"

    message_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    claimed_at: Mapped[float] = mapped_column(Float, nullable=False)


class IslandSubscription(Base):
    __tablename__ = "island_subscriptions"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    island_clean: Mapped[str] = mapped_column(String(255), primary_key=True)
    kind: Mapped[str] = mapped_column(String(64), primary_key=True, default="sub")
    has_island_access: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column("key", String(255), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)


class Island(Base):
    __tablename__ = "islands"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    type: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    items: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    theme: Mapped[str] = mapped_column(String(64), nullable=False, default="teal")
    cat: Mapped[str] = mapped_column(String(64), nullable=False, default="public")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    seasonal: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(64), nullable=False, default="OFFLINE")
    visitors: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    dodo_code: Mapped[str | None] = mapped_column(String(32))
    map_url: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[str | None] = mapped_column(String(64))
    required_roles: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    channel_id: Mapped[str | None] = mapped_column(String(64))
    display_name: Mapped[str | None] = mapped_column(String(255))
    is_visible: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class IslandBotStatus(Base):
    __tablename__ = "island_bot_status"

    island_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    island_name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_online: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[str | None] = mapped_column(String(64))


class IslandMetadata(Base):
    __tablename__ = "island_metadata"

    name: Mapped[str] = mapped_column(String(255), primary_key=True)
    category: Mapped[str] = mapped_column(String(64), nullable=False, default="public")
    theme: Mapped[str] = mapped_column(String(64), nullable=False, default="teal")
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    updated_at: Mapped[str | None] = mapped_column(String(64))


class IslandVisit(Base):
    __tablename__ = "island_visits"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ign: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    origin_island: Mapped[str] = mapped_column(String(255), nullable=False)
    destination: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    guild_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    authorized: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    timestamp: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    island_type: Mapped[str] = mapped_column(String(64), nullable=False, default="sub", index=True)
    has_island_access: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        Index("ix_island_visits_user_guild_ts", "user_id", "guild_id", "timestamp"),
        Index("ix_island_visits_ign_ts", "ign", "timestamp"),
    )


class Warning(Base):
    __tablename__ = "warnings"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    guild_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    reason: Mapped[str | None] = mapped_column(Text)
    mod_id: Mapped[int | None] = mapped_column(BigInteger)
    timestamp: Mapped[int | None] = mapped_column(BigInteger, index=True)
    visit_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    action_type: Mapped[str] = mapped_column(String(64), nullable=False, default="WARN", index=True)

    __table_args__ = (
        Index("ix_warnings_user_guild_ts", "user_id", "guild_id", "timestamp"),
    )


class DodoRevealMessage(Base):
    __tablename__ = "dodo_reveal_messages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    island_clean: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    channel_id: Mapped[str | None] = mapped_column(String(64))
    message_url: Mapped[str] = mapped_column(Text, nullable=False)
    username: Mapped[str | None] = mapped_column(String(255))
    nickname: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)


class DashboardAuditEvent(Base):
    __tablename__ = "dashboard_audit_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    actor_user_id: Mapped[str | None] = mapped_column(String(64), index=True)
    actor_name: Mapped[str | None] = mapped_column(String(255))
    action: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    target: Mapped[str | None] = mapped_column(String(255), index=True)
    details: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    ip_address: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)

    __table_args__ = (
        Index("ix_dashboard_audit_action_ts", "action", "created_at"),
    )

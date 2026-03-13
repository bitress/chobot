"""
SQLAlchemy ORM models for ChoBot.

These models are used by Flask-Migrate (Alembic) to manage database schema
migrations.  All tables are shared between the Flask dashboard (sync) and
the Discord bot (async).
"""

from sqlalchemy import (
    BigInteger, Column, ForeignKey, Integer, String, Text
)
from utils.db import Base


class IslandVisit(Base):
    """Records every island visitor arrival detected by the flight-logger bot."""

    __tablename__ = "island_visits"

    id             = Column(Integer, primary_key=True, autoincrement=True)
    ign            = Column(String(255), nullable=False)
    origin_island  = Column(String(255), nullable=False)
    destination    = Column(String(255), nullable=False)
    user_id        = Column(BigInteger, nullable=True)
    guild_id       = Column(BigInteger, nullable=True)
    authorized     = Column(Integer, nullable=False, default=0)
    timestamp      = Column(Integer, nullable=False)
    island_type    = Column(String(50), nullable=False, default="sub")


class Warning(Base):
    """Moderation warnings / kicks / bans issued by the flight-logger bot."""

    __tablename__ = "warnings"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    user_id     = Column(BigInteger, nullable=True)
    guild_id    = Column(BigInteger, nullable=True)
    reason      = Column(Text, nullable=True)
    mod_id      = Column(BigInteger, nullable=True)
    timestamp   = Column(Integer, nullable=True)
    visit_id    = Column(Integer, ForeignKey("island_visits.id"), nullable=True)
    action_type = Column(String(50), nullable=False, default="WARN")


class Island(Base):
    """Island metadata managed through the web dashboard."""

    __tablename__ = "islands"

    id          = Column(String(255), primary_key=True)
    name        = Column(String(255), nullable=False)
    type        = Column(String(100), nullable=False, default="")
    items       = Column(Text, nullable=False, default="[]")
    theme       = Column(String(50), nullable=False, default="teal")
    cat         = Column(String(50), nullable=False, default="public")
    description = Column(Text, nullable=False, default="")
    seasonal    = Column(Text, nullable=False, default="")
    status      = Column(String(50), nullable=False, default="OFFLINE")
    visitors    = Column(Integer, nullable=False, default=0)
    dodo_code   = Column(String(10), nullable=True)
    map_url     = Column(Text, nullable=True)
    updated_at  = Column(Text, nullable=True)


class IslandBotStatus(Base):
    """Live Discord-bot online presence, written by the island-monitor loop."""

    __tablename__ = "island_bot_status"

    island_id   = Column(String(255), primary_key=True)
    island_name = Column(String(255), nullable=False)
    is_online   = Column(Integer, nullable=False, default=0)
    updated_at  = Column(Text, nullable=True)


class IslandMetadata(Base):
    """Legacy island metadata table kept for backward compatibility."""

    __tablename__ = "island_metadata"

    name       = Column(String(255), primary_key=True)
    category   = Column(String(50), nullable=False, default="public")
    theme      = Column(String(50), nullable=False, default="teal")
    notes      = Column(Text, nullable=False, default="")
    updated_at = Column(Text, nullable=True)

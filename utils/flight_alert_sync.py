"""
Web -> Discord sync for #flight-logs alerts.

When a moderator resolves a flight on the web dashboard, the flight logger's Discord
alert is edited the way the in-Discord buttons would ("CASE CLOSED" + "Action Taken",
buttons removed), so nobody handles the same traveler twice.
"""

from __future__ import annotations

from typing import Any

from utils.flight_discord import close_flight_alert, run_async


def update_flight_alert(channel_id: Any, message_id: Any, action: str, mod_name: str, **details: Any) -> bool:
    """Mark a #flight-logs alert as resolved on the web. Never raises."""
    return close_flight_alert(channel_id, message_id, action, mod_name, **details)


def update_flight_alert_async(*args: Any, **kwargs: Any) -> None:
    """Fire-and-forget wrapper so HTTP handlers are never blocked by Discord."""
    run_async(update_flight_alert, *args, **kwargs)

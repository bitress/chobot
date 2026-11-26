"""
Seasonal pocket bundles: an optional show window (starts_at / ends_at) that can repeat every year.
Outside the window a bundle is hidden from public lists (website packs, Pocket Guide, Discord drop
bot); admins still see and manage it.

The window is in *local wall-clock time*, the same for everyone: "Nov 1 00:00 - Nov 30 23:59"
opens at midnight wherever the viewer is. starts_at / ends_at hold that wall-clock time encoded
as if it were UTC (Nov 1 00:00 -> the unix time of Nov 1 00:00 UTC). To check a window, the
viewer's "now" is moved by their UTC offset (the website sends it; anything without a viewer,
like the Discord bot, uses BUNDLE_DEFAULT_UTC_OFFSET_MINUTES).
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Any, Mapping

# Offset for checks without a viewer timezone (Discord drop bot, old clients). Philippine time.
DEFAULT_UTC_OFFSET_MINUTES = int(os.getenv("BUNDLE_DEFAULT_UTC_OFFSET_MINUTES", "480") or 480)


def _shift_years(ts: int, years: int) -> int:
    d = datetime.fromtimestamp(ts, tz=timezone.utc)
    try:
        return int(d.replace(year=d.year + years).timestamp())
    except ValueError:  # Feb 29 in a non-leap year
        return int(d.replace(year=d.year + years, day=28).timestamp())


def _int_or_none(v: Any) -> int | None:
    try:
        return int(v) if v not in (None, "", 0, "0") else None
    except (TypeError, ValueError):
        return None


def parse_utc_offset(value: Any) -> int:
    """Minutes east of UTC from a request value (e.g. "480", "-300"); the default when missing or odd."""
    try:
        minutes = int(str(value).strip())
    except (TypeError, ValueError):
        return DEFAULT_UTC_OFFSET_MINUTES
    return minutes if -14 * 60 <= minutes <= 14 * 60 else DEFAULT_UTC_OFFSET_MINUTES


def bundle_window(starts_at: Any, ends_at: Any, repeat_yearly: Any = False, now: int | None = None,
                  utc_offset_minutes: int | None = None) -> dict:
    """{status, start, end} for a bundle, as seen by a viewer at utc_offset_minutes.

    status: "always" (no window), "live", "upcoming" or "ended". start / end are wall-clock
    times encoded as UTC (format them as UTC); for yearly windows they are the current
    occurrence, or the next one once this year's has passed.
    """
    offset = DEFAULT_UTC_OFFSET_MINUTES if utc_offset_minutes is None else utc_offset_minutes
    # The viewer's local wall-clock time, in the same encoding as the stored window
    now = int(now if now is not None else time.time()) + offset * 60
    start, end = _int_or_none(starts_at), _int_or_none(ends_at)
    if start is None and end is None:
        return {"status": "always", "start": None, "end": None}
    if repeat_yearly and start is not None and end is not None and end > start:
        # Move the window to the occurrence that is current, or else the next one
        years = datetime.fromtimestamp(now, tz=timezone.utc).year - datetime.fromtimestamp(start, tz=timezone.utc).year
        s, e = _shift_years(start, years - 1), _shift_years(end, years - 1)
        for _ in range(3):
            if e >= now:
                break
            s, e = _shift_years(s, 1), _shift_years(e, 1)
        start, end = s, e
    if start is not None and now < start:
        return {"status": "upcoming", "start": start, "end": end}
    if end is not None and now > end:
        return {"status": "ended", "start": start, "end": end}
    return {"status": "live", "start": start, "end": end}


def is_bundle_live(row: Mapping[str, Any], now: int | None = None, utc_offset_minutes: int | None = None) -> bool:
    """True when a pocket_bundles row should be listed right now for a viewer at that UTC offset."""
    w = bundle_window(row.get("starts_at"), row.get("ends_at"), row.get("repeat_yearly"), now, utc_offset_minutes)
    return w["status"] in ("always", "live")

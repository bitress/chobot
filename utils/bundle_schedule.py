"""
Seasonal pocket bundles: an optional show window (starts_at / ends_at, unix seconds) that can
repeat every year. Outside the window a bundle is hidden from public lists (website packs,
Pocket Guide, Discord drop bot); admins still see and manage it.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Mapping


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


def bundle_window(starts_at: Any, ends_at: Any, repeat_yearly: Any = False, now: int | None = None) -> dict:
    """{status, start, end} for a bundle.

    status: "always" (no window), "live", "upcoming" or "ended". For yearly windows, start/end
    are this year's occurrence when live or ended-this-year-but-repeating moves to next year's.
    """
    now = int(now if now is not None else time.time())
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


def is_bundle_live(row: Mapping[str, Any], now: int | None = None) -> bool:
    """True when a pocket_bundles row should be listed publicly right now."""
    status = bundle_window(row.get("starts_at"), row.get("ends_at"), row.get("repeat_yearly"), now)["status"]
    return status in ("always", "live")

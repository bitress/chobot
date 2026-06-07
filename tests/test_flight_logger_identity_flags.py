import importlib.util
from pathlib import Path


_MODULE_PATH = Path(__file__).resolve().parents[1] / "bots" / "flight_logger.py"
_SPEC = importlib.util.spec_from_file_location("flight_logger_under_test", _MODULE_PATH)
flight_logger = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(flight_logger)

IDENTITY_EVENT_MEMBER_JOIN = flight_logger.IDENTITY_EVENT_MEMBER_JOIN
IDENTITY_EVENT_NICKNAME_CHANGE = flight_logger.IDENTITY_EVENT_NICKNAME_CHANGE
RECENT_IDENTITY_WINDOW_SECONDS = flight_logger.RECENT_IDENTITY_WINDOW_SECONDS
filter_recent_identity_events = flight_logger.filter_recent_identity_events
summarize_recent_identity_events = flight_logger.summarize_recent_identity_events


def test_recent_identity_filter_keeps_events_inside_24_hours():
    now = 1_700_000_000
    events = [
        {
            "event_type": IDENTITY_EVENT_NICKNAME_CHANGE,
            "old_display_name": "Old Name",
            "new_display_name": "New Name",
            "created_at": now - RECENT_IDENTITY_WINDOW_SECONDS + 1,
        }
    ]

    assert filter_recent_identity_events(events, now) == events


def test_recent_identity_filter_ignores_events_older_than_24_hours():
    now = 1_700_000_000
    events = [
        {
            "event_type": IDENTITY_EVENT_NICKNAME_CHANGE,
            "old_display_name": "Old Name",
            "new_display_name": "New Name",
            "created_at": now - RECENT_IDENTITY_WINDOW_SECONDS - 1,
        }
    ]

    assert filter_recent_identity_events(events, now) == []


def test_recent_identity_summary_combines_multiple_event_reasons():
    now = 1_700_000_000
    events = [
        {
            "event_type": IDENTITY_EVENT_MEMBER_JOIN,
            "old_display_name": None,
            "new_display_name": "New Member",
            "created_at": now - 10,
        },
        {
            "event_type": IDENTITY_EVENT_NICKNAME_CHANGE,
            "old_display_name": "Old Nick",
            "new_display_name": "New Nick",
            "created_at": now - 5,
        },
    ]

    summary, reasons = summarize_recent_identity_events(events)

    assert "Recently joined server" in reasons
    assert "Recent nickname change" in reasons
    assert "Display name: `New Member`" in summary
    assert "`Old Nick` -> `New Nick`" in summary

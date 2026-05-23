"""Pure date/time helpers. Both functions take `now_utc` as a parameter so tests can pin time."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

ALLOWED_DELIVERY_HOURS: frozenset[int] = frozenset({7, 8})


def previous_day_window(now_utc: datetime, tz_name: str) -> tuple[str, str]:
    """Return the previous calendar day in `tz_name` as a UTC ISO8601 [start, end) window.

    Both timestamps end with `Z`. The window is exactly 24 hours long in local time,
    which may span 23 or 25 hours of UTC at DST boundaries — `zoneinfo` handles that.
    """
    tz = ZoneInfo(tz_name)
    local_now = now_utc.astimezone(tz)
    prev_local_date = (local_now - timedelta(days=1)).date()
    start_local = datetime.combine(prev_local_date, datetime.min.time(), tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    return _to_iso_z(start_local.astimezone(UTC)), _to_iso_z(end_local.astimezone(UTC))


def should_run(now_utc: datetime, tz_name: str) -> bool:
    """True iff the local hour of `now_utc` in `tz_name` is in the delivery window.

    With both GH Actions cron fires (11:00 and 12:00 UTC) able to drift up to ~60 min,
    the {7, 8}-hour gate ensures at least one fire per day lands inside the window
    in both EST and EDT. The Slack-history idempotency check (in digest.py) prevents
    double-posting when both fires land in the window on the same local day.
    """
    return now_utc.astimezone(ZoneInfo(tz_name)).hour in ALLOWED_DELIVERY_HOURS


def _to_iso_z(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

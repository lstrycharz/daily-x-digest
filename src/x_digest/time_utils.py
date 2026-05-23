"""Pure date/time helpers. Both functions take `now_utc` as a parameter so tests can pin time."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo


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
    raise NotImplementedError


def _to_iso_z(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

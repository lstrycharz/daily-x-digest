"""Pure date/time helpers. Both functions take `now_utc` as a parameter so tests can pin time."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

ALLOWED_DELIVERY_HOURS: frozenset[int] = frozenset({7, 8, 9, 10, 11})


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

    The window is intentionally wide (7am-11am ET) to tolerate GitHub Actions
    cron delays of up to ~4 hours, which happen routinely under platform load.
    The Slack-history idempotency check (in digest.py) ensures only the first
    fire of the day posts; later delayed fires skip silently.

    Net behaviour: a digest arrives within the morning hours every day, even
    when GitHub silently delays scheduled fires.
    """
    return now_utc.astimezone(ZoneInfo(tz_name)).hour in ALLOWED_DELIVERY_HOURS


def _to_iso_z(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

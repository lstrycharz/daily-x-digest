"""Pure date/time helpers. Both functions take `now_utc` as a parameter so tests can pin time."""

from datetime import datetime


def previous_day_window(now_utc: datetime, tz_name: str) -> tuple[str, str]:
    raise NotImplementedError


def should_run(now_utc: datetime, tz_name: str) -> bool:
    raise NotImplementedError

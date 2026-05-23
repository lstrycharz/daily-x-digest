from datetime import UTC, datetime

from x_digest.time_utils import previous_day_window


def test_previous_day_window_returns_yesterday_et_as_utc_iso() -> None:
    # 2026-05-23 14:00 UTC = 2026-05-23 10:00 EDT (UTC-4). Previous ET day = 2026-05-22.
    # Midnight ET on both 2026-05-22 and 2026-05-23 is EDT (UTC-4), so the window is
    # 2026-05-22T04:00:00Z .. 2026-05-23T04:00:00Z.
    now = datetime(2026, 5, 23, 14, 0, tzinfo=UTC)
    start, end = previous_day_window(now, "America/New_York")
    assert start == "2026-05-22T04:00:00Z"
    assert end == "2026-05-23T04:00:00Z"


def test_previous_day_window_spans_dst_spring_forward() -> None:
    # US DST starts the second Sunday of March. In 2026 that's March 8 at 02:00 ET.
    # Running on Monday March 9 14:00 UTC, the previous ET day is March 8.
    # Midnight on March 8 was still EST (UTC-5) → 2026-03-08T05:00:00Z.
    # Midnight on March 9 is EDT (UTC-4)        → 2026-03-09T04:00:00Z.
    # Window is therefore 23 wall-clock hours of UTC — `zoneinfo` must handle this.
    now = datetime(2026, 3, 9, 14, 0, tzinfo=UTC)
    start, end = previous_day_window(now, "America/New_York")
    assert start == "2026-03-08T05:00:00Z"
    assert end == "2026-03-09T04:00:00Z"


def test_previous_day_window_spans_dst_fall_back() -> None:
    # US DST ends the first Sunday of November. In 2026 that's November 1 at 02:00 ET.
    # Running on Monday November 2 14:00 UTC, the previous ET day is November 1.
    # Midnight on November 1 was still EDT (UTC-4) → 2026-11-01T04:00:00Z.
    # Midnight on November 2 is EST (UTC-5)        → 2026-11-02T05:00:00Z.
    # Window is 25 wall-clock hours of UTC.
    now = datetime(2026, 11, 2, 14, 0, tzinfo=UTC)
    start, end = previous_day_window(now, "America/New_York")
    assert start == "2026-11-01T04:00:00Z"
    assert end == "2026-11-02T05:00:00Z"

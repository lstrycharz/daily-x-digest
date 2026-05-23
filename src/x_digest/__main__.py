"""CLI entrypoint for the X Daily Digest.

Phase 1 scope: fetch one (or a few) accounts → render plain text → print or
post to Slack. The full pipeline (Claude synthesis, Block Kit, alert path,
idempotency check) is layered on in Phases 2 and 3.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from slack_sdk import WebClient

from x_digest.digest import fetch_all, post_plain_text, render_raw_text
from x_digest.time_utils import previous_day_window, should_run

DEFAULT_TZ = "America/New_York"
ACCOUNTS_FILE = Path(__file__).resolve().parents[2] / "accounts.json"


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname.lower(),
            "event": record.getMessage(),
            "stage": getattr(record, "stage", "run"),
        }
        for key, value in record.__dict__.items():
            if key in payload or key in _LOG_RECORD_RESERVED:
                continue
            payload[key] = value
        return json.dumps(payload, default=str)


_LOG_RECORD_RESERVED = frozenset(vars(logging.LogRecord("", 0, "", 0, None, None, None)).keys()) | {
    "message",
    "asctime",
}


def _configure_logging() -> None:
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(_JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
    # httpx logs every request URL at INFO — useful at debug but noisy in production.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _load_accounts(only_handle: str | None) -> list[dict[str, str]]:
    raw = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
    accounts: list[dict[str, str]] = raw["accounts"]
    if only_handle is None:
        return accounts
    matches = [a for a in accounts if a["handle"].lower() == only_handle.lower()]
    if not matches:
        raise SystemExit(f"--account {only_handle!r} not found in accounts.json")
    return matches


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise SystemExit(f"required env var {name} is not set")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="x-digest", description="X Daily Digest")
    parser.add_argument("--force", action="store_true", help="skip the hour gate")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print rendered output; skip Slack post (Slack creds not required)",
    )
    parser.add_argument(
        "--account",
        help="limit to a single handle from accounts.json (useful for the tracer bullet)",
    )
    args = parser.parse_args(argv)

    _configure_logging()
    log = logging.getLogger("x_digest")
    tz_name = os.getenv("DIGEST_TIMEZONE", DEFAULT_TZ)
    now_utc = datetime.now(tz=UTC)

    if not args.force and not should_run(now_utc, tz_name):
        log.info("outside delivery window — exiting", extra={"stage": "run"})
        return 0

    bearer_token = _require_env("X_BEARER_TOKEN")
    start_iso, end_iso = previous_day_window(now_utc, tz_name)
    accounts = _load_accounts(args.account)

    log.info(
        "fetching",
        extra={"stage": "fetch", "accounts": len(accounts), "window": [start_iso, end_iso]},
    )
    tweets, _, failed = fetch_all(accounts, start_iso, end_iso, bearer_token)
    log.info(
        "fetch complete",
        extra={
            "stage": "fetch",
            "accounts_fetched": len(accounts) - len(failed),
            "accounts_failed": len(failed),
            "failed_handles": failed,
            "posts_processed": len(tweets),
        },
    )

    if not tweets:
        log.info("no posts found in window", extra={"stage": "run"})
        return 0

    rendered = render_raw_text(tweets)

    if args.dry_run:
        print(rendered)
        return 0

    slack_client = WebClient(token=_require_env("SLACK_BOT_TOKEN"))
    post_plain_text(slack_client, channel=_require_env("SLACK_CHANNEL_ID"), text=rendered)
    log.info("posted to slack", extra={"stage": "deliver"})
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""CLI entrypoint for the X Daily Digest.

Wires the four-stage pipeline: fetch → normalize → synthesize → post.
The {7,8}-hour gate runs by default; --force bypasses it. --dry-run runs
the full pipeline (including Claude) but prints the validated Digest as
JSON instead of posting to Slack — useful for prompt iteration.
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

from anthropic import Anthropic
from slack_sdk import WebClient

from x_digest.digest import (
    fetch_all,
    normalize,
    post_quiet_day,
    post_to_slack,
    synthesize,
)
from x_digest.time_utils import previous_day_window, should_run

DEFAULT_TZ = "America/New_York"
ACCOUNTS_FILE = Path(__file__).resolve().parents[2] / "accounts.json"
RUNS_DIR = Path(__file__).resolve().parents[2] / "runs"


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


def _save_corpus(corpus: str, date_iso: str) -> None:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    (RUNS_DIR / f"{date_iso}.txt").write_text(corpus, encoding="utf-8")


def _target_date_iso(start_iso: str) -> str:
    return start_iso.split("T")[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="x-digest", description="X Daily Digest")
    parser.add_argument("--force", action="store_true", help="skip the hour gate")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="run the full pipeline but print the Digest as JSON; skip Slack post",
    )
    parser.add_argument(
        "--account",
        help="limit to a single handle from accounts.json",
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
    anthropic_client = Anthropic(api_key=_require_env("ANTHROPIC_API_KEY"))
    slack_client = (
        WebClient(token=_require_env("SLACK_BOT_TOKEN")) if not args.dry_run else None
    )
    slack_channel = _require_env("SLACK_CHANNEL_ID") if not args.dry_run else None

    start_iso, end_iso = previous_day_window(now_utc, tz_name)
    target_date = _target_date_iso(start_iso)
    accounts = _load_accounts(args.account)

    log.info(
        "fetching",
        extra={"stage": "fetch", "accounts": len(accounts), "window": [start_iso, end_iso]},
    )
    tweets, includes, failed = fetch_all(accounts, start_iso, end_iso, bearer_token)
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
        log.info("no posts in window — posting quiet-day message", extra={"stage": "run"})
        if args.dry_run:
            print(f"[dry-run] would post quiet-day message for {target_date}")
        else:
            assert slack_client is not None
            assert slack_channel is not None
            post_quiet_day(slack_client, slack_channel, target_date)
        return 0

    corpus, included, dropped = normalize(tweets, includes)
    _save_corpus(corpus, target_date)
    log.info(
        "normalized",
        extra={
            "stage": "normalize",
            "posts_processed": included,
            "posts_dropped_for_size": dropped,
        },
    )

    fetch_summary = {
        "accounts_fetched": len(accounts) - len(failed),
        "accounts_failed": len(failed),
        "posts_dropped": dropped,
    }
    digest = synthesize(corpus, target_date, fetch_summary, client=anthropic_client)
    log.info(
        "synthesized",
        extra={"stage": "synthesize", "themes": len(digest.themes)},
    )

    if args.dry_run:
        print(digest.model_dump_json(indent=2))
        return 0

    assert slack_client is not None
    assert slack_channel is not None
    post_to_slack(slack_client, slack_channel, digest, fetch_summary)
    log.info("posted to slack", extra={"stage": "deliver"})
    return 0


if __name__ == "__main__":
    sys.exit(main())

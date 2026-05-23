"""The X Daily Digest pipeline.

One deep module: fetch → normalize → synthesize → post.
Phase 1 scope: fetch only.
"""

from typing import Any

import httpx
from slack_sdk import WebClient

X_API_BASE = "https://api.x.com/2"
TWEET_FIELDS = "created_at,public_metrics,referenced_tweets,entities,note_tweet"
EXPANSIONS = "referenced_tweets.id"
MAX_RESULTS_PER_PAGE = 100
HTTP_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)


def fetch_all(
    accounts: list[dict[str, str]],
    start_iso: str,
    end_iso: str,
    bearer_token: str,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], list[str]]:
    """Fetch every top-level post per account in the time window.

    Returns (tweets-with-handle-attached, quoted-tweet-includes-by-id, failed-handles).
    Per-account failures are isolated: one handle's failure must not abort the run.
    Phase 1 scope: no retries, no threshold guard — those land in Phase 2.
    """
    tweets: list[dict[str, Any]] = []
    includes_tweets: dict[str, dict[str, Any]] = {}
    failed: list[str] = []
    headers = {"Authorization": f"Bearer {bearer_token}"}

    with httpx.Client(timeout=HTTP_TIMEOUT, headers=headers) as client:
        for account in accounts:
            handle = account["handle"]
            try:
                page_tweets, page_includes = _fetch_one_account(
                    client, account["user_id"], start_iso, end_iso
                )
            except httpx.HTTPError:
                failed.append(handle)
                continue
            for t in page_tweets:
                t["handle"] = handle
            tweets.extend(page_tweets)
            for inc in page_includes:
                includes_tweets[inc["id"]] = inc

    return tweets, includes_tweets, failed


def _fetch_one_account(
    client: httpx.Client,
    user_id: str,
    start_iso: str,
    end_iso: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    tweets: list[dict[str, Any]] = []
    includes: list[dict[str, Any]] = []
    params: dict[str, str | int] = {
        "exclude": "replies,retweets",
        "start_time": start_iso,
        "end_time": end_iso,
        "max_results": MAX_RESULTS_PER_PAGE,
        "tweet.fields": TWEET_FIELDS,
        "expansions": EXPANSIONS,
    }
    while True:
        response = client.get(f"{X_API_BASE}/users/{user_id}/tweets", params=params)
        response.raise_for_status()
        body = response.json()
        tweets.extend(body.get("data", []))
        includes.extend(body.get("includes", {}).get("tweets", []))
        next_token = body.get("meta", {}).get("next_token")
        if not next_token:
            return tweets, includes
        params["pagination_token"] = next_token


def render_raw_text(tweets: list[dict[str, Any]]) -> str:
    """Phase 1 plain-text rendering — one tweet per block, separated by `---`.

    The themed Block Kit layout lands in Phase 2 alongside Claude synthesis.
    """
    blocks = [
        f"[@{t['handle']} | {t.get('created_at', '')}]\n"
        f"{t.get('text', '')}\n"
        f"https://x.com/{t['handle']}/status/{t['id']}"
        for t in tweets
    ]
    return "\n---\n".join(blocks)


def post_plain_text(
    client: WebClient,
    channel: str,
    text: str,
    max_chars: int = 2000,
) -> None:
    """Phase 1 Slack delivery — single chat.postMessage with a hard truncation."""
    payload = text if len(text) <= max_chars else text[: max_chars - 1] + "…"
    client.chat_postMessage(channel=channel, text=payload)

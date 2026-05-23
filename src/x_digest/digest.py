"""The X Daily Digest pipeline.

One deep module: fetch → normalize → synthesize → post.
Phase 1 scope: fetch only.
"""

from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, Field
from slack_sdk import WebClient

SYSTEM_PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "system.md"


def load_system_prompt() -> str:
    return SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")


class DigestNotablePost(BaseModel):
    handle: str
    excerpt: str
    url: str


class DigestTheme(BaseModel):
    title: str
    synthesis: str
    notable_posts: list[DigestNotablePost] = Field(max_length=8)


class DigestLink(BaseModel):
    title: str
    url: str


class Digest(BaseModel):
    """Validated synthesis output from Claude.

    Array bounds are belt-and-suspenders against prompt-injection driving runaway
    output. The system prompt also asks for 1-8 themes; this cap is enforced.
    """

    date: str
    headline: str
    themes: list[DigestTheme] = Field(min_length=0, max_length=10)
    quick_hits: list[str] = Field(default_factory=list, max_length=15)
    links: list[DigestLink] = Field(default_factory=list)

X_API_BASE = "https://api.x.com/2"
TWEET_FIELDS = "created_at,public_metrics,referenced_tweets,entities,note_tweet"
EXPANSIONS = "referenced_tweets.id"
MAX_RESULTS_PER_PAGE = 100
HTTP_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)
MAX_CORPUS_TOKENS = 120_000
QUOTE_TRUNCATE_CHARS = 280
CHARS_PER_TOKEN_ESTIMATE = 4


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


def normalize(
    raw_tweets: list[dict[str, Any]],
    includes: dict[str, dict[str, Any]],
    *,
    max_corpus_tokens: int = MAX_CORPUS_TOKENS,
) -> tuple[str, int, int]:
    """Turn raw API objects into a clean text corpus ready for the LLM.

    Dedupes by id, sorts chronologically, prefers note_tweet.text for long posts,
    resolves quoted-tweet text from includes (missing → render parent without quote),
    expands t.co URLs via entities.urls, and enforces a soft token cap by dropping
    the lowest-engagement posts (`likes + 2*reposts`) until under the cap.

    Returns (corpus_string, included_count, dropped_count).
    """
    deduped: dict[str, dict[str, Any]] = {}
    for tweet in raw_tweets:
        deduped.setdefault(tweet["id"], tweet)
    chronological = sorted(deduped.values(), key=lambda t: t.get("created_at", ""))
    kept, dropped = _enforce_size_cap(chronological, includes, max_corpus_tokens)
    blocks = [_render_tweet_block(t, includes) for t in kept]
    return "\n---\n".join(blocks), len(kept), dropped


def _engagement_score(tweet: dict[str, Any]) -> int:
    metrics = tweet.get("public_metrics", {})
    return int(metrics.get("like_count", 0)) + 2 * int(metrics.get("retweet_count", 0))


def _enforce_size_cap(
    tweets: list[dict[str, Any]],
    includes: dict[str, dict[str, Any]],
    max_tokens: int,
) -> tuple[list[dict[str, Any]], int]:
    kept = list(tweets)
    dropped = 0
    while kept:
        corpus_chars = sum(len(_render_tweet_block(t, includes)) for t in kept)
        if corpus_chars // CHARS_PER_TOKEN_ESTIMATE <= max_tokens:
            return kept, dropped
        # Drop the lowest-engagement tweet.
        lowest = min(range(len(kept)), key=lambda i: _engagement_score(kept[i]))
        kept.pop(lowest)
        dropped += 1
    return kept, dropped


def _render_tweet_block(tweet: dict[str, Any], includes: dict[str, dict[str, Any]]) -> str:
    handle = tweet["handle"]
    metrics = tweet.get("public_metrics", {})
    likes = metrics.get("like_count", 0)
    reposts = metrics.get("retweet_count", 0)
    header = (
        f"[@{handle} | {tweet.get('created_at', '')} | "
        f"likes {likes} reposts {reposts}]"
    )
    body = _resolve_body(tweet)
    quote_line = _resolve_quote_line(tweet, includes)
    links_line = _resolve_links_line(tweet)
    url_line = f"url: https://x.com/{handle}/status/{tweet['id']}"
    parts = [header, body]
    if quote_line:
        parts.append(quote_line)
    if links_line:
        parts.append(links_line)
    parts.append(url_line)
    return "\n".join(parts)


def _resolve_body(tweet: dict[str, Any]) -> str:
    note = tweet.get("note_tweet")
    if isinstance(note, dict) and note.get("text"):
        return str(note["text"])
    return str(tweet.get("text", ""))


def _resolve_quote_line(tweet: dict[str, Any], includes: dict[str, dict[str, Any]]) -> str:
    for ref in tweet.get("referenced_tweets", []) or []:
        if ref.get("type") != "quoted":
            continue
        quoted = includes.get(ref.get("id", ""))
        if not quoted:
            return ""
        quoted_text = str(quoted.get("text", ""))[:QUOTE_TRUNCATE_CHARS]
        return f'quotes: "{quoted_text}"'
    return ""


def _resolve_links_line(tweet: dict[str, Any]) -> str:
    urls = tweet.get("entities", {}).get("urls", []) or []
    expanded = [u["expanded_url"] for u in urls if u.get("expanded_url")]
    if not expanded:
        return ""
    return "links: " + " ".join(expanded)


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

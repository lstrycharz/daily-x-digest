"""The X Daily Digest pipeline.

One deep module: fetch → normalize → synthesize → post.
Phase 1 scope: fetch only.
"""

import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx
from anthropic import Anthropic
from pydantic import BaseModel, Field, ValidationError
from slack_sdk import WebClient

SYSTEM_PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "system.md"

# Approximate Sonnet 4.6 pricing per million tokens. Update if Anthropic changes them.
ANTHROPIC_INPUT_USD_PER_MTOK = 3.0
ANTHROPIC_OUTPUT_USD_PER_MTOK = 15.0

DEFAULT_CLAUDE_MODEL = "claude-sonnet-4-6"
DEFAULT_CLAUDE_MAX_TOKENS = 5000

SLACK_SECTION_TEXT_MAX = 3000
DIGEST_HEADER_PREFIX = "📰 X Daily Digest"
SLACK_HISTORY_LOOKBACK = 5


def digest_header_marker(date_iso: str) -> str:
    """Stable header prefix used both for rendering and the Phase 3 idempotency check."""
    return f"{DIGEST_HEADER_PREFIX} — {date_iso}"

_log = logging.getLogger("x_digest")


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

RETRY_MAX_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_CAP_SECONDS = 30.0
FAIL_RATIO_THRESHOLD = 0.5
RESOLVE_BATCH_SIZE = 100


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
                # 401 is raised as RuntimeError below and does NOT land here — those are
                # transient/per-account failures (404, 5xx after retry exhaustion, etc).
                failed.append(handle)
                continue
            for t in page_tweets:
                t["handle"] = handle
            tweets.extend(page_tweets)
            for inc in page_includes:
                includes_tweets[inc["id"]] = inc

    if accounts and len(failed) / len(accounts) > FAIL_RATIO_THRESHOLD:
        raise RuntimeError(
            f"{len(failed)}/{len(accounts)} accounts failed — aborting (threshold "
            f"{FAIL_RATIO_THRESHOLD:.0%})"
        )

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
        response = _request_with_retry(client, f"{X_API_BASE}/users/{user_id}/tweets", params)
        body = response.json()
        tweets.extend(body.get("data", []))
        includes.extend(body.get("includes", {}).get("tweets", []))
        next_token = body.get("meta", {}).get("next_token")
        if not next_token:
            return tweets, includes
        params["pagination_token"] = next_token


def resolve_handles_to_ids(
    handles: list[str],
    bearer_token: str,
) -> tuple[dict[str, str], list[str]]:
    """Look up X user IDs for the given handles. Returns (resolved, unresolved).

    Batches up to 100 handles per API call (the X API's limit). Suspended,
    renamed, or deleted handles appear in the response's `errors` array and
    end up in `unresolved` — the caller can warn and skip them.
    """
    resolved: dict[str, str] = {}
    unresolved: list[str] = []
    headers = {"Authorization": f"Bearer {bearer_token}"}

    with httpx.Client(timeout=HTTP_TIMEOUT, headers=headers) as client:
        for batch_start in range(0, len(handles), RESOLVE_BATCH_SIZE):
            batch = handles[batch_start : batch_start + RESOLVE_BATCH_SIZE]
            response = client.get(
                f"{X_API_BASE}/users/by",
                params={"usernames": ",".join(batch)},
            )
            response.raise_for_status()
            body = response.json()
            for user in body.get("data", []) or []:
                resolved[user["username"]] = user["id"]
            for err in body.get("errors", []) or []:
                missing = err.get("value")
                if missing:
                    unresolved.append(missing)

    return resolved, unresolved


def _request_with_retry(
    client: httpx.Client,
    url: str,
    params: dict[str, str | int],
) -> httpx.Response:
    """GET with exponential-backoff retry on 429 and 5xx and network errors.

    401 is raised immediately as a RuntimeError — it affects all accounts and
    must not be isolated as a single-account failure. Other 4xx surface as
    HTTPStatusError so the caller's per-account catch path can record them.
    """
    last_exc: Exception | None = None
    for attempt in range(RETRY_MAX_ATTEMPTS):
        try:
            response = client.get(url, params=params)
        except httpx.RequestError as exc:
            last_exc = exc
            if attempt < RETRY_MAX_ATTEMPTS - 1:
                time.sleep(_backoff_seconds(attempt))
                continue
            raise

        if response.status_code == 401:
            raise RuntimeError("X API auth rejected — check X_BEARER_TOKEN")
        if response.status_code == 429 or response.status_code >= 500:
            if attempt < RETRY_MAX_ATTEMPTS - 1:
                time.sleep(_retry_sleep_seconds(response, attempt))
                continue
            response.raise_for_status()  # exhausted → caller treats as per-account failure
        response.raise_for_status()  # other 4xx → caller handles as per-account failure
        return response

    # Should be unreachable, but for the type checker.
    raise last_exc or RuntimeError("retry loop exited without returning")


def _backoff_seconds(attempt: int) -> float:
    return float(min(BACKOFF_BASE_SECONDS * (2**attempt), BACKOFF_CAP_SECONDS))


def _retry_sleep_seconds(response: httpx.Response, attempt: int) -> float:
    """Honor `x-rate-limit-reset` on 429 if present; otherwise exponential backoff."""
    reset_raw = response.headers.get("x-rate-limit-reset")
    if reset_raw and response.status_code == 429:
        try:
            reset_at = int(reset_raw)
        except ValueError:
            return _backoff_seconds(attempt)
        delta: float = max(0.0, reset_at - time.time())
        return min(delta, BACKOFF_CAP_SECONDS)
    return _backoff_seconds(attempt)


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


def synthesize(
    corpus: str,
    target_date: str,
    fetch_summary: dict[str, int],
    client: Anthropic,
    model: str = DEFAULT_CLAUDE_MODEL,
    max_tokens: int = DEFAULT_CLAUDE_MAX_TOKENS,
) -> Digest:
    """Call Claude with the corpus and return a validated Digest.

    Behaviour:
      - stop_reason == "max_tokens" → raise (no parse-retry).
      - JSON parse / validation failure → one retry with the error fed back.
      - Second failure → raises the underlying error.
      - Logs a cost-ledger record on every successful parse.
    """
    system_prompt = load_system_prompt()
    user_message = _build_user_message(corpus, target_date, fetch_summary)

    response = _ask(client, model, max_tokens, system_prompt, user_message)
    try:
        digest = _parse_digest(_response_text(response))
        _log_cost(response, model)
        return digest
    except (json.JSONDecodeError, ValidationError) as err:
        retry_message = (
            user_message
            + "\n\nYour previous response failed validation:\n"
            + str(err)
            + "\nReturn corrected strict JSON matching the schema. No code fences, no prose."
        )
        retry_response = _ask(client, model, max_tokens, system_prompt, retry_message)
        digest = _parse_digest(_response_text(retry_response))
        _log_cost(retry_response, model, retried=True)
        return digest


def _ask(
    client: Anthropic,
    model: str,
    max_tokens: int,
    system_prompt: str,
    user_message: str,
) -> Any:
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    )
    if response.stop_reason == "max_tokens":
        raise RuntimeError(
            "Claude hit max_tokens — raise CLAUDE_MAX_TOKENS or tighten the prompt"
        )
    return response


def _response_text(response: Any) -> str:
    return str(response.content[0].text)


def _parse_digest(text: str) -> Digest:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        # Strip ```json / ``` fences.
        cleaned = cleaned.removeprefix("```json").removeprefix("```").rstrip("` \n")
    payload = json.loads(cleaned)
    return Digest.model_validate(payload)


def _build_user_message(corpus: str, target_date: str, fetch_summary: dict[str, int]) -> str:
    notes: list[str] = []
    failed = fetch_summary.get("accounts_failed", 0)
    dropped = fetch_summary.get("posts_dropped", 0)
    if failed:
        notes.append(f"{failed} account(s) failed to fetch and were skipped.")
    if dropped:
        notes.append(f"{dropped} low-engagement post(s) were dropped to fit the size budget.")
    notes_str = ("\n" + "\n".join(notes)) if notes else ""
    return (
        f"Target date: {target_date}\n"
        f"Posts follow, separated by `---` lines.{notes_str}\n\n"
        f"{corpus}"
    )


def _log_cost(response: Any, model: str, retried: bool = False) -> None:
    usage = response.usage
    in_tokens = int(usage.input_tokens)
    out_tokens = int(usage.output_tokens)
    cost = (
        in_tokens / 1_000_000 * ANTHROPIC_INPUT_USD_PER_MTOK
        + out_tokens / 1_000_000 * ANTHROPIC_OUTPUT_USD_PER_MTOK
    )
    _log.info(
        "synthesize complete",
        extra={
            "stage": "synthesize",
            "anthropic_model": model,
            "anthropic_input_tokens": in_tokens,
            "anthropic_output_tokens": out_tokens,
            "anthropic_cost_usd_estimate": round(cost, 4),
            "retried": retried,
        },
    )


def post_to_slack(
    client: WebClient,
    channel: str,
    digest: Digest,
    fetch_summary: dict[str, int],
) -> None:
    """Render the digest as Block Kit and post to Slack via chat.postMessage."""
    blocks = _build_blocks(digest, fetch_summary)
    client.chat_postMessage(
        channel=channel,
        blocks=blocks,
        text=f"{digest_header_marker(digest.date)} — {digest.headline}",
    )


def already_posted_today(client: WebClient, channel: str, date_iso: str) -> bool:
    """True if a digest for `date_iso` already appears in the channel's recent history.

    Replaces the state-file mechanism — one Slack call per run, no artifacts.
    On any API failure, returns False and logs a warning: better to risk a rare
    double-post than to skip the digest entirely.
    """
    marker = digest_header_marker(date_iso)
    try:
        history = client.conversations_history(channel=channel, limit=SLACK_HISTORY_LOOKBACK)
    except Exception as exc:
        _log.warning(
            "idempotency check failed — proceeding without it",
            extra={"stage": "deliver", "error": str(exc)},
        )
        return False
    messages: list[dict[str, Any]] = history.get("messages") or []
    for message in messages:
        text = message.get("text", "")
        if text.startswith(marker):
            return True
    return False


def post_quiet_day(client: WebClient, channel: str, date_iso: str) -> None:
    """Post the one-line message used when no posts were found in the window."""
    text = (
        f"{digest_header_marker(date_iso)}\n"
        f"Quiet day — no top-level posts from the tracked accounts on {date_iso}."
    )
    client.chat_postMessage(channel=channel, text=text)


def _build_blocks(digest: Digest, fetch_summary: dict[str, int]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"{digest_header_marker(digest.date)} — {digest.headline}",
                "emoji": True,
            },
        }
    ]
    for theme in digest.themes:
        blocks.extend(_theme_blocks(theme))
    if digest.quick_hits:
        bulleted = "\n".join(f"• {h}" for h in digest.quick_hits)
        blocks.extend(_section_chunks(f"*Quick hits*\n{bulleted}"))
    if digest.links:
        link_lines = [f"<{link.url}|{link.title}>" for link in digest.links]
        blocks.extend(_section_chunks("*Links*\n" + "\n".join(link_lines)))
    blocks.append(_coverage_footer(fetch_summary))
    return blocks


def _theme_blocks(theme: DigestTheme) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = [{"type": "divider"}]
    blocks.extend(_section_chunks(f"*{theme.title}*\n{theme.synthesis}"))
    if theme.notable_posts:
        notable_lines = [
            f"• <{p.url}|@{p.handle.lstrip('@')}>: {p.excerpt}" for p in theme.notable_posts
        ]
        blocks.extend(_section_chunks("\n".join(notable_lines)))
    return blocks


def _section_chunks(text: str) -> list[dict[str, Any]]:
    """Split a long section into multiple section blocks under the Slack char limit."""
    if len(text) <= SLACK_SECTION_TEXT_MAX:
        return [_section(text)]
    chunks: list[str] = []
    cursor = 0
    while cursor < len(text):
        chunks.append(text[cursor : cursor + SLACK_SECTION_TEXT_MAX])
        cursor += SLACK_SECTION_TEXT_MAX
    return [_section(c) for c in chunks]


def _section(text: str) -> dict[str, Any]:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _coverage_footer(fetch_summary: dict[str, int]) -> dict[str, Any]:
    fetched = fetch_summary.get("accounts_fetched", 0)
    failed = fetch_summary.get("accounts_failed", 0)
    dropped = fetch_summary.get("posts_dropped", 0)
    total = fetched + failed
    parts = [f"{fetched}/{total} accounts"]
    if failed:
        parts.append(f"{failed} unreachable")
    if dropped:
        parts.append(f"{dropped} posts dropped for size")
    return {
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": " · ".join(parts)}],
    }



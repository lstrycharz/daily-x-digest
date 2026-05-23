import json
from unittest.mock import Mock

import httpx
import pytest
import respx
from pydantic import ValidationError

from x_digest.digest import (
    Digest,
    DigestLink,
    DigestNotablePost,
    DigestTheme,
    digest_header_marker,
    fetch_all,
    normalize,
    post_plain_text,
    post_quiet_day,
    post_to_slack,
    render_raw_text,
    synthesize,
)


def _tweet(
    tweet_id: str,
    handle: str = "alice",
    text: str = "hello",
    created_at: str = "2026-05-22T10:00:00.000Z",
    likes: int = 0,
    reposts: int = 0,
    note_tweet_text: str | None = None,
    referenced_tweets: list[dict[str, str]] | None = None,
    urls: list[dict[str, str]] | None = None,
) -> dict[str, object]:
    tw: dict[str, object] = {
        "id": tweet_id,
        "handle": handle,
        "text": text,
        "created_at": created_at,
        "public_metrics": {"like_count": likes, "retweet_count": reposts},
    }
    if note_tweet_text is not None:
        tw["note_tweet"] = {"text": note_tweet_text}
    if referenced_tweets is not None:
        tw["referenced_tweets"] = referenced_tweets
    if urls is not None:
        tw["entities"] = {"urls": urls}
    return tw


@respx.mock
def test_fetch_all_one_account_attaches_handle_to_tweets() -> None:
    respx.get("https://api.x.com/2/users/123/tweets").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "1",
                        "text": "hello world",
                        "created_at": "2026-05-22T10:00:00.000Z",
                    }
                ],
            },
        )
    )
    accounts = [{"handle": "test_account", "user_id": "123"}]

    tweets, includes, failed = fetch_all(
        accounts,
        start_iso="2026-05-22T04:00:00Z",
        end_iso="2026-05-23T04:00:00Z",
        bearer_token="fake-token",
    )

    assert len(tweets) == 1
    assert tweets[0]["id"] == "1"
    assert tweets[0]["handle"] == "test_account"
    assert includes == {}
    assert failed == []


@respx.mock
def test_fetch_all_follows_pagination_token_until_exhausted() -> None:
    respx.get("https://api.x.com/2/users/123/tweets").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "data": [{"id": "1", "text": "page 1"}],
                    "meta": {"next_token": "page2"},
                },
            ),
            httpx.Response(
                200,
                json={
                    "data": [{"id": "2", "text": "page 2"}],
                    "meta": {"next_token": "page3"},
                },
            ),
            httpx.Response(
                200,
                json={"data": [{"id": "3", "text": "page 3"}]},
            ),
        ]
    )
    accounts = [{"handle": "test_account", "user_id": "123"}]

    tweets, _, failed = fetch_all(
        accounts,
        start_iso="2026-05-22T04:00:00Z",
        end_iso="2026-05-23T04:00:00Z",
        bearer_token="fake-token",
    )

    assert [t["id"] for t in tweets] == ["1", "2", "3"]
    assert failed == []


@respx.mock
def test_fetch_all_isolates_single_account_failure_and_continues() -> None:
    respx.get("https://api.x.com/2/users/404/tweets").mock(
        return_value=httpx.Response(404, json={"errors": [{"message": "not found"}]})
    )
    respx.get("https://api.x.com/2/users/200/tweets").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "1"}]})
    )
    accounts = [
        {"handle": "missing_user", "user_id": "404"},
        {"handle": "valid_user", "user_id": "200"},
    ]

    tweets, _, failed = fetch_all(
        accounts,
        start_iso="2026-05-22T04:00:00Z",
        end_iso="2026-05-23T04:00:00Z",
        bearer_token="fake-token",
    )

    assert [t["handle"] for t in tweets] == ["valid_user"]
    assert failed == ["missing_user"]


def test_render_raw_text_formats_each_tweet_with_handle_and_url() -> None:
    # Phase 1 plain-text renderer. The themed Block Kit layout comes in Phase 2.
    tweets = [
        {
            "id": "1",
            "handle": "alice",
            "text": "first post",
            "created_at": "2026-05-22T10:00:00.000Z",
        },
        {
            "id": "2",
            "handle": "bob",
            "text": "second post",
            "created_at": "2026-05-22T11:30:00.000Z",
        },
    ]
    rendered = render_raw_text(tweets)
    assert "@alice" in rendered
    assert "@bob" in rendered
    assert "first post" in rendered
    assert "second post" in rendered
    assert "https://x.com/alice/status/1" in rendered
    assert "https://x.com/bob/status/2" in rendered


def test_post_plain_text_truncates_to_max_chars() -> None:
    mock_client = Mock()
    post_plain_text(mock_client, channel="C123", text="x" * 5000, max_chars=2000)
    sent = mock_client.chat_postMessage.call_args.kwargs["text"]
    assert sent.startswith("xxx")
    assert sent.endswith("…")
    assert len(sent) <= 2001  # 2000 + the ellipsis


def test_post_plain_text_sends_short_text_unchanged() -> None:
    mock_client = Mock()
    post_plain_text(mock_client, channel="C123", text="short message", max_chars=2000)
    mock_client.chat_postMessage.assert_called_once_with(channel="C123", text="short message")


def _minimal_digest_payload(themes: int = 1) -> dict[str, object]:
    return {
        "date": "2026-05-22",
        "headline": "Sample headline",
        "themes": [
            {
                "title": f"Theme {i}",
                "synthesis": "Prose paragraph.",
                "notable_posts": [
                    {"handle": "@alice", "excerpt": "quote", "url": "https://x.com/alice/status/1"}
                ],
            }
            for i in range(themes)
        ],
        "quick_hits": ["one-liner"],
        "links": [{"title": "Example", "url": "https://example.com"}],
    }


def test_digest_accepts_a_well_formed_payload() -> None:
    digest = Digest.model_validate(_minimal_digest_payload())
    assert digest.headline == "Sample headline"
    assert len(digest.themes) == 1
    assert digest.themes[0].notable_posts[0].handle == "@alice"


def test_digest_rejects_more_than_ten_themes() -> None:
    payload = _minimal_digest_payload(themes=11)
    with pytest.raises(ValidationError):
        Digest.model_validate(payload)


def test_digest_rejects_more_than_eight_notable_posts_per_theme() -> None:
    payload = _minimal_digest_payload()
    payload["themes"][0]["notable_posts"] = [  # type: ignore[index]
        {"handle": f"@h{i}", "excerpt": "e", "url": f"https://x.com/h{i}/status/{i}"}
        for i in range(9)
    ]
    with pytest.raises(ValidationError):
        Digest.model_validate(payload)


def test_digest_rejects_more_than_fifteen_quick_hits() -> None:
    payload = _minimal_digest_payload()
    payload["quick_hits"] = [f"hit {i}" for i in range(16)]
    with pytest.raises(ValidationError):
        Digest.model_validate(payload)


def test_digest_models_are_directly_importable() -> None:
    # Smoke: each model exists and accepts a minimal instance via field args.
    DigestNotablePost(handle="@a", excerpt="x", url="https://x.com/a/status/1")
    DigestLink(title="t", url="https://example.com")
    DigestTheme(title="t", synthesis="s", notable_posts=[])


def test_normalize_returns_empty_corpus_for_empty_input() -> None:
    corpus, included, dropped = normalize([], {})
    assert corpus == ""
    assert included == 0
    assert dropped == 0


def test_normalize_renders_one_tweet_with_metrics_and_canonical_url() -> None:
    tweets = [_tweet("1", handle="alice", text="hello world", likes=12, reposts=4)]
    corpus, included, dropped = normalize(tweets, {})
    assert included == 1
    assert dropped == 0
    assert "@alice" in corpus
    assert "likes 12" in corpus
    assert "reposts 4" in corpus
    assert "hello world" in corpus
    assert "https://x.com/alice/status/1" in corpus


def test_normalize_dedupes_by_tweet_id() -> None:
    tweets = [_tweet("1", text="first"), _tweet("1", text="duplicate copy")]
    corpus, included, _ = normalize(tweets, {})
    assert included == 1
    assert corpus.count("https://x.com/alice/status/1") == 1


def test_normalize_sorts_tweets_chronologically_ascending() -> None:
    earlier = _tweet("1", text="EARLY", created_at="2026-05-22T08:00:00.000Z")
    later = _tweet("2", text="LATE", created_at="2026-05-22T22:00:00.000Z")
    corpus, _, _ = normalize([later, earlier], {})
    assert corpus.index("EARLY") < corpus.index("LATE")


def test_normalize_prefers_note_tweet_text_over_truncated_text() -> None:
    tweets = [_tweet("1", text="short truncated…", note_tweet_text="full long-form body")]
    corpus, _, _ = normalize(tweets, {})
    assert "full long-form body" in corpus
    assert "short truncated" not in corpus


def test_normalize_resolves_quoted_tweet_text_from_includes() -> None:
    tweets = [
        _tweet("1", text="commentary", referenced_tweets=[{"type": "quoted", "id": "999"}])
    ]
    includes = {"999": {"id": "999", "text": "the quoted source"}}
    corpus, _, _ = normalize(tweets, includes)
    assert "the quoted source" in corpus


def test_normalize_handles_missing_quoted_source_without_failing() -> None:
    tweets = [
        _tweet("1", text="commentary", referenced_tweets=[{"type": "quoted", "id": "missing"}])
    ]
    corpus, included, _ = normalize(tweets, {})  # includes is empty
    assert included == 1
    assert "commentary" in corpus
    assert "missing" not in corpus  # no broken-quote string leaks through


def test_normalize_expands_tco_urls_to_full_destination() -> None:
    tweets = [
        _tweet(
            "1",
            text="check this https://t.co/abc",
            urls=[{"url": "https://t.co/abc", "expanded_url": "https://example.com/article"}],
        )
    ]
    corpus, _, _ = normalize(tweets, {})
    assert "https://example.com/article" in corpus
    assert "https://t.co/abc" not in corpus.split("url:")[1]  # not in the url: section


def test_normalize_drops_lowest_engagement_posts_when_over_token_cap() -> None:
    # With a tight cap, the highest-engagement tweet survives, the lowest is dropped.
    high = _tweet("1", text="x" * 800, likes=1000, reposts=500)
    low = _tweet("2", text="y" * 800, likes=1, reposts=0)
    corpus, included, dropped = normalize([high, low], {}, max_corpus_tokens=300)
    assert included == 1
    assert dropped == 1
    assert "x" * 100 in corpus  # high-engagement body present
    assert "y" * 100 not in corpus  # low-engagement body dropped


_VALID_DIGEST_JSON = json.dumps(_minimal_digest_payload())


def _mock_anthropic_message(
    text: str, stop_reason: str = "end_turn", in_tokens: int = 100, out_tokens: int = 100
) -> Mock:
    msg = Mock()
    msg.content = [Mock(text=text)]
    msg.stop_reason = stop_reason
    msg.usage = Mock(input_tokens=in_tokens, output_tokens=out_tokens)
    return msg


def test_synthesize_returns_validated_digest_on_clean_json() -> None:
    client = Mock()
    client.messages.create.return_value = _mock_anthropic_message(_VALID_DIGEST_JSON)
    digest = synthesize(
        corpus="some corpus",
        target_date="2026-05-22",
        fetch_summary={"accounts_failed": 0, "posts_dropped": 0},
        client=client,
    )
    assert isinstance(digest, Digest)
    assert digest.headline == "Sample headline"
    client.messages.create.assert_called_once()


def test_synthesize_strips_code_fences_before_parsing() -> None:
    fenced = f"```json\n{_VALID_DIGEST_JSON}\n```"
    client = Mock()
    client.messages.create.return_value = _mock_anthropic_message(fenced)
    digest = synthesize(
        corpus="x",
        target_date="2026-05-22",
        fetch_summary={"accounts_failed": 0, "posts_dropped": 0},
        client=client,
    )
    assert digest.headline == "Sample headline"


def test_synthesize_raises_on_max_tokens_without_retry() -> None:
    client = Mock()
    client.messages.create.return_value = _mock_anthropic_message(
        "{partial json", stop_reason="max_tokens"
    )
    with pytest.raises(RuntimeError, match="max_tokens"):
        synthesize(
            corpus="x",
            target_date="2026-05-22",
            fetch_summary={"accounts_failed": 0, "posts_dropped": 0},
            client=client,
        )
    assert client.messages.create.call_count == 1  # no retry


def test_synthesize_retries_once_on_malformed_json_then_succeeds() -> None:
    client = Mock()
    client.messages.create.side_effect = [
        _mock_anthropic_message("definitely not json"),
        _mock_anthropic_message(_VALID_DIGEST_JSON),
    ]
    digest = synthesize(
        corpus="x",
        target_date="2026-05-22",
        fetch_summary={"accounts_failed": 0, "posts_dropped": 0},
        client=client,
    )
    assert digest.headline == "Sample headline"
    assert client.messages.create.call_count == 2


def test_synthesize_raises_when_retry_also_fails() -> None:
    client = Mock()
    client.messages.create.side_effect = [
        _mock_anthropic_message("not json #1"),
        _mock_anthropic_message("still not json #2"),
    ]
    with pytest.raises((json.JSONDecodeError, ValidationError)):
        synthesize(
            corpus="x",
            target_date="2026-05-22",
            fetch_summary={"accounts_failed": 0, "posts_dropped": 0},
            client=client,
        )
    assert client.messages.create.call_count == 2


def _sample_digest(synthesis: str = "Prose paragraph.") -> Digest:
    return Digest(
        date="2026-05-22",
        headline="The day in one line",
        themes=[
            DigestTheme(
                title="First Theme",
                synthesis=synthesis,
                notable_posts=[
                    DigestNotablePost(
                        handle="@alice",
                        excerpt="alice quote",
                        url="https://x.com/alice/status/1",
                    )
                ],
            )
        ],
        quick_hits=["Quick hit one", "Quick hit two"],
        links=[DigestLink(title="Background reading", url="https://example.com/article")],
    )


def _summary(fetched: int = 50, failed: int = 2, dropped: int = 0) -> dict[str, int]:
    return {"accounts_fetched": fetched, "accounts_failed": failed, "posts_dropped": dropped}


def test_post_to_slack_header_carries_idempotency_marker() -> None:
    client = Mock()
    post_to_slack(client, "C123", _sample_digest(), _summary())
    blocks = client.chat_postMessage.call_args.kwargs["blocks"]
    assert blocks[0]["type"] == "header"
    header_text = blocks[0]["text"]["text"]
    assert digest_header_marker("2026-05-22") in header_text


def test_post_to_slack_renders_theme_title_synthesis_and_notable_posts() -> None:
    client = Mock()
    post_to_slack(client, "C123", _sample_digest(), _summary())
    flat = json.dumps(client.chat_postMessage.call_args.kwargs["blocks"])
    assert "*First Theme*" in flat
    assert "Prose paragraph." in flat
    assert "@alice" in flat
    assert "alice quote" in flat
    assert "https://x.com/alice/status/1" in flat


def test_post_to_slack_includes_quick_hits_links_and_coverage_footer() -> None:
    client = Mock()
    post_to_slack(client, "C123", _sample_digest(), _summary(fetched=48, failed=2, dropped=12))
    flat = json.dumps(client.chat_postMessage.call_args.kwargs["blocks"])
    assert "Quick hit one" in flat
    assert "Quick hit two" in flat
    assert "Background reading" in flat
    assert "https://example.com/article" in flat
    assert "48" in flat and "50" in flat  # coverage 48/50
    assert "12" in flat  # dropped count


def test_post_to_slack_chunks_synthesis_over_section_text_limit() -> None:
    huge = "x" * 6500  # well over the ~3000-char Slack section limit
    client = Mock()
    post_to_slack(client, "C123", _sample_digest(synthesis=huge), _summary())
    blocks = client.chat_postMessage.call_args.kwargs["blocks"]
    section_texts = [
        b["text"]["text"] for b in blocks if b.get("type") == "section" and "text" in b
    ]
    # No single section's text should exceed the limit.
    assert all(len(t) <= 3000 for t in section_texts)
    # The chunks combined must still contain the body.
    joined = "".join(section_texts)
    assert "x" * 6000 in joined


def test_post_to_slack_passes_fallback_text_alongside_blocks() -> None:
    # Slack recommends always sending a `text` fallback for notifications.
    client = Mock()
    post_to_slack(client, "C123", _sample_digest(), _summary())
    kwargs = client.chat_postMessage.call_args.kwargs
    assert kwargs["text"]
    assert "X Daily Digest" in kwargs["text"]


def test_post_quiet_day_posts_one_line_message_with_marker() -> None:
    client = Mock()
    post_quiet_day(client, "C123", date_iso="2026-05-22")
    kwargs = client.chat_postMessage.call_args.kwargs
    assert kwargs["channel"] == "C123"
    assert digest_header_marker("2026-05-22") in kwargs["text"]
    assert "Quiet day" in kwargs["text"]


def test_synthesize_logs_cost_ledger(caplog: pytest.LogCaptureFixture) -> None:
    client = Mock()
    client.messages.create.return_value = _mock_anthropic_message(
        _VALID_DIGEST_JSON, in_tokens=42100, out_tokens=1850
    )
    with caplog.at_level("INFO", logger="x_digest"):
        synthesize(
            corpus="x",
            target_date="2026-05-22",
            fetch_summary={"accounts_failed": 0, "posts_dropped": 0},
            client=client,
        )
    cost_records = [
        r for r in caplog.records if getattr(r, "anthropic_input_tokens", None) == 42100
    ]
    assert cost_records, "expected a log record carrying the cost ledger fields"
    record = cost_records[0]
    assert record.anthropic_output_tokens == 1850  # type: ignore[attr-defined]
    assert isinstance(record.anthropic_cost_usd_estimate, float)  # type: ignore[attr-defined]
    assert record.anthropic_cost_usd_estimate > 0  # type: ignore[attr-defined]

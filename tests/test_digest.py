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
    fetch_all,
    post_plain_text,
    render_raw_text,
)


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

import httpx
import respx

from x_digest.digest import fetch_all


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

# X Daily Digest

Scheduled agent that fetches yesterday's top-level posts from a curated set of X (Twitter) accounts, synthesizes them with Claude Sonnet 4.6 into a themed editorial digest, and posts the result to a Slack channel around 7 AM ET. Fully autonomous: no human approval gates, no state file, no maintenance after setup beyond editing `accounts.json` when you want to follow new accounts.

> **The repo must be private.** `accounts.json` reveals the curated set of accounts you follow as signal — that's personal data worth not leaking.

---

## Architecture (one paragraph)

Four-stage sequential pipeline triggered by GitHub Actions cron. `fetch_all` calls `GET /2/users/:id/tweets` per account (server-side `exclude=replies,retweets`, no `author_id` expansion to keep X API costs down). `normalize` dedupes, sorts, resolves quoted-tweet text from `includes`, prefers `note_tweet.text` for long posts, expands `t.co` URLs, and enforces a soft 120k-token corpus cap by dropping the lowest-engagement posts. `synthesize` calls Claude Sonnet 4.6 with a strict-JSON output contract, retries once on a parse failure with the validation error fed back, and raises immediately on `stop_reason="max_tokens"`. `post_to_slack` renders the validated `Digest` into Block Kit. Idempotency comes from a `conversations.history` lookup — no state file, no GH Actions artifact. Failure alerts live in the workflow's `if: failure()` step, not in the module. See [docs/build-spec.md](docs/build-spec.md) if it exists, or the project's plan file under `~/.claude/plans/`.

---

## Setup

### 1. X (Twitter) API access

Create a project in the [X Developer Console](https://developer.x.com) and grab an **app-only bearer token**. The endpoint used (`GET /2/users/:id/tweets`) is available on the current pay-per-use plan. No OAuth user-context flow is needed.

### 2. Anthropic API access

Get a key at [console.anthropic.com](https://console.anthropic.com). The model defaults to `claude-sonnet-4-6`; you can override via `CLAUDE_MODEL`.

### 3. Slack app

Create a [new Slack app](https://api.slack.com/apps) and add a **Bot User** with these OAuth scopes:

- `chat:write` — to post the digest.
- `channels:history` (public channel) **or** `groups:history` (private channel) — for the idempotency check.

Install the app to your workspace and copy the **Bot User OAuth Token** (`xoxb-…`). Invite the bot to the target channel: `/invite @your-bot-name`.

Separately, create a Slack [**Incoming Webhook**](https://api.slack.com/messaging/webhooks) pointing at the same channel (or a different "alerts" channel). This is the URL the GitHub Actions workflow uses to post failure alerts — decoupled from the bot token so the bot isn't a single point of failure.

### 4. GitHub repository secrets

In the repo's **Settings → Secrets and variables → Actions**, add:

| Secret | Source |
|---|---|
| `X_BEARER_TOKEN` | step 1 |
| `ANTHROPIC_API_KEY` | step 2 |
| `SLACK_BOT_TOKEN` | step 3 (the `xoxb-…` token) |
| `SLACK_CHANNEL_ID` | the channel ID (right-click channel → "View channel details" → bottom) |
| `SLACK_FAILURE_WEBHOOK` | step 3 (the incoming webhook URL) |

### 5. Local development setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env
# edit .env with the same values as above (Slack creds optional if you only --dry-run)
```

### 6. Curate accounts.json and resolve user_ids

Edit `accounts.json` to list the handles you want to follow. Then run the resolver to fill in `user_id`s:

```bash
export $(grep -v '^#' .env | xargs)
.venv/bin/python -m x_digest resolve-ids
```

Re-run it any time you add new handles. Suspended/renamed handles are logged as warnings; the rest get resolved.

---

## Running

### Local dry-run

```bash
.venv/bin/python -m x_digest --force --dry-run
```

Runs the full pipeline (including Claude synthesis) against live X + Anthropic APIs, but prints the validated `Digest` as JSON to stdout instead of posting to Slack. Useful for prompt iteration. The normalized corpus is saved to `runs/{date}.txt` (gitignored) so you can re-run synthesis with a different prompt without re-paying the X API.

Limit to one handle for cheaper tests:

```bash
.venv/bin/python -m x_digest --force --dry-run --account karpathy
```

### Production

Push to the configured branch. The GitHub Actions cron at 11:00 and 12:00 UTC fires the workflow; the `{7, 8}`-hour ET gate and Slack-history idempotency check together ensure exactly one run per day. You don't need to do anything else.

To force a manual run, use **Actions → X Daily Digest → Run workflow**. The hour gate still applies, so the manual run only proceeds during 7 or 8 AM ET. For off-hour testing, run locally with `--force`.

---

## Configuration

### Environment variables

| Variable | Required | Default |
|---|---|---|
| `X_BEARER_TOKEN` | yes | — |
| `ANTHROPIC_API_KEY` | yes | — |
| `SLACK_BOT_TOKEN` | yes (except `--dry-run`) | — |
| `SLACK_CHANNEL_ID` | yes (except `--dry-run`) | — |
| `CLAUDE_MODEL` | no | `claude-sonnet-4-6` |
| `DIGEST_TIMEZONE` | no | `America/New_York` |
| `LOG_LEVEL` | no | `INFO` |

Everything else (corpus token cap, max output tokens, fail-ratio threshold, allowed hours) lives as a module-level constant in `src/x_digest/digest.py`. Tune in code if you need to.

### accounts.json

Hand-curated. JSON array of `{handle, user_id}` objects. The resolver fills in `user_id`s; you only ever touch `handle`. Suspended/renamed handles get warned about and skipped on the next resolve.

---

## Operations

### Cost envelope

Each run costs roughly `(corpus_tokens × $3/M) + (output_tokens × $15/M)` on Sonnet 4.6, plus the per-call X API charges (varies by plan). A 4-post day was ~$0.012 in synthesis cost in early testing. With ~50 accounts and a typical 100–300 posts a day, expect roughly `$0.10–$0.50/run`, or `$3–$15/month`. **Watch actual numbers in the cost-ledger log lines for the first week** and revisit if surprising.

### Logs

Every external call and the final run summary emit a structured JSON log line to stdout. GH Actions captures them in the workflow run output. Useful fields: `accounts_fetched`, `accounts_failed`, `failed_handles`, `posts_processed`, `posts_dropped_for_size`, `themes`, `anthropic_input_tokens`, `anthropic_output_tokens`, `anthropic_cost_usd_estimate`.

### Failure alerts

Any non-zero exit triggers the workflow's `if: failure()` step, which posts to `SLACK_FAILURE_WEBHOOK` with a link to the failing run. The pipeline itself never posts failure messages — that path moved out of the module.

The expected failure modes and what they mean:

| Symptom in the alert / log | Likely cause |
|---|---|
| `RuntimeError: X API auth rejected` | `X_BEARER_TOKEN` expired or wrong. |
| `RuntimeError: Claude hit max_tokens` | Output didn't fit; raise `DEFAULT_CLAUDE_MAX_TOKENS` or tighten the prompt. |
| `RuntimeError: N/M accounts failed` | Systemic problem (rate limits, outage). Inspect the `failed_handles` field. |
| `ValidationError` propagating from `synthesize` | Two consecutive bad JSON responses. Inspect the corpus in `runs/{date}.txt`, then iterate on the prompt. |
| Job missed for a day | GH Actions outage. Out of scope by design — the next day's run resumes normally. |

### Things to verify periodically

- The X API endpoint, `exclude` parameter, and billing categories haven't changed.
- Slack Block Kit limits (~3000 chars / section, ~50 blocks / message) haven't tightened.
- Anthropic's pricing constants in `digest.py` (`ANTHROPIC_INPUT_USD_PER_MTOK`, `ANTHROPIC_OUTPUT_USD_PER_MTOK`) still match your billing.
- `CLAUDE_MODEL` is still the latest Sonnet variant if you want the newest model.

---

## Development

```bash
.venv/bin/pytest              # unit tests
.venv/bin/mypy src/           # type check (--strict)
.venv/bin/ruff check src/ tests/   # lint
```

Strict TDD: every behaviour gets a failing test before the implementation. Three source files (`digest.py`, `time_utils.py`, `__main__.py`); one deep module by design. See `.claude/CLAUDE.md` for the project-specific rules.

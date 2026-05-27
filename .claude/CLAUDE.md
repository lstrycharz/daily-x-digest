# Project Instructions

<!-- ⚠️  THIS FILE IS AUTO-POPULATED after the first planning session.
     When you run plan mode for the first time, Claude will fill in
     Tech Stack, Commands, Project Structure, and Rules based on the plan.
     Review and adjust as needed. -->

## Session Start

**Fresh project (no PROGRESS.md):**
Run the full test suite to orient yourself on project scope and current state. Do not proceed if tests are failing unless the task is specifically to fix them.

**Resuming work (PROGRESS.md exists):**
1. Read `.claude/PROGRESS.md` for handoff context
2. Run `git log --oneline -10` to see recent commits
3. Run the full test suite — confirm current state is green
4. Read `tasks/todo.md` and `tasks/lessons.md` if they exist
5. Pick the highest-priority incomplete item from PROGRESS.md
6. Begin work — do not re-implement anything marked as Completed

## Tech Stack
Python 3.11+ · `httpx` (sync) · `anthropic` SDK · `slack-sdk` · `pydantic` v2 · stdlib `logging` with a JSON formatter · `respx` (httpx mocks in tests). Three source files: `digest.py` (the pipeline), `time_utils.py` (date math), `__main__.py` (CLI + config + logging setup). Deployed via GitHub Actions cron. Failure alerts handled by the workflow's `if: failure()` step, not by the module.

## Commands
- Create venv: `python3 -m venv .venv`
- Install: `.venv/bin/pip install -e ".[dev]"`
- Test: `.venv/bin/pytest`
- Lint: `.venv/bin/ruff check src/ tests/`
- Type check: `.venv/bin/mypy src/`
- Run dry: `.venv/bin/python -m x_digest --force --dry-run`
- Run dry against one account: `.venv/bin/python -m x_digest --force --dry-run --account <handle>`

## Project Structure
```
src/x_digest/
  digest.py        # the whole pipeline: fetch → normalize → synthesize → post
  time_utils.py    # previous_day_window() + should_run()
  __main__.py      # CLI + config + JSON logging setup
tests/
  test_time_utils.py
  test_digest.py
accounts.json      # curated accounts: [{handle, user_id}, ...]
prompts/system.md  # Claude system prompt (added in Phase 2)
.github/workflows/daily-digest.yml  # cron + if:failure alert (added in Phase 3)
```

## Rules
- `time_utils` functions take `now_utc` as a parameter — never call `datetime.now()` inside them.
- Never request `expansions=author_id` for main tweets — handles come from `accounts.json` (cost control).
- A single account's fetch failure must never abort the run.
- The summarizer's JSON output is untrusted — validate against the `Digest` schema before rendering (Phase 2).
- Sync `httpx` only. No `pytest-asyncio`.
- `digest.py` is one deep module on purpose. Don't split it.
- Failure alerts live in the workflow (`if: failure()` → `SLACK_FAILURE_WEBHOOK`), never inside `digest.py`.

## Definition of Done
- Tests written before implementation (red/green/refactor cycle)
- Types pass
- Tests pass
- No new linting errors
- DB migrations generated if models changed
- No `TODO` or `FIXME` left without a linked issue
- Works locally end-to-end before pushing

## Common Gotchas
- DST: ET ↔ UTC offset changes twice a year. Always use `zoneinfo`, never a fixed offset.
- The quadruple-cron at 11:17/12:17/13:17/14:17 UTC + the wide 7am-11am ET delivery window + the Slack-history idempotency check are a *system* that survives GH Actions routinely delaying scheduled fires by 1-4 hours. Don't "fix" any one piece without understanding what the others depend on. Narrowing the window or dropping a cron slot will silently miss days.
- Long X posts: the real text lives in `note_tweet.text`, not `text` (Phase 2 normalization).
- Quoted-tweet source can be missing from `includes.tweets` (deleted/suspended) — render the parent without the quote, not as a hard fail.
- Claude `stop_reason="max_tokens"` is not a parse failure — it's a "raise the max_tokens constant" signal (Phase 2 synthesizer).

## Core Principles
- **Simplicity First**: Make every change as simple as possible. Impact minimal code.
- **No Laziness**: Find root causes. No temporary fixes. Senior developer standards.
- **Minimal Impact**: Changes should only touch what's necessary. Avoid introducing bugs.
- **Own Your Mistakes**: When wrong, say so, fix it, add a lesson. No excuses.
- **Context Is King**: Read existing code before writing new code. Match patterns already in the repo.

# Daily X (Twitter) Digest

A little robot that reads X for you so you don't have to.

Every morning around 7 AM Eastern, it reads everything yesterday's chosen X accounts posted, asks Claude (an AI) to write a short summary, and posts that summary to a Slack channel. You stop scrolling X. The robot does it for you. You read the part worth reading — usually under 90 seconds.

---

## What one day's digest looks like

```
📰 X Daily Digest — 2026-05-22 — FTC settles "active listening" ad fraud case

"Active listening" ad targeting was always a vendor lie
FTC fined Cox Media Group ~$1M for selling a microphone-based
ad-targeting AI that didn't exist — fraud against advertisers, not
surveillance of consumers. @simonw called this in Sep 2024.

Quick hits
• @karpathy: short note on new model release
• @gregisenberg: founder advice thread worth a read

3/3 accounts
```

Each handle is clickable — tap it to jump to the original post on X if you want the full thread.

---

## Why it exists

X is good for catching signal, bad for spending an hour scrolling. This reverses the ratio: a few minutes of reading, no scrolling.

---

## How it works (in four steps)

Every morning, four things happen automatically:

1. **It fetches yesterday's posts** from the X accounts you chose, ignoring replies and retweets.
2. **It cleans them up** — drops duplicates, expands shortened links, fits everything into a single page of text.
3. **It asks Claude to summarize** — Claude reads everything and writes a short editorial digest with themes and links back to the original posts.
4. **It posts to Slack** — the digest lands in your chosen channel, ready to read with coffee.

If anything breaks, it sends a separate short note to a "failure alerts" channel so you know to check on it. Otherwise you'll never need to touch it.

---

## Setting up your own copy

This part assumes you can run a few commands in a terminal. If `pip`, `bash`, and "environment variables" mean nothing to you, the rest of this page won't be useful — but the result is a fully automatic morning digest, so it's worth finding a friend who can help.

### What you need first

- A free [X Developer account](https://developer.x.com) (for reading tweets).
- A free [Anthropic account](https://console.anthropic.com) (for the Claude summaries — pay-as-you-go, around $6–15/month at this scale).
- A Slack workspace you control (for the daily post).
- Python 3.11 or newer on your computer.
- A free GitHub account (GitHub runs the daily timer for you).

### Step 1 — Clone this repo

```bash
git clone https://github.com/<your-username>/<your-fork>.git
cd <your-fork>
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

### Step 2 — Get the secret keys

In the X developer dashboard, create a project and copy the **Bearer Token**. It's a long string starting with `AAAA…`.

In the Anthropic console, create a key. It starts with `sk-ant-…`.

In Slack, you need to make a small app:

1. Go to [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → **From a manifest** → pick your workspace.
2. Paste this YAML into the manifest box:

   ```yaml
   display_information:
     name: X Daily Digest
   features:
     bot_user:
       display_name: X Daily Digest
       always_online: true
   oauth_config:
     scopes:
       bot:
         - chat:write
         - channels:history
         - groups:history
         - incoming-webhook
   settings:
     org_deploy_enabled: false
     socket_mode_enabled: false
     token_rotation_enabled: false
   ```

3. **Create** → **Install to Workspace** → pick the channel where you want failure alerts → **Allow**.
4. Now grab two things from the app's settings:
   - **OAuth & Permissions** page → copy the **Bot User OAuth Token** (starts with `xoxb-…`).
   - **Incoming Webhooks** page → copy the listed **Webhook URL** (starts with `https://hooks.slack.com/…`).
5. Invite the bot to whichever channel you want the daily digest posted in: type `/invite @X Daily Digest` in that channel.

Get the channel ID for the digest channel: right-click the channel name → **View channel details** → scroll to the bottom — it's a string like `C0123ABC456`.

### Step 3 — Put the keys in a local file

Copy the example file and fill in your real values:

```bash
cp .env.example .env
```

Then edit `.env` with any text editor. You'll fill in five values:

| Variable | Where it came from |
|---|---|
| `X_BEARER_TOKEN` | X developer dashboard |
| `ANTHROPIC_API_KEY` | Anthropic console |
| `SLACK_BOT_TOKEN` | Slack app → OAuth & Permissions |
| `SLACK_CHANNEL_ID` | The digest channel's ID (the `C…` string) |

Keep this file. Never commit it. (It's already in `.gitignore`.)

### Step 4 — Choose which X accounts to follow

Edit `accounts.json` — it's a list of X handles you want the daily digest to cover. Start with 5–10; you can grow to 50.

Then run a one-time command to look up X's internal ID for each handle:

```bash
export $(grep -v '^#' .env | xargs)
.venv/bin/python -m x_digest resolve-ids
```

If any account is spelled wrong or no longer exists, it'll be skipped with a warning — you can fix and re-run.

### Step 5 — Try it locally first

A "dry run" exercises everything — it actually reads X and asks Claude for a summary, but only prints the result to your screen. No Slack post, no risk of spam.

```bash
.venv/bin/python -m x_digest --force --dry-run
```

You should see a few log lines, then a JSON summary of yesterday's posts. Costs about a penny in Claude tokens.

When that looks good, run it for real:

```bash
.venv/bin/python -m x_digest --force
```

The digest should appear in your Slack channel within ~15 seconds.

### Step 6 — Hand it off to GitHub for the daily timer

So far you've been running this manually. The point is that GitHub does it for you every morning.

1. Push this repo to GitHub:
   ```bash
   git remote add origin <your-private-repo-url>
   git push -u origin master
   ```

2. In the GitHub repo's **Settings → Secrets and variables → Actions**, add five secrets:

   | Secret name | Value |
   |---|---|
   | `X_BEARER_TOKEN` | from Step 2 |
   | `ANTHROPIC_API_KEY` | from Step 2 |
   | `SLACK_BOT_TOKEN` | from Step 2 |
   | `SLACK_CHANNEL_ID` | from Step 2 |
   | `SLACK_FAILURE_WEBHOOK` | from Step 2 |

3. Go to **Actions → X Daily Digest → Run workflow** during 7–8 AM ET. The digest should appear in Slack. (Outside that window the workflow exits without posting — the daily schedule will catch it the next morning.)

That's it. From here on, every morning at 7 AM ET the timer fires and the digest lands in Slack.

---

## When things go wrong

The robot is designed to fail loudly:

- If anything breaks (X is down, Anthropic rejects the key, Slack is unreachable), an alert message lands in your "failure alerts" channel with a link to the failing run.
- If you run it twice in the same day it won't double-post — it checks Slack first to see if today's digest already exists.

Common things to check first when the failure alert fires:

| Alert hint | Likely cause |
|---|---|
| `auth rejected` | One of your tokens expired or was changed. |
| `accounts failed` | X is rate-limiting or a chunk of handles got suspended. Look at the run log for which ones. |
| `Slack 404` or `channel_not_found` | The bot isn't a member of the digest channel. Re-invite it. |
| `Claude hit max_tokens` | One day's volume was unusually huge. Rarely happens; if it does, edit `DEFAULT_CLAUDE_MAX_TOKENS` upward in `src/x_digest/digest.py`. |

---

## For developers — quick reference

```bash
.venv/bin/pytest                  # unit tests
.venv/bin/mypy --strict src/      # type check
.venv/bin/ruff check src/ tests/  # lint
.venv/bin/python -m x_digest --force --dry-run     # full pipeline, no Slack post
.venv/bin/python -m x_digest --force --dry-run --account <handle>   # one-account preview
.venv/bin/python -m x_digest resolve-ids           # populate user_ids from handles
```

Three source files: `digest.py` (the pipeline), `time_utils.py` (date math), `__main__.py` (CLI + logging + config). Strict TDD; one deep module by design. See `.claude/CLAUDE.md` for project rules.

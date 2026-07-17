# Healthie Help Bot

A Slack bot that answers customer questions using only content from the
Healthie Help Center (help.gethealthie.com). If it cannot find a direct
answer in the help docs, it stays silent rather than guessing.

---

## Front end (what people see in Slack)

### 1. What the app does
The bot watches the Slack channels it has been invited to. When someone asks
a question it can confidently answer from the Help Center, it replies in a
thread with a short, plain-text answer and links to the supporting help
article(s) â€” usually 1 link, up to 3 when several articles are each highly
relevant. It never invents information: answers are written strictly from
the article text, and a strict relevance check runs before every reply.

### 2. How it gets triggered
- **Automatic:** any human message of 10+ characters in a channel the bot is
  in. The bot searches the Help Center, checks relevance, and replies only
  if an article directly answers the question. Otherwise it stays silent.
- **Direct mention:** start a message with `@Healthie Help Bot` followed by an
  actual question and the bot always replies â€” with the answer if it has
  one; with links to the closest related articles (stating explicitly that
  they do not directly answer the exact question) when the docs are on-topic
  but not a direct match; or a polite "I don't have an article for that"
  pointing to the Help Center. Mentions work even in muted channels. A bare
  mention (or mention with no real question) gets no response.
- It ignores messages from other bots, message edits, join/leave events,
  and channels listed in the `IGNORED_CHANNELS` env var.

### 3. How muting works
Every channel has its own mode, toggled by anyone in the channel:
- **Auto (default):** the bot may answer any qualifying message.
- **Mention-only (muted):** the bot is silent unless explicitly
  `@Healthie Help Bot` mentioned. Muting one channel never affects another.

### 4. What it has access to
- **Slack:** only channels it has been invited to (bot scopes: `chat:write`,
  `channels:history`, `groups:history`, `users:read`). It cannot read DMs or
  channels it isn't in. Removing it from a channel cuts off access entirely.
- **Healthie Help Center:** public help articles only, fetched over HTTPS.
- **Google Sheets:** one spreadsheet (the activity log below), via a
  dedicated Google service account with Editor access to that sheet only.
- **Anthropic API:** sends the customer's question plus the fetched help
  article text for relevance-checking and answer-writing.
- It has **no access** to Healthie customer accounts, PHI, billing systems,
  HubSpot/SFDC, or any internal database.

### 5. Why it's safe
- **Strict answer gate:** a separate model call checks whether the fetched
  articles DIRECTLY answer the exact question. Related-but-not-exact matches
  are skipped. Questions involving custom pricing, contracts, PHI, or
  account-specific data are always skipped.
- **Grounded answers only:** the answer model is instructed to use ONLY the
  provided article text and never invent information; every answer links its
  sources so the reader can verify.
- **Fails silent:** errors are never posted into customer channels. If
  something breaks mid-answer on a mention, the bot posts a graceful "I ran
  into a technical issue" note instead of an error trace.
- **Scoped credentials:** the Slack app uses Socket Mode (no public inbound
  endpoint), the Google service account can only edit the one log sheet, and
  all secrets live in Railway environment variables â€” none are in this repo.

### 6. How to mute it (per channel)
In the channel, type:

    @Healthie Help Bot mute

(`pause`, `stop`, and `quiet` also work.) The bot confirms in-thread and
stops auto-answering that channel. Mentions still get responses.

### 7. How to unmute it (per channel)
In the channel, type:

    @Healthie Help Bot unmute

(`resume` and `start` also work.) The bot confirms and resumes auto answers.
`@Healthie Help Bot status` shows the channel's current mode at any time.

### 8. Where data is recorded (for later review)
Everything lands in one Google Sheet:
https://docs.google.com/spreadsheets/d/1vcNM8R0E0mfoxhfjRv9PkA-kRkutUOiKYP8vx6GQ6VA/edit

- **Tab 1 (miss log):** one row per processed question â€” timestamp, channel,
  user, question text, and outcome (`answered`, `related_links` when only
  close-but-not-direct articles were offered, `gate_skip` with the reason,
  `no_search_results`, or `error:<type>`). Gate skips and related-links rows
  are the content backlog: questions customers asked that the Help Center
  can't answer yet.
- **`channel_modes` tab:** which channels are muted, when, and by whom.
- If the Sheet is ever unreachable, rows are printed to the Railway service
  logs instead so nothing is lost silently.

---

## Backend infrastructure

### 1. Where it lives in GitHub
https://github.com/vmarthi17/customerhelpbot (public repo)
- `healthie_help_bot.py` â€” the entire bot (~300 lines)
- `Dockerfile` / `requirements.txt` â€” deployment
- Changes go through pull requests to `main`; merging to `main` triggers a
  redeploy automatically.

### 2. Where it runs in Railway
Railway project **customer-help-bot**, environment **production**, service
**customerhelpbot**, deployed from the GitHub repo above (Dockerfile
auto-detected; auto-deploys on push to `main`). All configuration is in the
service's Variables tab:

| Variable | Purpose |
|---|---|
| `SLACK_BOT_TOKEN` | Bot identity (xoxb-) for reading/posting messages |
| `SLACK_APP_TOKEN` | Socket Mode connection (xapp-) |
| `ANTHROPIC_API_KEY` | Claude API access |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Service account key for the log Sheet |
| `IGNORED_CHANNELS` | Optional comma-separated channel blocklist |
| `WATCHED_CHANNELS` | Optional allowlist; unset = all joined channels |

### 3. What Anthropic key/models it uses
The bot authenticates with the `ANTHROPIC_API_KEY` set in Railway (manage or
rotate it in the Anthropic Console; it appears nowhere in this repo). Two
models per answered question:
- **claude-haiku-4-5** ($1/$5 per MTok) â€” turns the question into search
  keywords, re-ranks search hits by title, and runs the strict relevance gate
- **claude-sonnet-4-6** ($3/$15 per MTok) â€” writes the customer-facing answer

Typical cost: ~$0.004 per skipped question, ~$0.02 per answered question â€”
roughly $8â€“20/month at 1,000 processed messages.

---

## Risks at volume

Things to address before scaling this beyond a pilot:

1. **API spend scales with chatter, not just questions.** Every human
   message of 10+ chars in a watched channel costs ~$0.004 (search + gate)
   even when skipped. A busy channel of internal chatter is pure gate-skip
   cost. Mitigations: mute chatty channels, use `IGNORED_CHANNELS`, or add a
   cheap pre-filter (e.g., only process messages containing a question mark
   or question words).
2. **Single instance, no queue.** Socket Mode + one Railway container means
   one process handles everything sequentially-ish. A burst of messages
   means slow replies; a crash mid-question drops it (Slack does retry
   deliveries, but there's no dedup â€” a slow response can also cause
   double-processing). Running two replicas would double-answer every
   question, so this cannot be horizontally scaled as-is.
3. **Help Center scraping is fragile.** Article search and content come from
   parsing help.gethealthie.com HTML (a regex on search results + an HTML id
   for article bodies). Any redesign of the help site silently breaks
   retrieval and the bot goes quiet. At volume, this deserves monitoring
   (alert if answer-rate drops to zero) or a proper content API/index.
4. **Google Sheets is not a database.** Sheets API write quotas (~60
   writes/min per user) will throttle logging under load, and concurrent
   writes to `channel_modes` aren't locked. Fine for pilot volume; at scale,
   move logging to a real datastore and keep the Sheet as a synced view.
5. **Rate limits.** Anthropic API and Slack `chat.postMessage` both have
   rate limits. There's no client-side rate limiting or backoff beyond SDK
   defaults, so a message flood degrades into errors (which fail silent â€”
   see the miss log for `error:` rows).
6. **Anyone can mute/unmute.** Any channel member can toggle the bot. Low
   stakes internally; at customer scale you'd want the commands restricted
   to an allowlist of admin user IDs.
7. **Wrong-answer risk grows with volume.** The gate is strict, but at
   thousands of answers a small error rate becomes visible customer-facing
   mistakes. Review the miss log's `answered` rows periodically, and keep
   the gate strict rather than chasing answer-rate.
8. **Data governance.** Customer questions (which may contain names or
   account details customers volunteer) flow to the Anthropic API and are
   logged in the Google Sheet. Both are access-controlled, but at volume
   this needs a documented retention policy and a scrub for anything
   sensitive customers paste in.

---

## Run locally (development)

    pip install -r requirements.txt
    export SLACK_BOT_TOKEN=xoxb-...
    export SLACK_APP_TOKEN=xapp-...
    export ANTHROPIC_API_KEY=sk-ant-...
    export GOOGLE_SERVICE_ACCOUNT_JSON='{...}'   # service account key JSON
    export IGNORED_CHANNELS=C0YYYYYYY            # optional blocklist
    python healthie_help_bot.py

## Google Sheets setup (one time, already done for prod)
1. Google Cloud console â†’ create a service account, enable the Google
   Sheets API
2. Create a JSON key; put its contents in `GOOGLE_SERVICE_ACCOUNT_JSON`
3. Share the Sheet with the service account's `client_email` as Editor
4. Override the target sheet with `MISS_LOG_SHEET_ID` if needed

## Slack app requirements
- Socket Mode ON (app token scope: `connections:write` only)
- Bot scopes: `chat:write`, `channels:history`, `groups:history`, `users:read`
- Bot events: `message.channels`, `message.groups`
- Invite the bot to each channel it should watch

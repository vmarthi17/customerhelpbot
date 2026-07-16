# Healthie Help Bot

Answers customer questions in Slack using only help.gethealthie.com.

## Run locally (pilot)
    pip install -r requirements.txt
    export SLACK_BOT_TOKEN=xoxb-...
    export SLACK_APP_TOKEN=xapp-...
    export ANTHROPIC_API_KEY=sk-ant-...
    export WATCHED_CHANNELS=C0XXXXXXX   # optional allowlist; empty/unset = all joined channels
    export IGNORED_CHANNELS=C0YYYYYYY   # optional blocklist; always wins over membership/allowlist
    export GOOGLE_SERVICE_ACCOUNT_JSON='{...}'   # service account key JSON (miss log -> Google Sheets)
    python healthie_help_bot.py

## Miss log (Google Sheets)
Gate skips, errors, and answered questions are appended to a Google Sheet
(content backlog for help docs):
https://docs.google.com/spreadsheets/d/1vcNM8R0E0mfoxhfjRv9PkA-kRkutUOiKYP8vx6GQ6VA/edit

Setup (one time):
1. Google Cloud console -> create a service account, enable the Google Sheets API
2. Create a JSON key; put its contents in GOOGLE_SERVICE_ACCOUNT_JSON
3. Share the sheet with the service account's client_email as Editor

Override the target sheet with MISS_LOG_SHEET_ID. If the Sheet is unreachable,
the row is printed to the service logs instead.

## Behavior
The bot silently skips messages it cannot answer from the help center.
Exception: if a message starts with @healthie-help (or a direct bot mention),
the bot always replies in-thread — with the answer if it has one, otherwise
with a polite note pointing to the help center.

## Deploy (Railway / Render / Fly)
1. Push this folder to a GitHub repo
2. New project -> deploy from repo (Dockerfile auto-detected)
3. Set the same five env vars in the dashboard
4. Health check path: /healthz on port 8080
5. Enable auto-restart (default on Railway/Render)

## Files
- healthie_help_bot.py — the entire bot

## Slack app requirements
Socket Mode ON (app token scope: connections:write only)
Bot scopes: chat:write, channels:history, groups:history, users:read
Bot events: message.channels, message.groups
Invite the bot to each watched channel.

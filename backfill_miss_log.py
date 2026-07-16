"""One-time cleanup of the miss-log sheet: convert UTC ISO timestamps to
Eastern datetimes and resolve Slack channel/user IDs to names.

Idempotent and safe to re-run: timestamps and ID resolution are handled
independently, so if the Slack name scopes (users:read / channels:read)
aren't granted yet, a later re-run still upgrades the remaining raw IDs
without touching already-converted timestamps.

Needs the same env vars as the bot: SLACK_BOT_TOKEN, GOOGLE_SERVICE_ACCOUNT_JSON
(and MISS_LOG_SHEET_ID if not using the default).

Usage: python backfill_miss_log.py
"""
import re
from datetime import datetime

from healthie_help_bot import (
    EASTERN, MISS_LOG_HEADER, _sheet, _sheet_text, channel_name, user_name,
)

CHANNEL_ID_RE = re.compile(r"^[CGD][A-Z0-9]{7,}$")
USER_ID_RE = re.compile(r"^[UW][A-Z0-9]{7,}$")


def convert_ts(ts: str) -> str:
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return ts  # already converted or not a data row
    if dt.tzinfo is None:
        return ts
    return dt.astimezone(EASTERN).strftime("%Y-%m-%d %H:%M:%S")


def convert_row(row: list[str]) -> list[str]:
    ts, channel, user, question, reason = (row + [""] * 5)[:5]
    return [convert_ts(ts),
            channel_name(channel) if CHANNEL_ID_RE.match(channel) else channel,
            user_name(user) if USER_ID_RE.match(user) else user,
            _sheet_text(question), reason] + row[5:]  # keep feedback etc.


def main():
    ws = _sheet()
    values = ws.get_all_values()
    if not values:
        print("sheet is empty; nothing to do")
        return
    rows = [MISS_LOG_HEADER] + [convert_row(r) for r in values[1:]]
    ws.update(values=rows, range_name="A1", value_input_option="USER_ENTERED")
    print(f"rewrote {len(rows) - 1} data rows (+ header)")


if __name__ == "__main__":
    main()

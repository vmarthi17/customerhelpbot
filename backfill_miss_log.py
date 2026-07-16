"""One-time cleanup of the miss-log sheet: convert UTC ISO timestamps to
Eastern datetimes and resolve Slack channel/user IDs to names.

Idempotent: rows already converted (or otherwise unparseable) are left as-is.
Needs the same env vars as the bot: SLACK_BOT_TOKEN, GOOGLE_SERVICE_ACCOUNT_JSON
(and MISS_LOG_SHEET_ID if not using the default).

Usage: python backfill_miss_log.py
"""
from datetime import datetime

from healthie_help_bot import (
    EASTERN, MISS_LOG_HEADER, _sheet, _sheet_text, channel_name, user_name,
)


def convert_row(row: list[str]) -> list[str]:
    ts, channel, user, question, reason = (row + [""] * 5)[:5]
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return row  # already converted or not a data row
    if dt.tzinfo is None:
        return row
    return [dt.astimezone(EASTERN).strftime("%Y-%m-%d %H:%M:%S"),
            channel_name(channel), user_name(user), _sheet_text(question),
            reason] + row[5:]  # preserve any feedback already entered


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

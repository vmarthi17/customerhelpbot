import json
import os
import re
from datetime import datetime, timezone

import gspread
import requests
from bs4 import BeautifulSoup
from anthropic import Anthropic
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

HELP_BASE = "https://help.gethealthie.com"
MAX_ARTICLES = 3
ANSWER_MODEL = "claude-sonnet-4-6"
GATE_MODEL = "claude-haiku-4-5-20251001"   # cheap yes/no gate
WATCHED_CHANNELS = set(
    c for c in os.environ.get("WATCHED_CHANNELS", "").split(",") if c
)  # empty = all channels the bot is in
IGNORED_CHANNELS = set(
    c.strip() for c in os.environ.get("IGNORED_CHANNELS", "").split(",") if c.strip()
)  # blocklist: never respond in these, even if the bot is a member
MISS_LOG_SHEET_ID = os.environ.get(
    "MISS_LOG_SHEET_ID", "1vcNM8R0E0mfoxhfjRv9PkA-kRkutUOiKYP8vx6GQ6VA"
)
MISS_LOG_HEADER = ["timestamp_utc", "channel", "user", "question", "reason"]

MODES_SHEET_NAME = "channel_modes"
MODES_HEADER = ["channel", "mode", "updated_utc", "updated_by"]
MUTE_COMMANDS = {"mute", "pause", "stop", "quiet"}
UNMUTE_COMMANDS = {"unmute", "resume", "start"}
STATUS_COMMANDS = {"status", "mode"}

MENTION_RE = re.compile(r"^(<@[A-Z0-9]+>|@healthie-help)\s*")

NO_ANSWER_REPLY = (
    "I do not have a help center article that directly answers this, "
    "so I will leave it for the team rather than guess.\n"
    "You can also browse the help center here:\n"
    "<https://help.gethealthie.com|Healthie Help Center>"
)
ASK_A_QUESTION_REPLY = (
    "Hello. Ask me a question about using Healthie and I will answer it "
    "from the help center if I can."
)
ERROR_REPLY = (
    "I ran into a technical issue while looking that up. "
    "Please try again in a few minutes, or the team can follow up here."
)
MUTED_REPLY = (
    "Understood. I will stop answering automatically in this channel.\n"
    "Mention me with a question any time and I will still respond. "
    "Say \"@healthie-help unmute\" to turn automatic answers back on."
)
UNMUTED_REPLY = (
    "Automatic answers are back on for this channel.\n"
    "Say \"@healthie-help mute\" any time to switch me to mention-only."
)
STATUS_MUTED_REPLY = (
    "This channel is set to mention-only. I answer here only when mentioned.\n"
    "Say \"@healthie-help unmute\" to turn automatic answers back on."
)
STATUS_AUTO_REPLY = (
    "Automatic answers are on for this channel.\n"
    "Say \"@healthie-help mute\" to switch me to mention-only."
)

app = App(token=os.environ["SLACK_BOT_TOKEN"])
claude = Anthropic()

QUERY_PROMPT = """You turn a customer support message into search queries for a
help center search engine that matches keywords literally (filler words hurt it).

Extract 1-3 short queries, 1-4 words each, focused on the product feature or
noun being asked about. Drop greetings, politeness, and filler.
Reply with one query per line and nothing else."""

GATE_PROMPT = """You are a strict relevance gate for a customer support bot.
Decide whether the help center articles below DIRECTLY and COMPLETELY answer the
customer's question.

Reply ANSWER only if the articles explicitly and directly address the exact
question asked — one article alone, or a few articles that each directly cover
part of the question.
Reply SKIP if:
- The articles are merely related or adjacent to the topic
- Answering would require inference, piecing together loose fragments, or
  general knowledge beyond what the articles state
- The message is not actually a support question (greeting, internal chatter,
  opinion, feedback, sales/pricing negotiation, account-specific issue)
- The question involves custom pricing, contracts, PHI, or account data

Respond with one word - ANSWER or SKIP - followed by a one-line reason."""

STYLE_PROMPT = """You are Healthie Help, answering a customer question in Slack using
ONLY the help center articles provided. Never invent information.

Style rules:
- Professional but warm tone. Avoid jargon; use common words.
- Short sentences, 15-20 words average. Active voice. Important info first.
- No exclamation points. Minimal contractions. Direct and straightforward.
- Plain text with line breaks only. No markdown headers, no bold, no bullets
  unless listing 3+ items.
- End with the help doc links that directly support your answer, each on its
  own line, formatted as <URL|Article Title>. Usually this is 1 link; include
  2 or 3 only when each is highly relevant to the question on its own. Never
  pad the list with merely related articles."""


def extract_queries(question: str) -> list[str]:
    """Turn a conversational message into 1-3 keyword queries the help center
    search can actually match ("can you tell me about e-labs?" -> "e-labs")."""
    resp = claude.messages.create(
        model=GATE_MODEL, max_tokens=50, temperature=0, system=QUERY_PROMPT,
        messages=[{"role": "user", "content": question}],
    )
    lines = [l.strip() for l in resp.content[0].text.splitlines() if l.strip()]
    return lines[:3] or [question]


def search_help_center(query: str) -> list[str]:
    r = requests.get(f"{HELP_BASE}/search", params={"query": query},
                     headers={"User-Agent": "HealthieHelpBot/2.0"}, timeout=10)
    slugs = list(dict.fromkeys(re.findall(r'href="/article/([^"]+)"', r.text)))
    return [f"{HELP_BASE}/article/{s}" for s in slugs]


def search_articles(question: str) -> list[str]:
    """Search once per extracted keyword query; merge, dedupe, cap.
    Falls back to the raw message if the keyword queries find nothing."""
    urls = []
    for q in extract_queries(question):
        urls.extend(search_help_center(q))
    if not urls:
        urls = search_help_center(question)
    return list(dict.fromkeys(urls))[:MAX_ARTICLES]


def fetch_article(url: str) -> dict:
    r = requests.get(url, headers={"User-Agent": "HealthieHelpBot/2.0"}, timeout=10)
    soup = BeautifulSoup(r.text, "html.parser")
    title = soup.find("h1")
    body = soup.find(id="fullArticle")
    return {"title": title.get_text(strip=True) if title else url,
            "text": body.get_text(separator="\n", strip=True)[:12000] if body else "",
            "url": url}


def build_docs(articles: list[dict]) -> str:
    return "\n\n".join(
        f"=== ARTICLE: {a['title']} ({a['url']}) ===\n{a['text']}" for a in articles
    )


def gate(question: str, docs: str) -> tuple[bool, str]:
    """Strict exact-match gate. Deterministic. Returns (verdict, one-line reason)."""
    resp = claude.messages.create(
        model=GATE_MODEL, max_tokens=60, temperature=0, system=GATE_PROMPT,
        messages=[{"role": "user",
                   "content": f"ARTICLES:\n{docs}\n\nCUSTOMER QUESTION: {question}"}],
    )
    text = resp.content[0].text.strip()
    return text.upper().startswith("ANSWER"), text


def answer(question: str, docs: str) -> str:
    resp = claude.messages.create(
        model=ANSWER_MODEL, max_tokens=600, temperature=0, system=STYLE_PROMPT,
        messages=[{"role": "user",
                   "content": f"ARTICLES:\n{docs}\n\nCUSTOMER QUESTION: {question}"}],
    )
    return resp.content[0].text


_spreadsheet = None
_worksheet = None
_modes_worksheet = None
muted_channels = set()  # channels in mention-only mode; loaded from the Sheet


def _book():
    """Lazy-init the Google Spreadsheet handle."""
    global _spreadsheet
    if _spreadsheet is None:
        creds = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
        _spreadsheet = gspread.service_account_from_dict(creds).open_by_key(
            MISS_LOG_SHEET_ID)
    return _spreadsheet


def _sheet():
    """Miss-log worksheet (first tab); adds the header row on first use."""
    global _worksheet
    if _worksheet is None:
        ws = _book().sheet1
        if not ws.get_values("A1:E1"):
            ws.append_row(MISS_LOG_HEADER, value_input_option="RAW")
        _worksheet = ws
    return _worksheet


def _modes_sheet():
    """channel_modes worksheet; created with a header on first use."""
    global _modes_worksheet
    if _modes_worksheet is None:
        book = _book()
        try:
            ws = book.worksheet(MODES_SHEET_NAME)
        except gspread.WorksheetNotFound:
            ws = book.add_worksheet(MODES_SHEET_NAME, rows=100, cols=4)
            ws.append_row(MODES_HEADER, value_input_option="RAW")
        _modes_worksheet = ws
    return _modes_worksheet


def load_channel_modes():
    """Populate muted_channels from the Sheet at startup."""
    try:
        for row in _modes_sheet().get_all_values()[1:]:
            if len(row) >= 2 and row[1] == "muted":
                muted_channels.add(row[0])
        print(f"loaded channel modes: {len(muted_channels)} muted", flush=True)
    except Exception as e:
        print(f"could not load channel modes ({type(e).__name__}: {e}); "
              "starting with all channels on auto", flush=True)


def set_channel_mode(channel: str, muted: bool, user: str):
    """Flip a channel between auto and mention-only; persist to the Sheet."""
    if muted:
        muted_channels.add(channel)
    else:
        muted_channels.discard(channel)
    row = [channel, "muted" if muted else "auto",
           datetime.now(timezone.utc).isoformat(), user]
    try:
        ws = _modes_sheet()
        cell = ws.find(channel, in_column=1)
        if cell:
            ws.update(values=[row], range_name=f"A{cell.row}:D{cell.row}")
        else:
            ws.append_row(row, value_input_option="RAW")
    except Exception as e:
        # in-memory state still applies until the next restart
        print(f"mode persist failed ({type(e).__name__}: {e}); row: {row}",
              flush=True)


def log_miss(channel: str, user: str, question: str, reason: str):
    row = [datetime.now(timezone.utc).isoformat(), channel, user, question, reason]
    try:
        _sheet().append_row(row, value_input_option="RAW")
    except Exception as e:
        print(f"sheet log failed ({type(e).__name__}: {e}); dropped row: {row}",
              flush=True)


@app.event("message")
def handle_message(event, say, client):
    print(f"EVENT ch={event.get('channel')} user={event.get('user')} "
          f"bot={event.get('bot_id')} subtype={event.get('subtype')} "
          f"text={event.get('text', '')[:60]!r}", flush=True)
    # Spec step 2: human senders only; also ignore edits/joins/thread broadcasts
    if event.get("bot_id") or event.get("subtype"):
        return
    if event["channel"] in IGNORED_CHANNELS:
        return
    if WATCHED_CHANNELS and event["channel"] not in WATCHED_CHANNELS:
        return

    # Spec step 3: full text is the question; strip optional prefix/mention.
    # A leading @healthie-help / bot mention means the user addressed us
    # directly, so we always reply — even if only to say we have nothing.
    raw = event.get("text", "")
    mentioned = bool(MENTION_RE.match(raw))
    question = MENTION_RE.sub("", raw).strip()
    reply_ts = event.get("thread_ts", event["ts"])

    # Mode commands: "@healthie-help mute|unmute|status" — per channel
    if mentioned:
        command = question.lower().rstrip(".!")
        if command in MUTE_COMMANDS:
            set_channel_mode(event["channel"], True, event["user"])
            say(text=MUTED_REPLY, thread_ts=reply_ts)
            return
        if command in UNMUTE_COMMANDS:
            set_channel_mode(event["channel"], False, event["user"])
            say(text=UNMUTED_REPLY, thread_ts=reply_ts)
            return
        if command in STATUS_COMMANDS:
            muted = event["channel"] in muted_channels
            say(text=STATUS_MUTED_REPLY if muted else STATUS_AUTO_REPLY,
                thread_ts=reply_ts)
            return

    # Mention-only mode: stay silent for unmentioned messages in muted channels
    if not mentioned and event["channel"] in muted_channels:
        return

    if len(question) < 10:          # too short to be a real question
        if mentioned:
            say(text=ASK_A_QUESTION_REPLY, thread_ts=reply_ts)
        return

    try:
        urls = search_articles(question)
        articles = [a for a in (fetch_article(u) for u in urls) if a["text"]]

        # Spec step 5: strict confidence gate — silence unless direct match,
        # except when mentioned: then decline politely instead of silently.
        if not articles:
            ok, reason = False, "no_search_results"
        else:
            docs = build_docs(articles)
            ok, reason = gate(question, docs)
        if not ok:
            log_miss(event["channel"], event["user"], question,
                     f"gate_skip: {reason}" if articles else reason)
            if mentioned:
                say(text=NO_ANSWER_REPLY, thread_ts=reply_ts)
            return

        # Spec step 6: threaded reply with help doc link
        say(text=answer(question, docs), thread_ts=reply_ts)
        log_miss(event["channel"], event["user"], question, "answered")
    except Exception as e:
        # Spec step 7 spirit: never post raw errors into customer channels
        log_miss(event.get("channel", "?"), event.get("user", "?"),
                 question, f"error:{type(e).__name__}")
        if mentioned:
            say(text=ERROR_REPLY, thread_ts=reply_ts)


if __name__ == "__main__":
    load_channel_modes()
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()

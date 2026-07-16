import csv
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
MISS_LOG_SHEET_ID = os.environ.get(
    "MISS_LOG_SHEET_ID", "1vcNM8R0E0mfoxhfjRv9PkA-kRkutUOiKYP8vx6GQ6VA"
)
MISS_LOG_FALLBACK = "unanswered_questions.csv"  # used only if the Sheet is unreachable
MISS_LOG_HEADER = ["timestamp_utc", "channel", "user", "question", "reason"]

app = App(token=os.environ["SLACK_BOT_TOKEN"])
claude = Anthropic()

GATE_PROMPT = """You are a strict relevance gate for a customer support bot.
Decide whether the help center articles below DIRECTLY and COMPLETELY answer the
customer's question.

Reply ANSWER only if a single article explicitly addresses the exact question asked.
Reply SKIP if:
- The articles are merely related or adjacent to the topic
- Answering would require inference, combining fragments, or general knowledge
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
- End with the help doc link on its own line, formatted as <URL|Article Title>."""


def search_help_center(query: str) -> list[str]:
    r = requests.get(f"{HELP_BASE}/search", params={"query": query},
                     headers={"User-Agent": "HealthieHelpBot/2.0"}, timeout=10)
    slugs = list(dict.fromkeys(re.findall(r'href="/article/([^"]+)"', r.text)))
    return [f"{HELP_BASE}/article/{s}" for s in slugs[:MAX_ARTICLES]]


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


_worksheet = None


def _sheet():
    """Lazy-init the Google Sheet worksheet; adds the header row on first use."""
    global _worksheet
    if _worksheet is None:
        creds = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
        ws = gspread.service_account_from_dict(creds).open_by_key(
            MISS_LOG_SHEET_ID).sheet1
        if not ws.get_values("A1:E1"):
            ws.append_row(MISS_LOG_HEADER, value_input_option="RAW")
        _worksheet = ws
    return _worksheet


def log_miss(channel: str, user: str, question: str, reason: str):
    row = [datetime.now(timezone.utc).isoformat(), channel, user, question, reason]
    try:
        _sheet().append_row(row, value_input_option="RAW")
    except Exception as e:
        # never lose a miss: fall back to local CSV if the Sheet is unreachable
        print(f"sheet log failed ({type(e).__name__}: {e}); using CSV fallback",
              flush=True)
        new = not os.path.exists(MISS_LOG_FALLBACK)
        with open(MISS_LOG_FALLBACK, "a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(MISS_LOG_HEADER)
            w.writerow(row)


@app.event("message")
def handle_message(event, say, client):
    print(f"EVENT ch={event.get('channel')} user={event.get('user')} "
          f"bot={event.get('bot_id')} subtype={event.get('subtype')} "
          f"text={event.get('text', '')[:60]!r}", flush=True)
    # Spec step 2: human senders only; also ignore edits/joins/thread broadcasts
    if event.get("bot_id") or event.get("subtype"):
        return
    if WATCHED_CHANNELS and event["channel"] not in WATCHED_CHANNELS:
        return

    # Spec step 3: full text is the question; strip optional prefix/mention
    question = re.sub(r"^(<@[A-Z0-9]+>|@healthie-help)\s*", "",
                      event.get("text", "")).strip()
    if len(question) < 10:          # too short to be a real question
        return

    try:
        urls = search_help_center(question)
        articles = [a for a in (fetch_article(u) for u in urls) if a["text"]]
        if not articles:
            log_miss(event["channel"], event["user"], question, "no_search_results")
            return
        docs = build_docs(articles)

        # Spec step 5: strict confidence gate — silence unless direct match
        ok, reason = gate(question, docs)
        if not ok:
            log_miss(event["channel"], event["user"], question,
                     f"gate_skip: {reason}")
            return

        # Spec step 6: threaded reply with help doc link
        say(text=answer(question, docs),
            thread_ts=event.get("thread_ts", event["ts"]))
        log_miss(event["channel"], event["user"], question, "answered")
    except Exception as e:
        # Spec step 7 spirit: never post errors into customer channels
        log_miss(event.get("channel", "?"), event.get("user", "?"),
                 question, f"error:{type(e).__name__}")


if __name__ == "__main__":
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()

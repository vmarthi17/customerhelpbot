import json
import os
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import gspread
import requests
from bs4 import BeautifulSoup
from anthropic import Anthropic
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

HELP_BASE = "https://help.gethealthie.com"
MAX_ARTICLES = 3      # article bodies fetched and given to the gate/answer
MAX_CANDIDATES = 10   # search hits considered by the title re-rank
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
MISS_LOG_HEADER = ["timestamp_est", "channel", "user", "question", "reason",
                   "feedback"]  # feedback column is human-edited; never written

EASTERN = ZoneInfo("America/New_York")

MODES_SHEET_NAME = "channel_modes"
MODES_HEADER = ["channel", "mode", "updated_utc", "updated_by"]
MUTE_COMMANDS = {"mute", "pause", "stop", "quiet"}
UNMUTE_COMMANDS = {"unmute", "resume", "start"}
STATUS_COMMANDS = {"status", "mode"}

# Leading mention; group 2 is the mentioned user's ID (None for the literal)
MENTION_RE = re.compile(r"^(<@([A-Z0-9]+)>|@healthie-help)\s*")

NO_ANSWER_REPLY = (
    "I do not have a help center article that directly answers this, "
    "so I will leave it for the team rather than guess.\n"
    "You can also browse the help center here:\n"
    "<https://help.gethealthie.com|Healthie Help Center>"
)
RELATED_REPLY = (
    "I do not have a help center article that directly answers this, "
    "so I will leave it for the team rather than guess.\n"
    "These are the closest related articles. Please note they cover the "
    "same topic but do not directly answer your exact question:\n{links}"
)
ERROR_REPLY = (
    "I ran into a technical issue while looking that up. "
    "Please try again in a few minutes, or the team can follow up here."
)
MUTED_REPLY = (
    "Understood. I will stop answering automatically in this channel.\n"
    "Mention me with a question any time and I will still respond. "
    "Say \"{bot} unmute\" to turn automatic answers back on."
)
UNMUTED_REPLY = (
    "Automatic answers are back on for this channel.\n"
    "Say \"{bot} mute\" any time to switch me to mention-only."
)
STATUS_MUTED_REPLY = (
    "This channel is set to mention-only. I answer here only when mentioned.\n"
    "Say \"{bot} unmute\" to turn automatic answers back on."
)
STATUS_AUTO_REPLY = (
    "Automatic answers are on for this channel.\n"
    "Say \"{bot} mute\" to switch me to mention-only."
)

app = App(token=os.environ["SLACK_BOT_TOKEN"])
claude = Anthropic()

_bot_user_id = None


def bot_user_id() -> str:
    """The bot's own Slack user ID, cached; empty string if the lookup fails."""
    global _bot_user_id
    if _bot_user_id is None:
        try:
            _bot_user_id = app.client.auth_test()["user_id"]
        except Exception:
            return ""
    return _bot_user_id


def bot_mention() -> str:
    """The bot's real Slack mention (renders as its display name)."""
    uid = bot_user_id()
    return f"<@{uid}>" if uid else "@Healthie Help Bot"


_channel_names: dict[str, str] = {}
_user_names: dict[str, str] = {}


def channel_name(channel_id: str) -> str:
    """Human-readable channel name ("#support"), cached; falls back to the
    raw ID if the lookup fails (e.g. missing channels:read scope)."""
    if channel_id not in _channel_names:
        try:
            info = app.client.conversations_info(channel=channel_id)
            _channel_names[channel_id] = f"#{info['channel']['name']}"
        except Exception:
            return channel_id
    return _channel_names[channel_id]


def user_name(user_id: str) -> str:
    """Human-readable user name, cached; falls back to the raw ID if the
    lookup fails (e.g. missing users:read scope)."""
    if user_id not in _user_names:
        try:
            u = app.client.users_info(user=user_id)["user"]
            name = u.get("profile", {}).get("display_name") or u.get(
                "real_name") or u.get("name")
            if not name:
                return user_id
            _user_names[user_id] = name
        except Exception:
            return user_id
    return _user_names[user_id]

QUERY_PROMPT = """You turn a customer support message into search queries for a
help center search engine that matches keywords literally (filler words hurt it).

Extract 1-3 short queries, 1-4 words each, focused on the product feature or
noun being asked about. Drop greetings, politeness, and filler.
Reply with one query per line and nothing else."""

RERANK_PROMPT = """You pick the help center articles most likely to answer a
customer support question, judging only by article titles.

From the numbered list, reply with the numbers of up to 3 titles most relevant
to the question, most relevant first, comma-separated (e.g. "4, 1"). Reply with
numbers only."""

GATE_PROMPT = """You are the relevance gate for a customer support bot. Work
through these steps in order and reply with the first verdict that applies:

1. If the message is not a genuine customer support question about the product
   (greeting, internal chatter, opinion, feedback, thanks, sales/pricing
   negotiation, account-specific issue), or it involves custom pricing,
   contracts, PHI, or account data: reply SKIP.
2. If the articles explicitly and directly address the main thing being asked
   — one article alone, or a few articles that each directly cover part of the
   question: reply ANSWER. Reply ANSWER even when a minor detail is not
   covered, as long as the core is; the answer will state plainly what the
   docs do not cover. Never reply ANSWER when answering would require
   inference, piecing together loose fragments, or knowledge beyond what the
   articles state.
3. If the articles cover the same feature or topic as the question but do not
   directly answer the main thing being asked: reply RELATED. Questions asking
   whether a capability exists, where the articles cover that feature area
   without mentioning the capability, are RELATED.
4. Otherwise: reply SKIP.

Respond with one word - ANSWER, RELATED, or SKIP - followed by a one-line
reason."""

STYLE_PROMPT = """You are Healthie Help, answering a customer question in Slack using
ONLY the help center articles provided. Never invent information.

Style rules:
- Professional but warm tone. Avoid jargon; use common words.
- Short sentences, 15-20 words average. Active voice. Important info first.
- No exclamation points. Minimal contractions. Direct and straightforward.
- If a specific detail of the question is not covered by the articles, say so
  plainly instead of guessing or leaving it out.
- When the customer asks you to confirm something (a yes/no question), confirm
  only what the articles explicitly state. If the articles do not explicitly
  confirm it, do not say yes — say the help docs do not specify.
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


def search_help_center(query: str) -> list[dict]:
    """Search hits in the help center's own ranking order, as
    {"url", "title"} dicts (title falls back to a humanized slug)."""
    r = requests.get(f"{HELP_BASE}/search", params={"query": query},
                     headers={"User-Agent": "HealthieHelpBot/2.0"}, timeout=10)
    soup = BeautifulSoup(r.text, "html.parser")
    results = {}
    for a in soup.select('a[href^="/article/"]'):
        slug = a["href"].split("/article/", 1)[1]
        url = f"{HELP_BASE}/article/{slug}"
        if url not in results:
            title = a.get_text(strip=True) or slug.split("-", 1)[-1].replace("-", " ")
            results[url] = {"url": url, "title": title}
    return list(results.values())


def rerank(question: str, candidates: list[dict]) -> list[dict]:
    """Cheap Haiku pass: pick the MAX_ARTICLES candidates whose titles best
    match the question, so a good article buried by the help center's own
    ranking still gets fetched. Falls back to the top of the list as-is."""
    titles = "\n".join(f"{i + 1}. {c['title']}" for i, c in enumerate(candidates))
    try:
        resp = claude.messages.create(
            model=GATE_MODEL, max_tokens=30, temperature=0, system=RERANK_PROMPT,
            messages=[{"role": "user",
                       "content": f"QUESTION: {question}\n\nTITLES:\n{titles}"}],
        )
        picks = [int(n) for n in re.findall(r"\d+", resp.content[0].text)]
        chosen = [candidates[n - 1] for n in picks if 1 <= n <= len(candidates)]
        if chosen:
            return chosen[:MAX_ARTICLES]
    except Exception as e:
        print(f"rerank failed ({type(e).__name__}: {e}); using search order",
              flush=True)
    return candidates[:MAX_ARTICLES]


def search_articles(question: str) -> list[str]:
    """Search once per extracted keyword query; merge, dedupe, cap at
    MAX_CANDIDATES, then re-rank by title down to MAX_ARTICLES.
    Falls back to the raw message if the keyword queries find nothing."""
    hits = []
    for q in extract_queries(question):
        hits.extend(search_help_center(q))
    if not hits:
        hits = search_help_center(question)
    seen, candidates = set(), []
    for h in hits:
        if h["url"] not in seen:
            seen.add(h["url"])
            candidates.append(h)
    candidates = candidates[:MAX_CANDIDATES]
    if len(candidates) > MAX_ARTICLES:
        candidates = rerank(question, candidates)
    return [c["url"] for c in candidates[:MAX_ARTICLES]]


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


def gate(question: str, docs: str) -> tuple[str, str]:
    """Strict relevance gate. Deterministic. Returns (verdict, one-line
    reason) where verdict is ANSWER, RELATED, or SKIP (the default when
    the model's reply is unparseable)."""
    resp = claude.messages.create(
        model=GATE_MODEL, max_tokens=60, temperature=0, system=GATE_PROMPT,
        messages=[{"role": "user",
                   "content": f"ARTICLES:\n{docs}\n\nCUSTOMER QUESTION: {question}"}],
    )
    text = resp.content[0].text.strip()
    word = text.split()[0].strip(".:,-").upper() if text else ""
    verdict = word if word in ("ANSWER", "RELATED") else "SKIP"
    return verdict, text


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
        if not ws.get_values("A1:F1"):
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


def _now_eastern() -> str:
    """Eastern-time timestamp in a format Sheets parses as a native datetime."""
    return datetime.now(EASTERN).strftime("%Y-%m-%d %H:%M:%S")


def _sheet_text(value: str) -> str:
    """Keep user text literal under USER_ENTERED: a leading ' stops Sheets
    from parsing it as a formula (=, +, -) or mention (@)."""
    return f"'{value}" if value[:1] in ("=", "+", "-", "@") else value


def log_miss(channel: str, user: str, question: str, reason: str):
    row = [_now_eastern(), channel_name(channel), user_name(user),
           _sheet_text(question), reason]
    try:
        # USER_ENTERED so the timestamp lands as a real datetime cell
        _sheet().append_row(row, value_input_option="USER_ENTERED")
    except Exception as e:
        print(f"sheet log failed ({type(e).__name__}: {e}); dropped row: {row}",
              flush=True)


@app.event("message")
def handle_message(event, say, client):
    print(f"EVENT ch={event.get('channel')} user={event.get('user')} "
          f"bot={event.get('bot_id')} subtype={event.get('subtype')} "
          f"text={event.get('text', '')[:60]!r}", flush=True)
    # Spec step 2: human senders only; also ignore edits/joins/thread
    # broadcasts. file_share is exempt: a message with an attachment is
    # still a real question, and its text should be processed and logged.
    if event.get("bot_id") or event.get("subtype") not in (None, "file_share"):
        return
    if event["channel"] in IGNORED_CHANNELS:
        return
    if WATCHED_CHANNELS and event["channel"] not in WATCHED_CHANNELS:
        return

    # Spec step 3: full text is the question; strip optional prefix/mention.
    # A leading @healthie-help / bot mention means the user addressed us
    # directly, so we always reply — even if only to say we have nothing.
    # A message that opens by tagging some OTHER user is addressed to a
    # person, not to us: stay silent entirely.
    raw = event.get("text", "")
    m = MENTION_RE.match(raw)
    if m and m.group(2) and bot_user_id() and m.group(2) != bot_user_id():
        return
    mentioned = bool(m)
    question = MENTION_RE.sub("", raw).strip()
    reply_ts = event.get("thread_ts", event["ts"])

    # Mode commands: "@healthie-help mute|unmute|status" — per channel
    if mentioned:
        command = question.lower().rstrip(".!")
        if command in MUTE_COMMANDS:
            set_channel_mode(event["channel"], True, event["user"])
            say(text=MUTED_REPLY.format(bot=bot_mention()), thread_ts=reply_ts)
            return
        if command in UNMUTE_COMMANDS:
            set_channel_mode(event["channel"], False, event["user"])
            say(text=UNMUTED_REPLY.format(bot=bot_mention()), thread_ts=reply_ts)
            return
        if command in STATUS_COMMANDS:
            muted = event["channel"] in muted_channels
            template = STATUS_MUTED_REPLY if muted else STATUS_AUTO_REPLY
            say(text=template.format(bot=bot_mention()), thread_ts=reply_ts)
            return

    # Mention-only mode: stay silent for unmentioned messages in muted channels
    if not mentioned and event["channel"] in muted_channels:
        return

    if len(question) < 10:          # too short to be a real question
        return                      # silent even when mentioned — ask a question

    try:
        urls = search_articles(question)
        articles = [a for a in (fetch_article(u) for u in urls) if a["text"]]

        # Spec step 5: strict confidence gate — silence unless direct match,
        # except when mentioned: then reply with related links (if the
        # articles are at least on-topic) or a polite decline.
        if not articles:
            verdict, reason = "SKIP", "no_search_results"
        else:
            docs = build_docs(articles)
            verdict, reason = gate(question, docs)
        if verdict == "RELATED" and mentioned:
            links = "\n".join(f"<{a['url']}|{a['title']}>" for a in articles)
            say(text=RELATED_REPLY.format(links=links), thread_ts=reply_ts)
            log_miss(event["channel"], event["user"], question,
                     f"related_links: {reason}")
            return
        if verdict != "ANSWER":
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

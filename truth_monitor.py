"""
truth_monitor.py -- monitoring daemon for @realDonaldTrump on Truth Social.

Classifies every post against categories.json (hot-reloaded), records
everything, alerts on category matches flagged alert:true, and -- only for
alert-worthy posts -- triggers exactly one IBKR market-reaction snapshot via
ibkr_reaction.pull_snapshot(). See TRUTH_MONITOR_PROMPT.md for the full design
and the reasoning behind each piece; this file should track that spec, not
fork it.

First run: database is empty, so it backfills posts until it hits one already
seen (or a safety cap), classifying and recording each but firing no alerts
and no IBKR pulls, since it's history, not news. Every run after: polls for
new posts only, classifies live, alerts and pulls market data as configured.

Setup:
    pip install truthbrush anthropic ib_insync python-dotenv requests
    cp .env.example .env    -- fill in ANTHROPIC_API_KEY, Telegram, IBKR_*

Run:
    python truth_monitor.py
"""

import html
import json
import logging
import os
import random
import re
import sqlite3
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

import requests
from dotenv import load_dotenv
from truthbrush import Api

import ibkr_reaction

load_dotenv()   # reads .env if present; real env vars still take precedence

# ----------------------------- configuration -----------------------------

TARGET_USER = "realDonaldTrump"
POLL_INTERVAL_SEC = 30          # base interval; jittered +/-20% each cycle
JITTER_FRAC = 0.20

BACKFILL_SAFETY_CAP = 200       # stop backfilling even if nothing matches yet --
                                   # covers a genuinely empty db on first-ever run

# Keep this OUT of OneDrive/Dropbox -- sync processes grab file locks mid-write
# and SQLite throws intermittent "database is locked" errors. Override via
# TRUTH_DATA_DIR in .env; defaults to ~/truthdata so nothing is username-specific.
DATA_DIR = os.environ.get(
    "TRUTH_DATA_DIR", os.path.join(os.path.expanduser("~"), "truthdata")
)
DB_PATH = os.path.join(DATA_DIR, "truth.db")
STATE_FILE = os.path.join(DATA_DIR, "truth_monitor_state.json")

HERE = os.path.dirname(os.path.abspath(__file__))
CATEGORIES_FILE = os.environ.get(
    "TRUTH_CATEGORIES_FILE", os.path.join(HERE, "categories.json")
)

# Public timelines need no login. Flip to True (and set TRUTHSOCIAL_TOKEN in .env,
# pulled from the `truth:auth` Local Storage entry of a logged-in browser) only if
# the endpoint starts returning 401/403.
REQUIRE_TS_AUTH = os.environ.get("TRUTHSOCIAL_REQUIRE_AUTH", "false").lower() == "true"

USE_TELEGRAM = os.environ.get("TELEGRAM_BOT_TOKEN", "") != ""
USE_CONSOLE_BELL = True

HAIKU_MODEL = "claude-haiku-4-5-20251001"

# --------------------------------------------------------------------------


# --------------------------------- logging ---------------------------------

def init_logging() -> logging.Logger:
    os.makedirs(DATA_DIR, exist_ok=True)
    log = logging.getLogger("truth_monitor")
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    file_handler = RotatingFileHandler(
        os.path.join(DATA_DIR, "truth_monitor.log"),
        maxBytes=2_000_000, backupCount=3,
    )
    file_handler.setFormatter(fmt)
    log.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(fmt)
    log.addHandler(console_handler)
    return log


log = init_logging()


# ------------------------------- categories -------------------------------

_cat_cache = {"mtime": None, "categories": []}


def load_categories() -> list[dict]:
    """Reload categories.json only when it changes on disk. Each item:
    {"name": str, "description": str, "alert": bool}. app.py writes this file
    atomically (tmp + os.replace), so a read here only ever sees a complete
    old or complete new file -- never a partial one."""
    try:
        mtime = os.path.getmtime(CATEGORIES_FILE)
    except OSError:
        if not _cat_cache["categories"]:
            log.warning(f"{CATEGORIES_FILE} missing -- no categories configured")
        return _cat_cache["categories"]

    if mtime != _cat_cache["mtime"]:
        try:
            with open(CATEGORIES_FILE, encoding="utf-8") as f:
                cats = json.load(f)
            _cat_cache.update(mtime=mtime, categories=cats)
            log.info(f"loaded {len(cats)} categories: "
                     f"{', '.join(c['name'] for c in cats)}")
        except (json.JSONDecodeError, OSError) as e:
            log.warning(f"failed to load {CATEGORIES_FILE}: {e} -- keeping previous set")
    return _cat_cache["categories"]


# -------------------------------- storage ---------------------------------

def init_db() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA journal_mode=WAL")     # lets the dashboard read during writes
    con.execute("PRAGMA synchronous=NORMAL")   # safe with WAL, meaningfully faster
    con.execute("PRAGMA busy_timeout=2000")    # wait up to 2s on a lock instead of
                                                  # failing immediately -- daemon,
                                                  # dashboard, and backfill all touch
                                                  # this file
    con.execute("""
        CREATE TABLE IF NOT EXISTS posts (
            id          TEXT PRIMARY KEY,
            created_at  TEXT,
            seen_at     TEXT,
            text        TEXT,
            categories  TEXT,   -- JSON list of matched category names
            reasons     TEXT,   -- JSON dict: category -> one-line reason
            alerted     INTEGER,
            backfilled  INTEGER
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_seen_at ON posts(seen_at)")
    con.execute("""
        CREATE TABLE IF NOT EXISTS market_snapshots (
            post_id        TEXT PRIMARY KEY REFERENCES posts(id),
            pulled_at      TEXT,
            bars_json      TEXT,
            open_price     REAL,
            close_price    REAL,
            net_move_pct   REAL,
            market_open    INTEGER,
            error          TEXT
        )
    """)
    con.commit()
    con.close()


def post_exists(pid: str) -> bool:
    con = sqlite3.connect(DB_PATH)
    row = con.execute("SELECT 1 FROM posts WHERE id = ?", (pid,)).fetchone()
    con.close()
    return row is not None


def record_post(pid, created_at, text, categories, reasons, alerted, backfilled) -> None:
    try:
        con = sqlite3.connect(DB_PATH, timeout=10)
        con.execute(
            "INSERT OR REPLACE INTO posts VALUES (?,?,?,?,?,?,?,?)",
            (pid, created_at, datetime.now(timezone.utc).isoformat(), text,
             json.dumps(categories), json.dumps(reasons),
             int(alerted), int(backfilled)),
        )
        con.commit()
        con.close()
    except Exception as e:
        log.warning(f"db write (posts) failed for {pid}: {e}")


def record_snapshot(result: dict) -> None:
    try:
        con = sqlite3.connect(DB_PATH, timeout=10)
        con.execute(
            "INSERT OR REPLACE INTO market_snapshots VALUES (?,?,?,?,?,?,?,?)",
            (result["post_id"], datetime.now(timezone.utc).isoformat(),
             result["bars_json"], result["open_price"], result["close_price"],
             result["net_move_pct"], result["market_open"], result["error"]),
        )
        con.commit()
        con.close()
    except Exception as e:
        log.warning(f"db write (market_snapshots) failed for {result['post_id']}: {e}")


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"since_id": None}


def save_state(state: dict) -> None:
    """Atomic write -- same pattern as categories.json/ibkr_settings.json,
    since this file is also read (by us) and could in principle be inspected
    by another process mid-write otherwise."""
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_FILE)


# ------------------------------ classification -----------------------------

def strip_html(raw: str) -> str:
    """Truth Social status content arrives as HTML (Mastodon-derived markup).
    Handles the cases that actually show up in practice:
    - <p> paragraph wrappers -> blank line between paragraphs
    - <br>/<br/> -> newline
    - <span class="invisible">/"ellipsis"> link-truncation spans -- these
      wrap parts of a URL for *display* purposes only; stripping tags but
      keeping their text content (which this does) naturally reconstructs
      the full URL text, just not always contiguous -- good enough for
      classification, which only needs the gist of a linked domain, not a
      perfectly formed URL.
    - collapses repeated whitespace left behind after tag removal
    """
    text = re.sub(r"</p>\s*<p>", "\n\n", raw)
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def is_retweet(post: dict) -> bool:
    """API's 'reblog' field is the primary signal; text fallback because it's
    inconsistent across pulls -- both 'RT @user...' and 'RT: <url>' show up."""
    if post.get("reblog"):
        return True
    text = strip_html(post.get("content", "")).strip()
    return text.startswith(("RT ", "RT:", "RT@"))


def build_prompt(text: str, categories: list[dict], retweet: bool) -> str:
    cat_list = "\n".join(f"- {c['name']}: {c['description']}" for c in categories)
    context_line = (
        "This post is a retweet/repost of someone else's content.\n"
        if retweet else ""
    )
    return (
        "Classify the following social media post against this list of categories.\n\n"
        f"CATEGORIES:\n{cat_list}\n\n"
        f"POST:\n{context_line}{text}\n\n"
        "Instructions:\n"
        "- Include plausible or indirect connections, not only posts that are "
        "directly and explicitly about a category. Err toward flagging a "
        "reasonable connection rather than requiring certainty.\n"
        "- Select at most 3 categories. If more than 3 plausibly apply, keep only "
        "the 3 most relevant, ordered from most to least relevant.\n"
        "- If this post is a retweet, prefix each reason with '(retweet) '.\n"
        "- Each reason is one short sentence.\n"
        "- If nothing plausibly connects, return an empty list.\n\n"
        "Respond with ONLY a JSON object, no other text:\n"
        '{"matches": [{"category": "<exact name from the list above>", '
        '"reason": "<one short sentence>"}]}'
    )


def classify(text: str, categories: list[dict], retweet: bool) -> dict:
    """One Claude call, multi-label against all active categories at once.
    Fails open to no matches -- a classifier outage records the post with an
    empty category list rather than crashing the daemon."""
    if not categories:
        return {"categories": [], "reasons": {}}

    valid_names = {c["name"] for c in categories}
    try:
        import anthropic
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=300,
            messages=[{"role": "user",
                       "content": build_prompt(text, categories, retweet)}],
        )
        raw = resp.content[0].text.strip()
        raw = re.sub(r"^```(json)?|```$", "", raw, flags=re.MULTILINE).strip()
        parsed = json.loads(raw)
        matches = parsed.get("matches", [])
        # drop hallucinated category names rather than let them silently
        # fail to join against `alert` flags downstream
        matches = [m for m in matches if m.get("category") in valid_names]
        return {
            "categories": [m["category"] for m in matches],
            "reasons": {m["category"]: m["reason"] for m in matches},
        }
    except Exception as e:
        log.warning(f"classification failed: {e}")
        return {"categories": [], "reasons": {}}


# --------------------------------- alerts -----------------------------------

def send_telegram(msg: str) -> None:
    try:
        token = os.environ["TELEGRAM_BOT_TOKEN"]
        chat_id = os.environ["TELEGRAM_CHAT_ID"]
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": msg},
            timeout=10,
        )
    except Exception as e:
        log.warning(f"Telegram send failed: {e}")


def alert(created_at: str, text: str, matched: list[str], reasons: dict) -> None:
    detail = "; ".join(f"{c}: {reasons.get(c, '')}" for c in matched)
    body = (f"TRUTH ALERT [{', '.join(matched)}] {created_at}\n{detail}\n"
            f"{'-' * 40}\n{text[:1000]}")
    if USE_CONSOLE_BELL:
        print("\a", end="")
    log.info(body)
    if USE_TELEGRAM:
        send_telegram(body)


# ------------------------------ post pipeline -------------------------------

def parse_created_at(raw: str) -> datetime:
    """Truth Social timestamps are UTC ISO8601. Normalize explicitly rather
    than trusting fromisoformat's 'Z' handling to match across Python
    versions."""
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def process_post(post: dict, live: bool) -> None:
    """Classify one post, record it, alert and (only for alert-worthy
    matches) trigger one IBKR snapshot. `live=False` is backfill: still
    classified and recorded, but no alerts and no market pulls -- it's
    history, not news."""
    text = strip_html(post.get("content", ""))
    if not text:
        return  # media-only post; extend here if media handling is wanted
    pid, created_raw = post["id"], post.get("created_at", "")
    retweet = is_retweet(post)

    categories = load_categories()
    result = classify(text, categories, retweet)
    matched = result["categories"]

    alert_cats = {c["name"] for c in categories if c.get("alert")}
    alert_hit = bool(set(matched) & alert_cats)
    should_alert = live and alert_hit

    if should_alert:
        alert(created_raw, text, matched, result["reasons"])
    else:
        tag = ", ".join(matched) if matched else "no match"
        prefix = "[backfill]" if not live else f"[{datetime.now(timezone.utc):%H:%M:%S}]"
        log.info(f"{prefix} {tag} -- {pid}: {text[:80]}...")

    record_post(pid, created_raw, text, matched, result["reasons"],
                should_alert, not live)

    # market pull: only for alert-worthy matches, live or backfilled --
    # not spent on every single post regardless of relevance
    if alert_hit:
        try:
            created_at = parse_created_at(created_raw)
        except ValueError:
            log.warning(f"could not parse created_at {created_raw!r} for {pid}; "
                        "skipping market pull")
            return

        if live:
            # can't pull data from the future -- wait until the window's
            # end has actually elapsed. Typically well under a minute extra,
            # since the poll cycle itself already takes ~30s.
            wait_until = created_at.timestamp() + 60
            remaining = wait_until - datetime.now(timezone.utc).timestamp()
            if remaining > 0:
                time.sleep(remaining)

        snap = ibkr_reaction.pull_snapshot(
            pid, created_at, ibkr_reaction.IBKR_CLIENT_ID_DAEMON
        )
        record_snapshot(snap)
        if snap["error"]:
            log.warning(f"market pull failed for {pid}: {snap['error']}")


# ----------------------------------- main ------------------------------------

def main() -> None:
    init_db()
    api = Api(require_auth=REQUIRE_TS_AUTH)
    state = load_state()
    load_categories()

    if state["since_id"] is None:
        log.info("Empty state -- backfilling until an already-seen post is hit "
                  f"(cap {BACKFILL_SAFETY_CAP})...")
        gen = api.pull_statuses(TARGET_USER, replies=False, verbose=False)
        to_process = []
        for i, post in enumerate(gen):
            if i >= BACKFILL_SAFETY_CAP:
                log.warning(f"hit safety cap ({BACKFILL_SAFETY_CAP}) before finding "
                            "an already-seen post -- stopping backfill here")
                break
            if post_exists(post["id"]):
                break
            to_process.append(post)
        if not to_process:
            raise SystemExit("Could not reach timeline -- check auth/connectivity.")

        # backfill is a burst of quick pulls in succession -- a small delay
        # between them avoids hammering either API in a tight loop, even
        # though normal volume here is well under any documented pacing limit
        for post in reversed(to_process):   # oldest-first
            process_post(post, live=False)
            time.sleep(0.5)

        state["since_id"] = str(max(int(p["id"]) for p in to_process))
        save_state(state)
        log.info(f"Backfill complete: {len(to_process)} posts. "
                 f"since_id={state['since_id']}")

    log.info(f"Monitoring @{TARGET_USER} every ~{POLL_INTERVAL_SEC}s. "
             f"since_id={state['since_id']}  db={DB_PATH}")

    while True:
        try:
            new_posts = list(
                api.pull_statuses(
                    TARGET_USER,
                    since_id=state["since_id"],
                    replies=False,
                    verbose=False,
                )
            )
            if new_posts:
                for post in reversed(new_posts):   # oldest-first
                    process_post(post, live=True)
                state["since_id"] = str(max(int(p["id"]) for p in new_posts))
                save_state(state)
        except Exception as e:
            log.warning(f"poll cycle failed: {e}")
            time.sleep(POLL_INTERVAL_SEC * 4)   # back off on failure
            continue

        time.sleep(POLL_INTERVAL_SEC * (1 + random.uniform(-JITTER_FRAC, JITTER_FRAC)))


if __name__ == "__main__":
    main()

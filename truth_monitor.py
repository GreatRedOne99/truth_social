"""
truth_monitor.py — poll a Truth Social account, classify new posts, alert on important ones.

Setup (once, in your cuda311 env):
    pip install truthbrush anthropic requests

Runs unauthenticated -- no Truth Social login needed for public user timelines.
If the endpoint starts returning 401/403, log in via a browser, copy the token from
the `truth:auth` Local Storage entry, export it as TRUTHSOCIAL_TOKEN, and switch to
    api = Api()

Environment variables required:
    ANTHROPIC_API_KEY                      (only if USE_LLM_CLASSIFIER = True)
    TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID  (only if USE_TELEGRAM = True)

Smoke-test the alert path without waiting for a real post:
    python -c "import truth_monitor as t; t.process_post({'id':'0','created_at':'test','content':'<p>New tariffs on China effective Monday.</p>'})"

Telegram setup: message @BotFather -> /newbot -> get token.
Then message your new bot once, and GET
https://api.telegram.org/bot<TOKEN>/getUpdates to read your chat_id.
"""

import html
import json
import os
import random
import re
import time
from datetime import datetime, timezone

import requests
from truthbrush import Api

# ----------------------------- configuration -----------------------------

TARGET_USER = "realDonaldTrump"
POLL_INTERVAL_SEC = 30          # base interval; jittered +/-20% each cycle
JITTER_FRAC = 0.20
STATE_FILE = "truth_monitor_state.json"   # persists last-seen post id across restarts

USE_LLM_CLASSIFIER = True       # tier-2 Claude Haiku classification
USE_TELEGRAM = True
USE_CONSOLE_BELL = True         # terminal bell + print, works everywhere

# tier-1 keyword fast path: any hit -> immediate alert, skip LLM
MARKET_KEYWORDS = re.compile(
    r"\b(tariff|tariffs|fed|federal reserve|powell|interest rate|rates?|"
    r"china|chinese|dollar|treasur(y|ies)|trade deal|sanction|opec|oil|"
    r"crypto|bitcoin|nvidia|semiconductor|chips?|executive order|"
    r"nominat(e|ion)|fire[ds]?|resign)\b",
    re.IGNORECASE,
)

HAIKU_MODEL = "claude-haiku-4-5-20251001"
IMPORTANCE_THRESHOLD = 6        # LLM score 0-10; alert at or above this

# --------------------------------------------------------------------------


def strip_html(raw: str) -> str:
    """Truth Social status content arrives as HTML; reduce to plain text."""
    text = re.sub(r"<br\s*/?>", "\n", raw)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"since_id": None}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def classify_with_haiku(text: str) -> dict:
    """Returns {"score": int 0-10, "reason": str}. Fails open to score 0."""
    try:
        import anthropic
        client = anthropic.Anthropic()
        prompt = (
            "You are a market-news triage filter. Score the following Truth Social "
            "post 0-10 for potential impact on US equity/rates/FX markets. "
            "10 = immediately tradeable (tariff announcement, Fed personnel, major "
            "policy action). 0 = pure politics/personal with no market channel.\n\n"
            f"POST:\n{text}\n\n"
            'Respond with ONLY a JSON object: {"score": <int>, "reason": "<one sentence>"}'
        )
        resp = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        raw = re.sub(r"^```(json)?|```$", "", raw, flags=re.MULTILINE).strip()
        return json.loads(raw)
    except Exception as e:
        print(f"[warn] LLM classification failed: {e}")
        return {"score": 0, "reason": "classifier unavailable"}


def send_telegram(msg: str) -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": msg},
            timeout=10,
        )
    except Exception as e:
        print(f"[warn] Telegram send failed: {e}")


def alert(post_id: str, created_at: str, text: str, tier: str, detail: str) -> None:
    header = f"TRUTH ALERT [{tier}] {created_at}"
    body = f"{header}\n{detail}\n{'-' * 40}\n{text[:1000]}"
    if USE_CONSOLE_BELL:
        print("\a")  # terminal bell
    print(body)
    if USE_TELEGRAM:
        send_telegram(body)


def process_post(post: dict) -> None:
    text = strip_html(post.get("content", ""))
    if not text:
        return  # media-only post; extend here if you want media alerts
    created = post.get("created_at", "?")
    pid = post["id"]

    # tier 1: keyword fast path
    m = MARKET_KEYWORDS.search(text)
    if m:
        alert(pid, created, text, "KEYWORD", f"matched: '{m.group(0)}'")
        return

    # tier 2: LLM triage
    if USE_LLM_CLASSIFIER:
        result = classify_with_haiku(text)
        if result["score"] >= IMPORTANCE_THRESHOLD:
            alert(pid, created, text, f"LLM {result['score']}/10", result["reason"])
            return

    print(f"[{datetime.now(timezone.utc):%H:%M:%S}] low-importance post {pid}: "
          f"{text[:80]}...")


def main() -> None:
    api = Api(require_auth=False)
    state = load_state()

    # cold start: seed from the newest post so we don't backfill-alert history.
    # deliberately outside try/except -- fail loudly if the endpoint is dead.
    if state["since_id"] is None:
        seed = next(api.pull_statuses(TARGET_USER, replies=False, verbose=False), None)
        if seed is None:
            raise SystemExit("Could not reach timeline -- check connectivity/endpoint.")
        state["since_id"] = seed["id"]
        save_state(state)
        print(f"Seeded since_id={seed['id']}")

    print(f"Monitoring @{TARGET_USER} every ~{POLL_INTERVAL_SEC}s. "
          f"since_id={state['since_id']}")

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
                for post in reversed(new_posts):   # oldest-first ordering
                    process_post(post)
                state["since_id"] = str(max(int(p["id"]) for p in new_posts))
                save_state(state)
        except Exception as e:
            print(f"[warn] poll cycle failed: {e}")
            time.sleep(POLL_INTERVAL_SEC * 4)   # back off
            continue

        time.sleep(POLL_INTERVAL_SEC * (1 + random.uniform(-JITTER_FRAC, JITTER_FRAC)))


if __name__ == "__main__":
    main()
"""
app.py -- dashboard + category manager for the Truth Social monitor.

    streamlit run app.py

Read-only on the posts/market_snapshots tables (opens with mode=ro) so the UI
can never lock or corrupt the daemon's writes. Category edits, by contrast, DO
write -- atomically -- to categories.json, which the daemon hot-reloads on its
own next poll cycle. The two processes never touch each other's files directly.
"""

import json
import os
import sqlite3
import threading
import urllib.parse
from datetime import datetime, timedelta, timezone

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

import ibkr_reaction


load_dotenv()

DATA_DIR = os.environ.get(
    "TRUTH_DATA_DIR", os.path.join(os.path.expanduser("~"), "truthdata")
)
DB_PATH = os.path.join(DATA_DIR, "truth.db")

HERE = os.path.dirname(os.path.abspath(__file__))
CATEGORIES_FILE = os.environ.get(
    "TRUTH_CATEGORIES_FILE", os.path.join(HERE, "categories.json")
)
TECH_DESIGN_FILE = os.path.join(HERE, "Sessions_and_Prompts", "TRUTH_MONITOR_PROMPT.md")

REFRESH_SEC = 15
STALE_ALERT_MIN = 60


# ------------------------------- categories --------------------------------

def read_categories() -> list[dict]:
    if not os.path.exists(CATEGORIES_FILE):
        return []
    with open(CATEGORIES_FILE, encoding="utf-8") as f:
        return json.load(f)


def write_categories(cats: list[dict]) -> None:
    """Atomic write -- tmp file + os.replace -- so the daemon's mtime-checked
    read (which can land at any moment) never sees a half-written file."""
    tmp = CATEGORIES_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cats, f, indent=2)
    os.replace(tmp, CATEGORIES_FILE)


def category_manager() -> None:
    st.subheader("Categories")
    st.caption(
        "The monitor classifies every new post against these. Edits apply to the "
        "next poll cycle automatically -- no restart needed. \"Alert\" means a "
        "match pushes a notification and triggers a market-reaction pull; "
        "unchecked categories are still recorded and shown here, just silently."
    )

    # smaller font on the per-row controls -- explicitly requested, since the
    # sidebar is too narrow (even with the main page set to "wide") for these
    # at default size without wrapping
    st.markdown(
        "<style>section[data-testid='stSidebar'] "
        "div[data-testid='stButton'] button, "
        "section[data-testid='stSidebar'] "
        "label[data-testid='stWidgetLabel'] p "
        "{ font-size: 0.75rem; }</style>",
        unsafe_allow_html=True,
    )

    cats = read_categories()

    for i, c in enumerate(cats):
        # one row per category, two lines: name+description on top, the
        # alert checkbox and delete button in a horizontal row below --
        # side-by-side columns here get squeezed to the point of hiding
        # controls, since the sidebar itself stays narrow even in wide mode
        with st.container(border=True):
            st.markdown(f"**{c['name']}**")
            st.caption(c["description"])
            with st.container(horizontal=True):
                new_alert = st.checkbox(
                    "Alert", value=c.get("alert", False), key=f"alert_{i}")
                if new_alert != c.get("alert", False):
                    cats[i]["alert"] = new_alert
                    write_categories(cats)
                    st.rerun()
                if st.button("Delete", icon=":material/delete:", key=f"del_{i}"):
                    cats.pop(i)
                    write_categories(cats)
                    st.rerun()

    with st.form("add_category", clear_on_submit=True):
        st.markdown("**Add a category**")
        c1, c2, c3 = st.columns([2, 4, 1])
        name = c1.text_input("Name", placeholder="e.g. Christmas")
        desc = c2.text_input(
            "Description (this is what the classifier reads)",
            placeholder="e.g. Anything about Christmas, holidays, gift-giving",
        )
        alert_flag = c3.checkbox("Alert", value=False)
        submitted = st.form_submit_button("Add")
        if submitted and name.strip() and desc.strip():
            if any(c["name"].lower() == name.strip().lower() for c in cats):
                st.error(f"'{name}' already exists.")
            else:
                cats.append({"name": name.strip(), "description": desc.strip(),
                            "alert": alert_flag})
                write_categories(cats)
                st.rerun()


# --------------------------------- links ------------------------------------

def truth_social_url(post_id: str) -> str:
    return f"https://truthsocial.com/@realDonaldTrump/posts/{post_id}"


def x_search_url(text: str) -> str:
    words = " ".join(text.split()[:12])   # X's search degrades on long queries
    q = urllib.parse.quote(words)
    return f"https://twitter.com/search?q={q}&src=typed_query&f=live"


# --------------------------------- data -------------------------------------

@st.cache_data(ttl=REFRESH_SEC)
def load(hours: int) -> pd.DataFrame:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    try:
        con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=10)
    except sqlite3.OperationalError:
        return pd.DataFrame()
    try:
        df = pd.read_sql_query(
            """
            SELECT p.*, m.bars_json, m.open_price, m.close_price,
                   m.net_move_pct, m.market_open, m.error AS market_error
            FROM posts p
            LEFT JOIN market_snapshots m ON m.post_id = p.id
            WHERE p.seen_at > ?
            ORDER BY p.seen_at DESC
            """,
            con, params=(cutoff,),
        )
    except pd.errors.DatabaseError:
        df = pd.DataFrame()
    finally:
        con.close()
    if not df.empty:
        df["categories"] = df["categories"].apply(json.loads)
        df["reasons"] = df["reasons"].apply(json.loads)
        # stored/queried in UTC throughout -- parsed to tz-aware here so the
        # dataframe's DatetimeColumns can render them in ET for display,
        # matching the ET convention ibkr_reaction.py already uses for market
        # hours. Nothing downstream (freshness/age_min) is affected -- that
        # math is done on tz-aware UTC values either way.
        df["seen_at"] = pd.to_datetime(df["seen_at"], utc=True)
        df["created_at"] = pd.to_datetime(df["created_at"], utc=True)

        def closes(bars_json):
            if not bars_json:
                return None
            try:
                bars = json.loads(bars_json)
                return [b["close"] for b in bars]
            except (json.JSONDecodeError, KeyError, TypeError):
                return None

        df["chart"] = df["bars_json"].apply(closes)
        df["truth_link"] = df["id"].apply(truth_social_url)
        df["x_link"] = df["text"].apply(x_search_url)
        # a post needs a retry if it was alerted but never got a successful
        # snapshot -- either no row at all (market_open is NaN after the
        # LEFT JOIN) or a failed pull (market_open is None/NaN with an error)
        df["needs_retry"] = (df["alerted"] == 1) & (df["market_open"].isna())
    return df


# -------------------------------- retry --------------------------------------

def retry_pull(post_id: str, created_at: pd.Timestamp) -> None:
    """Fire-and-forget: runs pull_snapshot in a background thread so the UI
    doesn't block, per the async decision in the spec. Writes straight to
    SQLite -- never touches st.session_state from the thread, which is the
    documented way to crash a Streamlit app.

    created_at is a tz-aware (UTC) pandas Timestamp -- load() parses it from
    the stored ISO string so DatetimeColumns can render it in ET."""
    def _run():
        from datetime import datetime as dt
        created = created_at.to_pydatetime()
        result = ibkr_reaction.pull_snapshot(
            post_id, created, ibkr_reaction.IBKR_CLIENT_ID_DASHBOARD
        )
        try:
            con = sqlite3.connect(DB_PATH, timeout=10)
            con.execute(
                "INSERT OR REPLACE INTO market_snapshots VALUES (?,?,?,?,?,?,?,?)",
                (result["post_id"], dt.now(timezone.utc).isoformat(),
                 result["bars_json"], result["open_price"], result["close_price"],
                 result["net_move_pct"], result["market_open"], result["error"]),
            )
            con.commit()
            con.close()
        except Exception:
            pass   # best-effort; next refresh will just show it as still pending

    threading.Thread(target=_run, daemon=True).start()


# -------------------------------- documentation -------------------------------

def user_guide() -> None:
    st.markdown(
        """
### What this is

Monitors [@realDonaldTrump](https://truthsocial.com/@realDonaldTrump) on Truth
Social, classifies every post against the categories you define below, alerts
you on the ones you've flagged as important, and pulls a snapshot of ES
futures price action around alert-worthy posts so you can see whether the
market actually reacted.

### Running it

Two processes, both required:

- **`python truth_monitor.py`** (or `run_all.bat`) -- the daemon. The only
  thing that writes to the database: polls for new posts, classifies them,
  fires alerts, and triggers market pulls. Nothing shows up here until this
  has run at least once.
- **`streamlit run app.py`** (this dashboard) -- read-only. Displays what the
  daemon has written, lets you manage categories, and lets you retry a failed
  market pull.

### Managing categories (sidebar)

Each category has a **name**, a **description** (this is literally what the
classifier reads to decide a match -- write it like you're briefing someone
who's never seen the account before), and an **Alert** checkbox. Checked means
a match pushes a notification and triggers an IBKR market-reaction pull;
unchecked means it's still recorded and shown here, just silently. Edits apply
on the daemon's next poll cycle automatically -- no restart needed.

### Reading the live feed

- **Window** -- how far back to look.
- **Filter by category** -- narrow the feed to one or more categories, or
  `(none)` for posts that matched nothing.
- The metrics row shows post volume, alert count, backfill count, how many
  posts landed while the market was open vs. closed, and how long ago the
  daemon last wrote anything -- if that number gets large, the daemon has
  likely died.
- **⚠ alerts missing a market pull** -- shown when an alert-worthy post never
  got a successful IBKR snapshot (Gateway unreachable, etc). Retry from here.
- **2min reaction** column is a thumbnail: 5-second ES bars for the 2 minutes
  straddling the post (1 minute before/after). **Net move %** is computed once
  from that window's first Open to its last Close -- there's only ever one
  pull per post, never a live-updating feed.
- **Truth Social** / **Search X** columns link out to the original post and an
  X search for the same text, so you can check cross-platform spread.

### Something looks wrong

- **Dashboard shows nothing** -- daemon isn't running, or hasn't finished its
  first backfill yet.
- **"Last record N min ago -- daemon may be dead"** -- check the daemon's
  terminal/log for a crash.
- **Market column stuck on "pending"** -- either the post hasn't hit its
  1-minute-after window yet, or the pull failed; check the retry expander.
        """
    )


def technical_design() -> None:
    st.caption(
        f"Rendered live from `{os.path.relpath(TECH_DESIGN_FILE, HERE)}` -- always "
        "in sync with the spec this app was built from."
    )
    if not os.path.exists(TECH_DESIGN_FILE):
        st.warning(f"Spec file not found at `{TECH_DESIGN_FILE}`.")
        return
    with open(TECH_DESIGN_FILE, encoding="utf-8") as f:
        st.markdown(f.read())


# -------------------------------- live feed -----------------------------------

def live_feed() -> None:
    c1, c2 = st.columns([1, 2])
    hours = c1.selectbox("Window (hours)", [1, 6, 24, 72, 168, 24 * 30], index=2)

    df = load(hours)
    if df.empty:
        st.info(
            f"No posts recorded in the last {hours}h.\n\n"
            f"Is `truth_monitor.py` running? Expected database at `{DB_PATH}`.\n\n"
            "On first run it backfills until it hits an already-seen post."
        )
        return

    all_cats = sorted({c for row in df["categories"] for c in row} | {"(none)"})
    picked = c2.multiselect("Filter by category", all_cats, default=[])

    # freshness -- the failure mode that actually bites is the daemon dying quietly
    # overnight while the dashboard keeps cheerfully showing yesterday's alerts
    last_seen = pd.to_datetime(df["seen_at"]).max()
    age_min = (datetime.now(timezone.utc)
               - last_seen.to_pydatetime()).total_seconds() / 60
    if age_min > STALE_ALERT_MIN:
        st.error(f"Last record {age_min:.0f} min ago -- daemon may be dead.")

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Posts in window", len(df))
    m2.metric("Alerts fired", int(df["alerted"].sum()))
    m3.metric("Backfilled", int(df["backfilled"].sum()))
    m4.metric("Market open / closed",
              f"{int((df['market_open'] == 1).sum())} / "
              f"{int((df['market_open'] == 0).sum())}")
    m5.metric("Last seen", f"{age_min:.0f} min ago")

    view = df
    if picked:
        mask = df["categories"].apply(
            lambda cs: bool(set(cs) & set(picked))
            or ("(none)" in picked and not cs)
        )
        view = df[mask]

    if view.empty:
        st.warning("No rows match the current filters.")
        return

    # --- pending retries, shown separately since st.dataframe can't host
    # per-row buttons; kept compact since these should be rare ---
    pending = view[view["needs_retry"]]
    if len(pending):
        with st.expander(f"⚠ {len(pending)} alert(s) missing a market pull", expanded=True):
            for _, row in pending.iterrows():
                rc1, rc2 = st.columns([5, 1])
                rc1.write(f"`{row['id']}` — {row['text'][:80]}...")
                if rc2.button("Retry", key=f"retry_{row['id']}"):
                    retry_pull(row["id"], row["created_at"])
                    st.info("Retry queued -- will appear on next refresh.")

    display = view.copy()
    display["categories"] = display["categories"].apply(
        lambda cs: ", ".join(cs) if cs else "-")
    display["reason"] = display.apply(
        lambda r: "; ".join(f"{k}: {v}" for k, v in r["reasons"].items()), axis=1)
    display["source"] = display["backfilled"].apply(
        lambda b: "backfill" if b else "live")
    display["market"] = display["market_open"].map(
        {1: "open", 0: "closed"}).fillna("pending")

    st.dataframe(
        display[["seen_at", "created_at", "source", "categories", "reason",
                  "text", "market", "net_move_pct", "chart",
                  "truth_link", "x_link"]],
        width="stretch",
        hide_index=True,
        column_config={
            "seen_at": st.column_config.DatetimeColumn(
                "Detected", format="MM-DD HH:mm z", timezone="America/New_York"),
            "created_at": st.column_config.DatetimeColumn(
                "Posted", format="MM-DD HH:mm z", timezone="America/New_York",
                width="small"),
            "source": st.column_config.TextColumn("Source", width="small"),
            "categories": st.column_config.TextColumn("Categories", width="medium"),
            "reason": st.column_config.TextColumn("Why", width="large"),
            "text": st.column_config.TextColumn("Post", width="large"),
            "market": st.column_config.TextColumn("Market", width="small"),
            "net_move_pct": st.column_config.NumberColumn(
                "Net move %", format="%.3f%%", width="small"),
            "chart": st.column_config.LineChartColumn(
                "2min reaction", width="small"),
            "truth_link": st.column_config.LinkColumn(
                "Truth Social", display_text="view", width="small"),
            "x_link": st.column_config.LinkColumn(
                "Search X", display_text="search", width="small"),
        },
    )

    with st.expander("Category frequency"):
        counts = pd.Series(
            [c for row in view["categories"] for c in row]
        ).value_counts()
        if len(counts):
            st.bar_chart(counts)
        else:
            st.caption("No category matches in the current filter.")

    st.caption(f"Auto-refreshes every {REFRESH_SEC}s. Database: {DB_PATH} (read-only)")


# -------------------------------- dashboard ----------------------------------

def dashboard() -> None:
    st.set_page_config(
        page_title="Truth Social Monitor",
        page_icon=":material/newspaper:",
        layout="wide",
    )
    st.title("Truth Social Monitor")

    with st.sidebar:
        category_manager()

    tab_feed, tab_guide, tab_design = st.tabs(
        ["Live feed", "User guide", "Technical design"]
    )
    with tab_feed:
        live_feed()
    with tab_guide:
        user_guide()
    with tab_design:
        technical_design()


if __name__ == "__main__":
    dashboard()
else:
    dashboard()

# Truth Social Monitor 
> for DJT Posts and Market Reaction

Monitors a Truth Social account continuously, classifies every post against a set of categories you define (Stocks, Economy, Political, Christmas, whatever), records everything, and pushes an alert when a post matches a category you've flagged.

## Architecture

Three pieces that only communicate through files, never directly:

```
categories.json  <--edits from-->  app.py (sidebar)
       |
       v (hot-reload, mtime-checked)
truth_monitor.py  --writes-->  SQLite (WAL)  <--reads (mode=ro)--  app.py (feed)
```

The dashboard never writes to the database, so no UI bug can lock or corrupt the
daemon's writes. The daemon never writes to `categories.json`, only reads it — all
editing happens through the app's sidebar. Either process restarts independently.

**First run:** database is empty, so the daemon backfills the last 20 posts,
classifies and records them, but does not push alerts (it's history). After that it
polls for new posts only, and every new post gets classified live with alerts firing
for category matches you've flagged.

## Setup

```bash
conda create -n truthmon python=3.14
conda activate truthmon
pip install truthbrush anthropic requests streamlit pandas python-dotenv ib_async

cp .env.example .env      # Windows: copy .env.example .env
# then fill in ANTHROPIC_API_KEY and the Telegram values
```

`truthbrush` must be the maintained fork (`w2rc`), not the archived PyPI release --
the PyPI build lacks `require_auth` and forces a login:

```bash
pip install --force-reinstall git+https://github.com/w2rc/truthbrush
```

## Usage

```bash
python truth_monitor.py       # start the daemon (backfills automatically if db is empty)
streamlit run app.py          # dashboard + category manager at http://localhost:8501
```

Run both, in separate terminals. Add categories from the app's sidebar; the daemon
picks them up on its next poll cycle with no restart needed.

```bash
python dbclean.py              # inspect row counts, category frequency
python dbclean.py --purge      # delete any seed/test rows
```

## Categories

Each entry in `categories.json` is `{"name", "description", "alert"}`. The
`description` is what Claude actually reads to decide a match, so write it like
you're briefing someone who's never seen the account before -- "Anything about
Christmas, holidays, gift-giving" works better than just "Christmas". `alert: true`
means a match pushes a notification; `false` means it's still recorded and visible
in the dashboard, just silently.

Editing is meant to happen through the app's sidebar (add / toggle-alert / delete),
not by hand-editing the JSON, though either works since it's just a file.

## Tuning poll interval

Detection latency for a post arriving uniformly within a polling window:

    E[delay] = T/2,  max = T + t_request

where `T` is `POLL_INTERVAL_SEC` and `t_request` is the HTTP round trip (~0.5-2s
through Cloudflare). Request rate is `1/T`. There is no published rate limit, so
treat `T` empirically: start at 30s, tighten only if it survives several days.
Requests are jittered +/-20% so the traffic pattern doesn't look like a cron job.

This is an "alert me so I can look at my screen" architecture, not an
execution-grade one -- firms trading these headlines pay for sub-second commercial
feeds.

## Notes

- Automated polling likely violates Truth Social's terms of service. Running
  unauthenticated (the default) means there is no account to ban.
- Keep `TRUTH_DATA_DIR` out of OneDrive/Dropbox. Sync processes grab file locks
  mid-write and SQLite fails intermittently in ways that are painful to diagnose.
- The classifier fails open to no matches. If `ANTHROPIC_API_KEY` is unset or a
  call errors, that post is recorded with an empty category list rather than
  crashing the daemon -- check the console for `[warn] classification failed`.
- One Claude call per post (not per category), asking for all matches at once.
  At normal posting volume this costs well under a cent a day.
- If you already have a `truth.db` from an earlier version (keyword/score schema),
  it won't match the new `posts` table. Delete it and let the daemon recreate and
  re-backfill, or migrate manually if you want to keep the history.
"""
ibkr_reaction.py -- one-shot market-reaction snapshot around a Truth Social
post's timestamp. Every call is connect -> pull -> disconnect; nothing here
ever holds a connection open. See TRUTH_MONITOR_PROMPT.md, "Market reaction
(IBKR)" for the full design and reasoning.

Exactly one pull per post, ever -- a fixed 2-minute window of 5-second ES
bars straddling created_at. Not a growing window, not a recurring re-pull.
Works identically for a post detected live or discovered days later during
backfill, since the window is anchored to created_at, never to "now".
"""

import os
import zoneinfo
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from ib_async import IB, ContFuture, Future, util

load_dotenv()

IBKR_HOST = os.environ.get("IBKR_HOST", "127.0.0.1")
IBKR_PORT = int(os.environ.get("IBKR_PORT", "4002"))
IBKR_CLIENT_ID_DAEMON = int(os.environ.get("IBKR_CLIENT_ID_DAEMON", "101"))
IBKR_CLIENT_ID_DASHBOARD = int(os.environ.get("IBKR_CLIENT_ID_DASHBOARD", "102"))

_ET = zoneinfo.ZoneInfo("America/New_York")


def es_market_open(window_start_utc: datetime, window_end_utc: datetime) -> bool:
    """Rule-based check against ES's known schedule. Does NOT cover CME
    holidays -- an unrecognized holiday falls through to the len(df) check
    in pull_snapshot rather than being caught here. Good enough to skip the
    routine closures (weekends, daily maintenance, post-cash-close halt)
    without spending an API call on a window already known to be closed."""
    for t in (window_start_utc, window_end_utc):
        t_et = t.astimezone(_ET)
        dow, hm = t_et.weekday(), t_et.hour * 60 + t_et.minute
        if dow == 4 and hm >= 17 * 60:           # Fri after 5pm ET
            return False
        if dow == 5:                             # all day Saturday
            return False
        if dow == 6 and hm < 18 * 60:             # Sun before 6pm ET
            return False
        if 17 * 60 <= hm < 18 * 60:               # daily maintenance 5-6pm ET
            return False
        if 16 * 60 + 15 <= hm < 16 * 60 + 30:     # post-cash-close halt
            return False
    return True


def pull_snapshot(post_id: str, created_at: datetime, client_id: int) -> dict:
    """Connect, pull the fixed 2-minute 5-second-bar window straddling
    created_at, disconnect. Same call whether the post is seconds or days
    old -- the window is anchored to created_at, never to "now".

    client_id: IBKR_CLIENT_ID_DAEMON or IBKR_CLIENT_ID_DASHBOARD -- never
    share one across both; a backfill batch and a live pull, or a manual
    retry click, could otherwise collide.

    Returns a dict ready for the market_snapshots row:
        market_open: 0 = confirmed closed (skipped, no API call spent)
                     1 = open, pull succeeded
                     None = unknown -- pull failed (Gateway down, etc.)
    """
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)

    window_start = created_at - timedelta(seconds=60)
    window_end = created_at + timedelta(seconds=60)

    # never ask for data from the future -- caller (truth_monitor.py) is
    # responsible for waiting until window_end has actually elapsed for a
    # live post; this is a defensive second check, not the primary control
    now = datetime.now(timezone.utc)
    if window_end > now:
        return {"post_id": post_id, "bars_json": None, "open_price": None,
                "close_price": None, "net_move_pct": None,
                "market_open": None,
                "error": "window_end is in the future -- called too early"}

    if not es_market_open(window_start, window_end):
        return {"post_id": post_id, "bars_json": None, "open_price": None,
                "close_price": None, "net_move_pct": None,
                "market_open": 0, "error": None}

    ib = IB()
    try:
        ib.connect(IBKR_HOST, IBKR_PORT, clientId=client_id, timeout=10)
        # ContFuture itself can't take an explicit endDateTime for historical
        # bars (IBKR error 10339) -- only reqHistoricalData(endDateTime='')
        # is allowed on it. Qualifying it resolves the current front-month's
        # real contract fields (lastTradeDateOrContractMonth, localSymbol);
        # re-resolving on every call means this follows quarterly rollover
        # automatically, with no hardcoded expiry.
        cont = ContFuture('ES', 'CME')
        ib.qualifyContracts(cont)
        contract = Future(
            symbol=cont.symbol,
            lastTradeDateOrContractMonth=cont.lastTradeDateOrContractMonth,
            exchange=cont.exchange,
            currency=cont.currency,
        )
        ib.qualifyContracts(contract)
        bars = ib.reqHistoricalData(
            contract,
            endDateTime=window_end,   # always an explicit past timestamp --
                                        # never '', since the window is always
                                        # anchored to the post, not to "now"
            durationStr='120 S',      # fixed, always -- never grows
            barSizeSetting='5 secs',
            whatToShow='TRADES',
            useRTH=False,             # Trump posts at all hours
            formatDate=2,
        )
        df = util.df(bars)
        if df is None or len(df) == 0:
            return {"post_id": post_id, "bars_json": None, "open_price": None,
                    "close_price": None, "net_move_pct": None,
                    "market_open": 0, "error": None}

        open_price = float(df.iloc[0]["open"])
        close_price = float(df.iloc[-1]["close"])
        net_move_pct = (close_price - open_price) / open_price * 100

        return {"post_id": post_id, "bars_json": df.to_json(orient="records"),
                "open_price": open_price, "close_price": close_price,
                "net_move_pct": net_move_pct, "market_open": 1, "error": None}
    except Exception as e:
        return {"post_id": post_id, "bars_json": None, "open_price": None,
                "close_price": None, "net_move_pct": None,
                "market_open": None, "error": str(e)}
    finally:
        ib.disconnect()   # always, even on failure

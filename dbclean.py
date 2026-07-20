"""
dbclean.py -- inspect and clean the truth_monitor database.

    python dbclean.py            # report only, changes nothing
    python dbclean.py --purge    # delete synthetic seed rows

Reports the resolved DB path first, so you can confirm both this and the Streamlit
app are pointed at the same file -- a path mismatch is the usual reason the
dashboard disagrees with backfill output.
"""

import os
import sqlite3
import sys

import truth_monitor as tm


def main() -> None:
    print(f"DATA_DIR : {tm.DATA_DIR}")
    print(f"DB_PATH  : {tm.DB_PATH}")
    print(f"exists   : {os.path.exists(tm.DB_PATH)}")
    if not os.path.exists(tm.DB_PATH):
        raise SystemExit("No database at that path -- check TRUTH_DATA_DIR in .env")

    con = sqlite3.connect(tm.DB_PATH)
    con.row_factory = sqlite3.Row

    total = con.execute("SELECT COUNT(*) c FROM posts").fetchone()["c"]
    print(f"\nrows total: {total}\n")

    print(f"{'source':10} {'count':>5}  {'oldest seen':17} {'newest seen':17}")
    print("-" * 60)
    for r in con.execute("""
        SELECT backfilled, COUNT(*) c, MIN(seen_at) lo, MAX(seen_at) hi
        FROM posts GROUP BY backfilled ORDER BY c DESC
    """):
        source = "backfill" if r["backfilled"] else "live"
        print(f"{source:10} {r['c']:>5}  {str(r['lo'])[:16]:17} {str(r['hi'])[:16]:17}")

    alerted = con.execute("SELECT COUNT(*) c FROM posts WHERE alerted = 1").fetchone()["c"]
    print(f"\nalerts fired: {alerted}")

    snaps = con.execute("SELECT COUNT(*) c FROM market_snapshots").fetchone()["c"]
    pending = con.execute("""
        SELECT COUNT(*) c FROM posts p
        WHERE p.alerted = 1
        AND NOT EXISTS (SELECT 1 FROM market_snapshots m WHERE m.post_id = p.id)
    """).fetchone()["c"]
    print(f"market snapshots: {snaps}  (pending/missing for alerted posts: {pending})")

    seeds = con.execute(
        "SELECT COUNT(*) c FROM posts WHERE id LIKE 'seed%'").fetchone()["c"]
    print(f"\nsynthetic seed rows: {seeds}")

    if "--purge" in sys.argv:
        con.execute("DELETE FROM market_snapshots WHERE post_id LIKE 'seed%'")
        con.execute("DELETE FROM posts WHERE id LIKE 'seed%'")
        con.commit()
        con.execute("VACUUM")
        left = con.execute("SELECT COUNT(*) c FROM posts").fetchone()["c"]
        print(f"purged {seeds} seed rows -- {left} real rows remain")
    elif seeds:
        print("run with --purge to delete them")

    con.close()


if __name__ == "__main__":
    main()

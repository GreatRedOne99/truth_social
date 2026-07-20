"""
backfill.py -- pull the last N real posts and classify them offline, outside
the daemon's poll loop.

Does NOT fire alerts, does NOT pull IBKR data, and does NOT touch
truth_monitor_state.json -- it's the same live=False path truth_monitor.py
itself uses on first run, just runnable on demand against a fixed count. Safe
to run against a live daemon; rows land in the same posts table with
backfilled=1, indistinguishable from the daemon's own first-run backfill.

    python backfill.py

Purpose: a quick way to eyeball how the current categories.json classifies a
batch of recent real posts before trusting it live.
"""

import truth_monitor as tm

N_POSTS = 20


def main() -> None:
    tm.init_db()
    api = tm.Api(require_auth=tm.REQUIRE_TS_AUTH)
    cats = tm.load_categories()
    if not cats:
        raise SystemExit(f"No categories loaded from {tm.CATEGORIES_FILE} -- "
                          "add at least one before backfilling.")

    print(f"Pulling last {N_POSTS} posts from @{tm.TARGET_USER}...\n")
    gen = api.pull_statuses(tm.TARGET_USER, replies=False, verbose=False)
    posts = [p for _, p in zip(range(N_POSTS), gen)]
    if not posts:
        raise SystemExit("No posts returned -- check auth/connectivity.")

    print(f"{'created_at':20} {'rt':3}  matches")
    print("-" * 100)
    for post in reversed(posts):   # oldest-first, matches daemon ordering
        text = tm.strip_html(post.get("content", ""))
        if not text:
            continue
        retweet = tm.is_retweet(post)
        result = tm.classify(text, cats, retweet)
        tag = ", ".join(result["categories"]) if result["categories"] else "-"
        print(f"{post.get('created_at', '?'):20} {'RT' if retweet else '':3}  {tag}")
        tm.record_post(post["id"], post.get("created_at", ""), text,
                        result["categories"], result["reasons"],
                        alerted=False, backfilled=True)

    print(f"\n{len(posts)} posts pulled, recorded with backfilled=1, alerted=0.")


if __name__ == "__main__":
    main()

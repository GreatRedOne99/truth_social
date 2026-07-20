# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with
code in this repository.

Truth Social monitor for @realDonaldTrump: classify posts by category, alert on
matches, pull ES futures reaction data around alert-worthy posts.

## Before writing any code

Read `TRUTH_MONITOR_PROMPT.md` in full. It's the source of truth for
architecture, the classification prompt, the IBKR pull design, and the reasoning
behind each decision. This file is a conventions summary, not a spec substitute
— if something here and the spec disagree, the spec wins and this file is stale.

## Rules

### NASA Power of Ten — Coding Prohibitions (Language Agnostic)
#### Negated from NASA's 10 Rules for Safety-Critical Code.
##### All items are SHALL NOT unless marked *should not*.

- Do not use unstructured control flow: no goto-equivalents, no non-local jumps, no exception/condition mechanisms used as control flow substitutes, no direct or indirect recursion.
- Do not write loops without a statically determinable upper bound. Every loop over external, dynamic, or recursively-structured data must have an explicit cap enforced in code. The bound must be a named constant with a documented rationale. If a static analysis tool cannot prove the bound, the rule is violated.
- Do not allocate memory dynamically after initialization. Pre-allocate all required data structures at startup. Do not grow collections, buffers, or strings in runtime hot paths.
- Do not write functions longer than 60 lines at one statement per line and one declaration per line. No exceptions.
- Do not write any function without at least two assertions. Assertions must be side-effect free, Boolean, and trigger an explicit recovery action on failure — not a crash or silent continuation. Do not write assertions that a static tool can prove always pass or always fail.
- Do not declare data objects at broader scope than their first use requires. No mutable global state.
- Do not ignore return values of non-void functions. Do not write functions that skip validation of all parameters provided by the caller.
- Do not use preprocessor or metaprogramming facilities beyond file inclusion and simple constant definitions: no token pasting, no variadic macro arguments, no recursive macro expansion, no conditional compilation beyond a single top-level feature-detection block. All macros or code-generation constructs must expand to complete syntactic units.
- Do not dereference pointers, references, or indirect accessors more than one level deep per expression without an intermediate named binding. Do not hide dereference operations inside macros, templates, or type aliases. Do not use function pointers or callable-as-data patterns without explicit documented justification.
- Do not commit code with compiler warnings, linter warnings, or static analyzer warnings. Zero warnings is the only acceptable state. Static analysis tooling must be configured and enforced from the first day of development, not retrofitted.

---
*Source: NASA Power of Ten — Rules for Developing Safety Critical Code, Gerard Holzmann, JPL.*

### Applying these rules to a Python daemon/dashboard, honestly

These were written for safety-critical embedded C. Some translate cleanly to
this project; some are structurally incompatible with the architecture already
in the spec, or with Python itself. Naming that precisely, rule by rule, is more
useful than a silent merge that leaves you finding the conflict mid-build.

**Translate directly, no issue:**
- Function length (60 lines, one statement/declaration per line) — real,
  adoptable discipline. `process_post()`, `pull_snapshot()`, `classify()` should
  all stay well under this; if a build produces something longer, split it.
- Ignoring return values / skipping parameter validation — no conflict.
- Preprocessor/metaprogramming restrictions — Python has no preprocessor; the
  nearest analogs (decorators, metaclasses, dynamic class generation) aren't
  needed anywhere in this project, so the rule is trivially satisfied.
- Zero warnings, static analysis from day one — fully adoptable. Configure
  `ruff` (and `mypy` if you want type-checking) at project start, not after.

**Need a specific translation, not a literal reading:**
- *"No exception mechanisms used as control-flow substitutes."* Python's error
  model **is** exceptions — `try/except` around IBKR pulls, JSON parsing, and
  network calls isn't a control-flow hack, it's the mechanism the spec's own
  "fail open, not closed" convention requires. Read this rule as: don't use
  exceptions to implement what should be an `if`/`return` (e.g. don't
  `raise` to jump out of a loop when `break` would do). Do use them for actual
  error handling — that's compliant, not a violation.
- *"At least two assertions per function... trigger an explicit recovery
  action, not a crash."* Python's `assert` keyword does the opposite of this by
  default: it raises `AssertionError` (a crash, not a recovery), and it's
  stripped entirely when running under `python -O`. Satisfying the actual
  intent here means **not** using the `assert` keyword and instead writing
  explicit `if not condition: handle_and_return(...)` guards. Worth getting
  right up front — a build full of bare `assert` statements would silently do
  nothing under `-O` and wouldn't recover from anything even without it.
- *"No function pointers / callable-as-data without documented
  justification."* First-class functions are load-bearing throughout this
  project — `classify_with_haiku` passed as a value, Streamlit's
  callback-driven widgets, `ib_async`'s own event-driven callback pattern.
  Treat this rule as satisfied by default for those idioms rather than
  requiring per-instance justification comments; reserve actual scrutiny for
  cases where a callable is being passed around in a way that obscures what
  will actually run.
- *"No dereference more than one level deep without a named binding."* Python
  has no pointers; the nearest analog is attribute/dict chaining
  (`resp.content[0].text`, `post.get("content", "")`). Worth applying loosely
  for readability, not as a hard gate — this codebase will have short chains
  like that throughout and they're fine.

**Doesn't map cleanly onto this project — noted plainly rather than glossed
over:**
- *"No loop without a statically determinable upper bound."* The daemon's
  `while True:` poll loop is unbounded by design — that's what makes it a
  daemon. Applies fine to **inner** loops (new posts in a batch, categories,
  snapshot rows), and most of those already have explicit bounds
  (`BACKFILL_N = 20`, category count is small and user-controlled, IBKR's own
  row caps bound bar counts). The outer service loop just isn't the kind of
  loop this rule is describing.
- *"No dynamic memory allocation after initialization; no growing
  collections/buffers in hot paths."* A fixed-memory-budget rule for languages
  with manual memory management. Doesn't map onto Python at all — the
  interpreter allocates dynamically for every dict insert, list append, and
  string concat regardless of what's written. The part worth carrying
  forward: don't let unbounded data pile up in long-lived *in-memory* process
  state — don't cache every post's full text in a growing global list across
  the daemon's lifetime, write it to SQLite and let the reference go.
- *"No mutable global state."* Conflicts with the hot-reload cache pattern the
  spec relies on — `categories.json` and `ibkr_settings.json` are read into a
  module-level cache dict keyed by mtime, specifically so edits apply without
  a daemon restart. That's mutable global state, deliberately. Practical
  version of the rule: keep it to one clearly-named object per concern (which
  the cache dicts already are) instead of scattering ad hoc globals.

## Environment

Conda environment `truthmon`, Python 3.14+.

## Stack

- Python 3.14, conda env `truthmon`
- `truthbrush` — **w2rc fork**, not the archived PyPI package (`pip install
  --force-reinstall git+https://github.com/w2rc/truthbrush`)
- `ib_async` — real IB Gateway, classic socket API. Not `ib_insync`: its
  `eventkit` dependency breaks at import time on Python 3.14 (`eventkit.util`
  calls `asyncio.get_event_loop()` at module load, which now raises instead of
  auto-creating a loop). `ib_async` depends on `aeventkit`, a fork that fixes
  this. If a stray `ib_insync`/`eventkit` ever ends up in user site-packages
  (e.g. from a `pip install --user` run outside this conda env), it can shadow
  the env-local one across every same-version interpreter — uninstall it, don't
  patch around it.
- `anthropic` SDK — classification, `claude-haiku-4-5-20251001`
- `streamlit` — dashboard
- `sqlite3`, WAL mode
- `python-dotenv` — all config/secrets

## Architecture, one paragraph

Three pieces, communicating only through files/DB, never in-process:
`truth_monitor.py` (daemon — poll, classify, alert, persist, schedule IBKR
follow-up pulls), `app.py` (Streamlit — category manager, live feed, on-demand
IBKR button), `ibkr_reaction.py` (connect-pull-disconnect snapshot function,
called identically by both of the above). Full detail, including why it's
shaped this way, is in the spec.

## Hard constraints — don't relitigate these

These came from real corrections made while speccing this project. Don't
rederive or second-guess them without a specific reason.

- **IBKR is the classic socket API via `ib_async`, not the Web API/CPAPI REST
  gateway.** Sub-minute bars (`"1 secs"`) only exist on this path.
- **Every IBKR connection is connect → pull → disconnect, every single call.**
  Never held open, never stored in Streamlit `session_state`. Pulls run **async**.
- **Two pull modes:** `pre` = 60 sec ending at `created_at` (before the post);
  `post` = last 60 sec ending at now (after the post). Stitch segments into one
  time series for the sparkline.
- **IBKR config in `.env`:** `IBKR_HOST`, `IBKR_PORT`, `IBKR_CLIENT_ID_DAEMON`,
  `IBKR_CLIENT_ID_DASHBOARD`.
- **Follow-up cadence:** wait 1 min (default), pull another post segment, repeat
  for `followup_duration_min` (default 20). Manual button any time after.
- **Instrument is `ContFuture('ES', 'CME')`, not SPY.** Needed for near-24/5
  coverage matching when posts actually happen and get reacted to
  internationally. Don't substitute SPY or a specific-expiry `Future()`.
- **Distinct `clientId` per component** via `IBKR_CLIENT_ID_DAEMON` /
  `IBKR_CLIENT_ID_DASHBOARD` in `.env`.
- **`categories.json` and `ibkr_settings.json` are hot-reloaded by mtime**,
  edited only through the Streamlit sidebar, never written by the daemon.
- **Classification is one LLM call per post**, multi-label against the active
  category list, capped at 3 matches, retweets flagged (`(retweet)` prefix on the
  reason) not excluded. Prompt template is in the spec — use it verbatim. Model
  ID is a config default, not locked.

## Conventions

- Minimal, hardcoded scripts. No argparse. No abstraction layers the current
  scope doesn't need.
- SQLite WAL mode. Daemon is the only writer; dashboard opens `mode=ro`.
- All config/secrets via `.env` (`python-dotenv`) — nothing hardcoded, nothing
  committed. Includes IBKR host/port/client IDs and `TRUTH_DATA_DIR` (must not be
  inside OneDrive/Dropbox).
- Fail open, not closed: a classification error or a failed IBKR pull records
  the failure and moves on — never crashes the daemon, never blocks or delays
  an alert.

## Verify before trusting the automation

- `endDateTime=''` ("now") across ES's weekend-close and daily maintenance-break
  windows — confirm behavior with a couple of manually-timed test pulls before
  the scheduler leans on it unattended.
- `ContFuture` rollover near a quarterly roll date (Mar/Jun/Sep/Dec), once real
  data exists — check for an artificial jump in the stitched series.
- Exact `reqHistoricalData` parameter behavior against the actually-installed
  `ib_async` version, rather than assuming it matches the spec's code exactly.

## Explicitly out of scope for this build

Phase 2 (embedding-based reaction profile, k-NN estimate over historical
`market_snapshots`) is fully spec'd but deferred. Don't build it alongside the
core app — it needs months of accumulated data to be worth anything.

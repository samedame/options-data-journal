# VERIFY

What was verified, how, and what could **not** be verified from this machine (`UNVERIFIED-LIVE`, each with the exact command to run).
Verification standard: pre-stated acceptance criteria, an independent re-implementation of everything numerical, honest reporting of gaps.

## Acceptance gate results

| # | criterion | result | evidence |
|---|---|---|---|
| 1 | `make verify` (ruff + ruff format + actionlint + mypy strict + pytest) | **PASS** | 551 passed, 10 skipped, 0 failed; mypy: no issues in 45 source files; ruff clean. The 10 skips are IV round-trips that are *intentionally* skipped where the option price is insensitive to vol (deep-ITM, ≤7 DTE), i.e. IV is ill-conditioned by construction. |
| 2 | `make selftest` offline | **PASS** (20/20 checks, ≈13 s) | `tests/test_selftest.py` additionally patches `socket.create_connection` to raise, proving no network use. |
| 3 | Live `odj snapshot --underlying SPY --dry-run` | **PASS** (after-hours data; see below) | row count, usable %, flags, parquet size recorded below. |
| 4 | Every workflow syntactically valid; every issue form loads | **PASS** | `actionlint 1.7.12` exits 0 on all 7 workflows; `tests/test_repo_files.py` parses every YAML, requires a `permissions:` block and `timeout-minutes`, forbids untrusted `${{ github.event.* / inputs.* }}` inside `run:` scripts, loads the 3 issue forms, and asserts form field labels == the parser's expected headings and dropdown options == schema enums. |
| 5 | README zero→working in ~10 min, incl. fake alert + fill + `odj reset-test-data` | **PASS locally / UNVERIFIED-LIVE on GitHub** | The CLI/ingest/reset path is covered by `tests/test_cli.py` and `tests/test_ingest.py`. The GitHub-side clicks (labels, issue form UI, dispatch with your token) need you: see U5–U7. |
| 6 | `INTEGRATION.md`, `BOTDECK_HANDOFF.md`, `DECISIONS.md`, `VERIFY.md` exist and are specific | **PASS** | `tests/test_repo_files.py::test_docs_exist_and_are_specific` asserts length and required specifics. |

### Live snapshot (acceptance item 3)

`/Users/samputzer/options-data-journal$ .venv/bin/python -m odj snapshot --dry-run` run 2026-11-04 00:20–00:21 ET (market **closed**, so `market_open_at_fetch=false`; quotes are the closing/after-hours Yahoo values), dry-run = nothing written, nothing committed:

| underlying | rows | usable | parquet (zstd-9) | flag counts |
|---|---|---|---|---|
| SPY | 2,758 | **94.96 %** | **164,889 B** | zero_bid 139, zero_ask 15, crossed 0, wide_spread 0, stale_last 2,758 (all last trades were from the previous session), iv_divergence 120, parity_violation 144, missing_oi 0 |
| QQQ | 2,581 | 86.94 % | 157,017 B | zero_bid 104, zero_ask 1, wide_spread 271, stale_last 2,581, iv_divergence 262, parity_violation 4 |
| IWM | 1,430 | 97.20 % | 92,838 B | zero_bid 40, zero_ask 4, stale_last 1,430, iv_divergence 56, parity_violation 210 |

Also fetched live and recorded in the market file: VIX 20.83, VIX9D 21.96, VIX3M 21.27, VXN 30.53, `^IRX` 4.025 % (→ rate 0.04025), dividend yield SPY 0.75 % / QQQ 0.26 % / IWM 0.72 % (`trailingAnnualDividendYield`). A one-day parquet for the three underlyings ≈ 0.83 MB for both slots.

### How the numerical parts were verified

* **Greeks/price:** closed-form vectorized BSM vs an independent scalar `math.erf` implementation (price, ≤1e-9) and central finite differences (delta/gamma/vega/theta) across a 7 strike × 5 expiry (1 d … 2 y) × 4 vol × call/put grid, plus zero-vol, expiry, put-call-parity and vectorization tests (`tests/test_bs.py`). Finite-difference steps are scaled to σ√T, otherwise FD truncation error (not the code) fails the near-expiry cases.
* **IV solver:** price→IV→price round trips (≤2e-4 vol), plus impossible prices (below intrinsic, above spot, T=0, NaN) → NaN.
* **Quality flags:** each flag has a positive and a negative test; parity has a clean chain (no flags) and a chain with one known broken strike (exactly that pair flagged).
* **Slippage / P&L:** hand-computed credit spread (worse-than-alert partial fill), debit straddle, iron condor, equity multiplier, partial exits with pro-rata fee allocation.
* **Shadow tracker:** profit target, stop, DTE exit, time exit (days & ISO), expiry worthless / partial-ITM settled / max loss, unmarkable (missing leg, stock leg, multi-expiry, zero price, no settlement snapshot), `mid` vs `conservative`, invalid-quote gaps, pre-alert snapshots ignored, priority order, and the documented **known miss** for an intraday-only touch.
* **Calibration:** known-correlation synthetic data recovers ρ with a CI excluding 0; pure noise gives a CI including 0 and the "NOT demonstrated" verdict; `INSUFFICIENT SAMPLE` fires below `min_sample` and not at it; bootstrap reproducible by seed.
* **Idempotency/concurrency:** same slot twice ⇒ byte-identical `data/` tree; compaction lossless (frame equality) and idempotent; 4 processes × 25 appends ⇒ 100 intact lines; 8 simultaneous identical alerts ⇒ 1 record; **4 git clones pushing concurrently** ⇒ every record and every heartbeat entry survives.
* **Injection safety:** shell metacharacters, command substitution, markdown/HTML tricks, template syntax and front-matter lookalikes in an issue body are stored byte-for-byte as inert text, never echoed into the reply comment, and create no files; a duplicated known heading inside free text is rejected; non-owner issues are ignored with no comment.
* **Calendar:** holiday (Thanksgiving, Jul-3 observed), weekend, half-day (13:00 close ⇒ 12:50 slot), both DST transition weekends (2026-03-08, 2026-11-01), and the "wrong-offset cron exits quietly" behaviour on both sides.

## UNVERIFIED-LIVE checklist (run these; tick what passes)

| id | what | exact command / action | why I could not verify it |
|---|---|---|---|
| U1 | Snapshot with **market-hours** quotes (bid/ask populated; broken-chain thresholds behave) | on a trading day 10:00–15:45 ET: `cd ~/options-data-journal && .venv/bin/python -m odj snapshot --underlying SPY --dry-run` — expect `usable` well above 90 %, `market_open_at_fetch` true, no "broken chain" | verified only on after-hours data |
| U2 | **PARTLY VERIFIED** – Yahoo works from **GitHub-hosted runners** (cloud IPs are sometimes throttled): a real run fetched SPY chain, VIX family, ^IRX and dividend yield in 37 s (see below). Still unverified: *market-hours* quotes from the cloud. | `gh workflow run snapshot.yml --repo samedame/options-data-journal -f slot=open -f ignore_window=true -f force=true && gh run watch` | cannot execute Actions from here |
| U3 | `open-scheduler` snippet works | paste README step 3, then `wrangler tail` at 09:45 ET; or trigger once by hand: `gh workflow run snapshot.yml -f slot=close -f ignore_window=true` | I cannot see/run the Worker |
| U4 | **Robinhood CSV parser** against your real export (column names, trans codes, description format, amounts are hypotheses) | `cd ~/options-data-journal && .venv/bin/python -m odj import-robinhood ~/Downloads/<your-activity>.csv --dry-run` then without `--dry-run`; review `journal/unmatched.json` and `odj unmatched list` | needs your real data |
| U5 | **VERIFIED with `gh auth token`** (OAuth token, not a fine-grained PAT) – still unverified: which fine-grained permission GitHub requires. End-to-end `repository_dispatch` with your token (and which fine-grained permission GitHub actually requires) | README step 5: `JOURNAL_REPO=samedame/options-data-journal JOURNAL_DISPATCH_TOKEN=<fine-grained PAT> .venv/bin/python src/odj/journal/client.py examples/test_alert.json` → Actions → *ingest-alert* green, Discord line | needs a token |
| U6 | **VERIFIED via `gh issue create` with the form's exact markdown** (valid ⇒ journaled, commented, closed; invalid ⇒ commented, open, nothing written; shell/markdown injection stored inert). Not clicked through the real form UI. Issue-form flow in the GitHub UI (labels, `[fill]` title gate, comment with slippage, auto-close, **error leaves the issue open**) | Issues → New → *Log a fill* (Alert ID `readme-test-bot-20261103T150000Z-1`, Contracts 1, Net price 0.40, tick Test record); then a second one with Contracts `two` and confirm the error comment + issue still open | GitHub UI |
| U7 | **VERIFIED** (3 simultaneous dispatches ⇒ 3 records, 6 commits, no loss). Three alerts in the same second all land | `.venv/bin/python - <<'PY'`<br>`import threading; from odj.journal.client import build_alert, leg, send_alert`<br>`go=lambda n: print(n, send_alert(build_alert("readme-test-bot","CONC","SPY","put_credit_spread",[leg("sell","put",500,"2026-12-18"),leg("buy","put",495,"2026-12-18")],0.45,n=n,test=True)))`<br>`[t.start() for t in (ts:=[threading.Thread(target=go,args=(n,)) for n in (1,2,3)])]; [t.join() for t in ts]`<br>`PY`<br>then `git pull` and count 3 new lines in `journal/alerts/`; clean up with `odj reset-test-data --yes` | needs GitHub + token |
| U8 | Discord formatting/rate limiting with your webhook | `DISCORD_WEBHOOK_URL=<url> .venv/bin/python -m odj notify-failure "test failure"` | needs your webhook |
| U9 | Watchdog + weekly report on GitHub | `gh workflow run watchdog.yml`; `gh workflow run weekly-report.yml -f force=true` | needs Actions |
| U10 | Alpha Vantage `odj backfill` (response shape written from AV's docs, never run) | `ALPHAVANTAGE_API_KEY=<key> .venv/bin/python -m odj backfill --start 2026-10-01 --end 2026-10-05 --underlying SPY` | no key; premium endpoint |
| U11 | Compaction on a real month, and its effect on repo size | after the first full month: `.venv/bin/python -m odj compact --month 2026-11 && .venv/bin/python -m odj size-check` | no real month of data yet |
| U12 | **VERIFIED** – CI green on ubuntu-latest (Python 3.12, pandas 3.1.0, numpy 2.5.4): `make verify` + selftest in 1 m 15 s. Pinned versions install on the Ubuntu runner (pandas 3.1 / numpy 2.5 wheels) | first push of `ci.yml`: `gh run list --workflow ci.yml` | status recorded in the section below when known |

## GitHub-side checks performed after push (2026-11-04, real private repo `samedame/options-data-journal`)

All test records were tagged `test: true` and removed with `odj reset-test-data`; the test snapshot and heartbeat were deleted so the first real slot (Wed 2026-11-04 09:45 ET) is not skipped as "already exists".

| check | result |
|---|---|
| `ci.yml` on push (twice) | green on ubuntu-latest; no Node-20 deprecation annotation after bumping to `checkout@v5` / `setup-python@v6` |
| 3 simultaneous `repository_dispatch` alerts (client thread-pool, `gh auth token`) | 3 `ingest-alert` runs, all green (35-48 s); all 3 records in `journal/alerts/2026-11.jsonl`; `status/heartbeat.json` and `data/summary/latest.json` consistent; 6 commits (data + "regenerate derived files") – the race handling works on real GitHub |
| Issue `[fill] live test (valid)` (form markdown, Alert ID + contracts + net price, injection text in Notes) | run green; fill `fill-gh1` journaled; comment: *entry slippage −0.05/share = −5.00 USD/contract, −11.1 % of alert premium*; issue auto-closed; the note `$(echo pwned) \`id\` [x](javascript:alert(1))` stored verbatim and inert |
| Issue `[fill] live test (invalid contracts)` ("one") | run red (by design); comment `'Contracts' must be a whole number, got 'one'`; issue left **open**; nothing written |
| `snapshot.yml` dispatch (`slot=close underlying=SPY ignore_window=true`) | green in 37 s: gate → checkout → install → snapshot → publish. Yahoo served the chain to the cloud runner (2,758 rows, rate from `^IRX`, dividend yield from `info`). |

### Finding worth knowing: Yahoo clears bid/ask overnight

At 00:20 ET the local dry-run showed 94.96 % usable rows (real bid/ask, after-hours); at 00:36 ET both the cloud run **and** an immediate local re-run showed 2,735 of 2,758 rows with zero bid *and* zero ask (0.83 % usable). It is time-dependent, not an IP block. Consequences, all handled: (1) the broken-chain rejection is skipped when the market is closed, so such a snapshot is **stored but flagged** (`usable_pct` 0.8) and the run posts a Discord warning when < 50 % of rows are usable; (2) both slots are in market hours / right after the close, where quotes exist; (3) the watchdog does not know about data quality, only that the job ran – check `data_collection.*.last_usable_pct` in the summary (BotDeck shows it). **Do not run `odj snapshot` by hand late at night and expect usable data.**

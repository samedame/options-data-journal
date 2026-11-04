# options-data-journal

Two jobs in one private repo, built for alert-only bots + manual execution on Robinhood:

* **Module A – chain snapshotter.** Twice per trading day (09:45 and 15:50 New York time) it captures the
  SPY / QQQ / IWM option chains (plus any contract an open alert references) into parquet, with
  Black-Scholes IV/greeks and quality flags, so you finally have a historical chain dataset to backtest on.
* **Module B – fill / slippage / edge-survival journal.** Bots send their alerts here; you log fills, exits and skips
  (phone-friendly issue forms, CLI, or a Robinhood CSV). A shadow tracker replays each alert's exit rules on the
  snapshots, and weekly reports tell you how much of the theoretical edge survives your real execution – and whether the
  bots' 1–100 confidence scores mean anything.

> **Data limits, read once.** Yahoo Finance (yfinance) data is **delayed and indicative, not an exchange feed**. Bid/ask can be
> zero or stale off-hours (rows are flagged, never silently dropped). Marks are taken **twice a day**, so an intraday touch
> of a profit target or stop is **missed** by the shadow tracker. Every report says so in its footer.

## Setup (≈10 minutes)

The repo already exists locally; if `gh` was not logged in when it was built, create and push it yourself:

```bash
cd ~/options-data-journal
gh repo create samedame/options-data-journal --private --source . --remote origin --push
```

1. **Secrets** (Settings → Secrets and variables → Actions, or the CLI – you will be prompted for the value, nothing is written to disk):

   ```bash
   gh secret set DISCORD_WEBHOOK_URL --repo samedame/options-data-journal
   gh secret set ALPHAVANTAGE_API_KEY --repo samedame/options-data-journal   # optional: only for `odj backfill`
   ```

2. **Enable Actions** (Settings → Actions → General → "Allow all actions"; private repos have them on by default) and create the
   three labels the issue forms use (GitHub does not auto-create them):

   ```bash
   for l in log-fill log-exit log-skip; do gh label create "$l" --repo samedame/options-data-journal --color 0E8A16 || true; done
   ```

3. **Scheduler.** Paste this into your `open-scheduler` Cloudflare Worker (I cannot see that Worker's code, so this is generic –
   adapt the names). Create a fine-grained PAT limited to **only this repository** with **Actions: Read and write**,
   and store it as a Worker secret: `wrangler secret put JOURNAL_ACTIONS_TOKEN`.

   `wrangler.toml` (UTC; both DST offsets of each slot – the workflow/script exits quietly when the New York time is outside the slot window):

   ```toml
   [triggers]
   crons = ["45 13 * * 1-5", "45 14 * * 1-5", "50 19 * * 1-5", "50 20 * * 1-5"]
   ```

   Worker code:

   ```js
   const JOURNAL = "samedame/options-data-journal";
   const SLOT_BY_CRON = {
     "45 13 * * 1-5": "open", "45 14 * * 1-5": "open",
     "50 19 * * 1-5": "close", "50 20 * * 1-5": "close",
   };
   function nyMinutes() { // minutes since midnight, New York time
     const p = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", hour: "2-digit", minute: "2-digit", hourCycle: "h23" })
       .formatToParts(new Date()).reduce((a, x) => (a[x.type] = x.value, a), {});
     return Number(p.hour) * 60 + Number(p.minute);
   }
   // Optional cost guard: only dispatch for the DST offset that is actually inside the slot window.
   const WINDOW = { open: [9 * 60 + 40, 10 * 60 + 30], close: [12 * 60 + 45, 16 * 60 + 30] }; // close also covers the 12:50 half-day slot
   async function dispatchSnapshot(env, slot) {
     const [lo, hi] = WINDOW[slot], now = nyMinutes();
     if (now < lo || now > hi) return;
     const r = await fetch(`https://api.github.com/repos/${JOURNAL}/actions/workflows/snapshot.yml/dispatches`, {
       method: "POST",
       headers: { Authorization: `Bearer ${env.JOURNAL_ACTIONS_TOKEN}`, Accept: "application/vnd.github+json",
                  "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "open-scheduler" },
       body: JSON.stringify({ ref: "main", inputs: { slot } }),
     });
     if (r.status !== 204) console.error("journal dispatch failed", r.status, await r.text());
   }
   // inside your existing scheduled(event, env, ctx):
   //   const slot = SLOT_BY_CRON[event.cron]; if (slot) ctx.waitUntil(dispatchSnapshot(env, slot));
   ```

   The Worker triggers `snapshot.yml` through its `workflow_dispatch` endpoint with a `slot` input. The repo also has its own cron fallback in `snapshot.yml`; running both is safe because snapshots are idempotent per
   (underlying, date, slot).

4. **First live check** (no commit, no network writes):

   ```bash
   cd ~/options-data-journal && make install            # once: creates .venv with pinned deps
   .venv/bin/python -m odj snapshot --underlying SPY --dry-run
   ```

5. **Log a first fake alert and fill, then delete them.** `examples/test_alert.json` is tagged `"test": true`.

   ```bash
   export JOURNAL_REPO=samedame/options-data-journal JOURNAL_DISPATCH_TOKEN="$(gh auth token)"   # your own token is fine for this one-off
   .venv/bin/python src/odj/journal/client.py examples/test_alert.json          # sends it through repository_dispatch
   ```

   Watch Actions → *ingest-alert* (and a one-line Discord confirmation). Then on your phone or the web: **Issues → New issue → "Log a fill"**
   with Alert ID `readme-test-bot-20261103T150000Z-1`, Contracts `1`, Net price per share `0.40`, tick **Test record**. The
   workflow comments the computed slippage (0.45 alert → 0.40 fill = −5.00 USD/contract) and closes the issue.
   Clean up – this removes **only** records tagged `test: true`:

   ```bash
   git pull --rebase && .venv/bin/python -m odj reset-test-data --yes && git add -A && git commit -m "remove test records" && git push
   ```

6. **Copy the client into your bots** and add the dispatch token – see `INTEGRATION.md`.

## Daily use

**Log a fill from your phone** (fastest): GitHub app/website → *New issue* → *Log a fill* → Alert ID, contracts, net price per share
(**credit positive, debit negative**) → submit. Leave *Legs* empty and they are copied from the alert. Same for *Log an exit*
(net price is the closing cash flow: closing a credit spread is **negative**, expiry worthless is `0`) and *Log a skipped alert*.
Mistakes are commented on the issue, nothing is written, and the issue stays open.

**CLI** (desktop): `odj fill add --alert-id <id> --contracts 2 --net 0.85 --fees 1.30`, `odj exit add --fill-id fill-gh41 --contracts 2 --net -0.20 --reason profit_target`,
`odj skip add --alert-id <id> --reason risk_gate`. Add `--dry-run` to validate without writing, `--test` to tag a record as test data.

**Robinhood CSV:** `odj import-robinhood ~/Downloads/activity.csv --dry-run`, then without `--dry-run`. Unmatched or ambiguous items land in
`journal/unmatched.json` (`odj unmatched list`, `odj unmatched resolve <id> --alert-id <alert>` or `--no-alert`). Re-importing is a no-op.
*The parser is a hypothesis until you run it on your real export – see `VERIFY.md`.*

**Reports:** `reports/weekly/YYYY-Www.md|json` every Sunday evening ET (also on Discord). `data/summary/latest.json` is the stable file BotDeck reads
(`BOTDECK_HANDOFF.md`). Every metric shows `n`; below `min_sample` (30) it is labelled **INSUFFICIENT SAMPLE** and means nothing yet.

**Backtests:**

```python
from odj.snapshot.store import load_chains, load_market
chains = load_chains("SPY", "2026-11-01", "2026-12-31", dte_min=20, dte_max=45, right="P")  # usable rows only
mkt = load_market("2026-11-01", "2026-12-31")                                            # vix, vix9d, vix3m, vxn, rate
```

`odj export --format csv|parquet --underlying SPY --start 2026-11-01 --end 2026-11-30` writes to `exports/` (git-ignored).

## Layout

| path | what |
|---|---|
| `config/tickers.yml`, `config/journal.yml` | all thresholds, slots, filters, enums, Robinhood column map |
| `src/odj/snapshot/` | Module A (providers, BSM, quality flags, store, compaction, export, backfill) |
| `src/odj/journal/` | Module B (store, issue forms, Robinhood import, slippage, shadow tracker, metrics, reports, summary, **client.py**) |
| `data/chains/{U}/{date}/{HHMM}.parquet` (+`.meta.json`) | snapshots; `data/market/{date}.json` VIX family, rate, dividend yields |
| `journal/{alerts,fills,exits,skips}/YYYY-MM.jsonl` | append-only, schema-validated; `journal/unmatched.json` |
| `data/summary/latest.json`, `status/heartbeat.json`, `reports/weekly/` | derived state |
| `.github/workflows/` | snapshot, ingest-alert, ingest-issue, weekly-report, compact, watchdog, ci |

## Commands

`make install | verify | selftest` · `odj snapshot|compact|size-check|export|backfill` · `odj alert|fill|exit|skip add` ·
`odj import-robinhood|unmatched` · `odj report --weekly|summary|shadow <alert-id>|watchdog` · `odj reset-test-data`.

## GitHub Actions minutes (private repo allowance: 2,000 min/month on the free plan)

Measured on GitHub-hosted runners (this repo's own runs): snapshot job 37 s, alert-ingest 35-48 s, issue-ingest 35 s, CI (install + `make verify` + selftest) 1 m 15 s.
Billing rounds each job up to a whole minute, so a snapshot/ingest job is **1 billed minute** and a CI run is 2. Estimate per month (21 trading days):

| job | runs | ≈ min |
|---|---|---|
| snapshot (2 real + 2 gated-out crons per day; the gated-out ones finish in seconds but still bill 1 min) | 42 + 42 | ~85 |
| alert ingest (assume 10 alerts/day) | 210 | ~210 |
| issue ingest (assume 5/day) | 105 | ~105 |
| watchdog (3 per weekday) | ~65 | ~65 |
| weekly report + compact | ~5 | ~10 |
| CI on pushes (assume 20/month) | 20 | ~40 |
| **total** | | **≈ 515 (≈ 26 % of the allowance)** |

If you Worker-dispatch the snapshots (step 3) the wrong-offset crons never start. If alert volume is much higher than 10/day, ingest dominates.

## Troubleshooting

* **Snapshot job red / Discord "FAILURE":** Yahoo throttles some cloud IPs. The code retries with backoff and rejects broken chains instead of storing them;
  if GitHub runners are blocked persistently, run `odj snapshot` from your own machine on a schedule (it commits through the same `odj ci publish`) – see `VERIFY.md`.
* **Watchdog says a slot is late:** open Actions → *snapshot*; re-run with `slot=open|close`, `force=true`, `ignore_window=true` if you are outside the window.
* **Alert rejected:** the Discord message lists the schema errors; run the same JSON through `odj alert validate --json alert.json` locally.
* **Repo size warning:** see `DECISIONS.md` (storage) – compaction does not shrink `.git`.

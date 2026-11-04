# BOTDECK_HANDOFF

I did **not** touch BotDeck. This file has (a) the contract BotDeck reads and (b) a ready-to-paste Claude Code prompt that builds the page.

## (a) The contract

* **File:** `data/summary/latest.json` in the private repo `samedame/options-data-journal` (branch `main`). It is rewritten after every snapshot and every ingest (alert, fill, exit, skip), so it is at most a few minutes stale.
* **Schema:** `schemas/summary.schema.json` (JSON Schema 2020-12). `schema_version` is `1`; it only changes on a *breaking* change. New optional fields may appear within a version, so read defensively and ignore unknown keys.
* **How to read it with the GitHub contents API** (reuse BotDeck's existing GitHub token/Octokit; if that token is a fine-grained PAT, add this repo with *Contents: Read-only*):

  ```
  GET https://api.github.com/repos/samedame/options-data-journal/contents/data/summary/latest.json
  Authorization: Bearer <token>
  Accept: application/vnd.github.raw+json       # returns the file body itself (no base64 wrapper)
  X-GitHub-Api-Version: 2022-11-28
  ```

  With the default `application/vnd.github+json` the response is `{ "content": "<base64>", "sha": ... }`; decode `content`. In Next.js use a server component / route handler with `fetch(url, { headers, next: { revalidate: 300 } })`.
  Weekly reports: `GET .../contents/reports/weekly` (list) then `GET .../contents/reports/weekly/2026-W45.json` (same headers) – each has the full metrics object and `.md` twin.

* **Shape (abridged; every number below is SYNTHETIC):**

  ```jsonc
  {
    "schema_version": 1,
    "generated_at_utc": "2026-11-18T21:05:00Z",
    "min_sample": 30,
    "bots": { "spy-bps-bot": { "alerts": 12, "fills": 9, "skips": 3, "exits": 6, "open_positions": 3,
                                "last_alert_ts_utc": "2026-11-18T14:35:00Z", "last_fill_ts_utc": "2026-11-18T14:41:00Z" } },
    "open_positions": [ { "fill_id": "fill-gh41", "alert_id": "spy-bps-bot-20261118T143500Z-1", "bot": "spy-bps-bot", "underlying": "SPY",
                          "structure": "put_credit_spread", "contracts_open": 2, "entry_ts_utc": "2026-11-18T14:41:00Z",
                          "entry_net_price": 0.85, "earliest_expiry": "2026-12-18" } ],
    "rolling": {                                        // keys "30d" and "90d"
      "90d": { "window_days": 90, "alerts": 40,
               "take_rate":  { "n": 40, "fills": 30, "skips": 8, "undecided": 2, "take_rate": 0.75, "skip_reasons": {"capital": 5, "risk_gate": 3},
                               "insufficient_sample": false, "label": null },
               "entry_slippage_usd_per_contract": { "n": 30, "mean": -4.1, "insufficient_sample": false },
               "entry_slippage_pct_of_credit":    { "n": 30, "mean": -6.8, "insufficient_sample": false },
               "latency_seconds": { "n": 28, "median_s": 140, "p90_s": 900, "insufficient_sample": true, "label": "INSUFFICIENT SAMPLE" },
               "capture_ratio":   { "n": 18, "capture_ratio": 0.62, "realized_usd": 410, "theoretical_usd": 661, "insufficient_sample": true, "label": "INSUFFICIENT SAMPLE" },
               "slippage_vs_edge": { "n": 18, "ratio": 0.31, "red_flag": false, "plain_english": "Execution is eating 31% of ...", "insufficient_sample": true },
               "unmarkable": 2, "error": null } },
    "data_collection": { "SPY": { "days_collected": 18, "snapshots": 36, "first_date": "2026-11-05", "last_date": "2026-11-30",
                                  "last_snapshot_ts_utc": "2026-11-30T20:51:03Z", "last_snapshot_status": "ok", "last_usable_pct": 94.9 } },
    "heartbeat": { "snapshot": { "last_success_utc": "2026-11-30T20:52:10Z", "last_slot": "2026-11-30/close", "last_status": "ok" } },
    "disclaimer": "Snapshot marks come from delayed, indicative Yahoo Finance data ..."
  }
  ```

  Metric objects always carry `n`, `insufficient_sample` and `label`. **Never present a metric with `insufficient_sample: true` as a conclusion** – grey it out and show the label.
  Richer data (slippage distribution inputs, calibration buckets, skip reasons by bot, capture ratio by bot/strategy/structure/underlying, unmarkable reasons) is in `reports/weekly/<YYYY-Www>.json` → `metrics`.
  Fields available there: `metrics.entry_slippage`, `metrics.latency`, `metrics.exit_slippage`, `metrics.capture_ratio_by_bot|strategy|structure|underlying`,
  `metrics.calibration.buckets[]` (`bucket, n, win_rate, mean_theoretical_pnl, mean_realized_pnl, n_realized`) and `metrics.calibration.rank_correlation` (`rho, ci_low, ci_high, ci_includes_zero`) + `metrics.calibration.verdict`,
  `metrics.take_rate_by_bot`, `metrics.gate_effect`, `metrics.unmarkable{count, reasons, alert_ids}`.

## (b) Prompt to paste into Claude Code, run inside the BotDeck repo

````text
You are working in the BotDeck repo (Next.js on Vercel, deployed at botdeck-theta.vercel.app). Add a new "Execution quality" page.

FIRST, before changing anything: inspect the repo. Read AGENTS.md and the relevant guide in node_modules/next/dist/docs/ (this Next.js version has
breaking changes versus what you remember), then find and reuse (do not reinvent): the existing GitHub integration (how bots.config.ts / lib/* read
other repos through GitHub, which token/env var is used, how errors and rate limits are handled, any caching/revalidate convention), the layout/nav
and where pages are registered, the component and styling conventions (tailwind/CSS modules, cards, tables, charts library if any, dark mode), the test
setup (vitest, playwright, fixtures/) and how existing pages are tested. Match all of these. Do not add a new chart library if one is already used.

DATA CONTRACT (read-only, from a private repo)
- Repo samedame/options-data-journal, file data/summary/latest.json (schema_version 1; schema at schemas/summary.schema.json in that repo).
  Read it through the GitHub contents API with the existing GitHub client: GET /repos/samedame/options-data-journal/contents/data/summary/latest.json with
  Accept: application/vnd.github.raw+json. If BotDeck's token is a fine-grained PAT, tell me to add that repo with Contents: Read-only (do not try to change it yourself).
- Richer detail: latest file in reports/weekly/*.json (list the directory, take the newest YYYY-Www): .metrics has entry_slippage, latency, exit_slippage,
  capture_ratio_by_bot/by_strategy/by_structure/by_underlying, take_rate_by_bot, calibration{buckets[],rank_correlation,verdict}, gate_effect, unmarkable{count,reasons}.
- Every metric object has n, insufficient_sample and label ("INSUFFICIENT SAMPLE"). Treat insufficient_sample:true as "no conclusion": render greyed-out with the label and n, never as a headline number or a green/red verdict.
- Unknown extra keys must be ignored; missing keys must not crash the page (the journal starts empty). Validate the payload with a small zod (or the repo's existing validator) schema and show a clear error state if schema_version !== 1.

PAGE: /execution (nav label "Execution quality"), server-rendered with the repo's existing revalidate convention (about 5 minutes).
Sections:
1. Header strip: generated_at_utc, min_sample, and the disclaimer text from the file (always visible: data is delayed Yahoo, marks are twice daily).
2. Take rate and skip reasons: per bot table (alerts, fills, skips, take rate) from weekly metrics.take_rate_by_bot, plus a skip-reason breakdown (stacked bar or list) from take_rate.skip_reasons.
3. Slippage: entry slippage in USD/contract and % of credit (rolling 30d and 90d from summary.rolling) and, from the weekly report, a distribution view if the data allows
   (otherwise mean/median with n). Negative = worse than the alert: colour and wording must say so. Latency median/p90.
4. Capture ratio by bot (bar chart), with n beside each bar; also strategy/structure/underlying tabs. Show slippage_vs_edge as a gauge/callout and render slippage_vs_edge.plain_english;
   red state only when red_flag is true.
5. Confidence-score calibration: bucket chart (win rate and mean theoretical vs realized P&L per bucket, n on each bar) and the rank-correlation with its CI as a range plot;
   print calibration.verdict verbatim. If the CI includes zero or the sample is insufficient, say so prominently.
6. Data-collection progress: per underlying days_collected, snapshots, first/last date, last_usable_pct, last_snapshot_status, plus heartbeat last_success_utc per job with a staleness warning
   if a job's last success is older than 2 trading days.
7. Warnings panel: unmarkable count with reasons (unmarkable.reasons), every low-sample metric, any rolling[*].error, and open positions table (summary.open_positions).
Empty state (no alerts yet) must be friendly and explain what populates the page.

QUALITY BAR: follow existing lint/format/test conventions; add unit tests for the payload parsing/formatting helpers and a fixture under the repo's fixtures/ folder
(clearly synthetic, e.g. execution-summary.SYNTHETIC.json) used by an e2e/playwright or component test if that is how other pages are tested; no secrets in code; no new env vars
unless unavoidable (then document them). Run the project's lint, typecheck, unit tests and build and fix what you break. Do not modify unrelated pages.
When done, summarise what you reused, what you added, and anything that needs me to do (e.g. extend the GitHub token to the new repo).
````

# INTEGRATION – sending each bot's alerts into the journal

I cannot see your bots' code. Where a bot's internals are unknown I give the snippet in terms of **generic variable names**
(`sym`, `short_k`, `exp`, `credit`, …) and say so. Everything below assumes the alert is sent **right where the bot posts to Discord**,
after the Discord post succeeds (or fails – the journal call never raises into the bot and costs ≤ 8 s).

## 0. One-time setup (≈5 minutes per bot repo)

1. **Create one fine-grained personal access token** (GitHub → Settings → Developer settings → Fine-grained tokens):
   *Resource owner* `samedame`, *Repository access* → **Only select repositories → `options-data-journal`**, and only the permission
   needed to send `repository_dispatch` events. GitHub's REST docs list that as **Repository permissions → Contents: Read and write**
   (verify on the token page; nothing else is needed – no Actions, Issues, Administration). Give it an expiry and a name like `odj-dispatch`.
   Contents-write can also push to that repo, so store it **only** as an Actions secret in the bot repos.
2. In **each bot repo**: Settings → Secrets and variables → Actions → add `JOURNAL_DISPATCH_TOKEN` (and optionally `JOURNAL_REPO` as a variable, default below).
   ```bash
   gh secret set JOURNAL_DISPATCH_TOKEN --repo samedame/<bot-repo>
   ```
3. Expose them to the step that runs the bot:
   ```yaml
   env:
     JOURNAL_REPO: samedame/options-data-journal
     JOURNAL_DISPATCH_TOKEN: ${{ secrets.JOURNAL_DISPATCH_TOKEN }}
   ```
4. **Copy one file** into the bot repo (stdlib only, Python 3.8+):
   ```bash
   cp ~/options-data-journal/src/odj/journal/client.py ~/<bot-repo>/odj_client.py
   ```
   It provides `send_alert(alert, dry_run=None)`, `build_alert(...)`, `leg(...)`, `make_alert_id(...)`. `send_alert` never raises, returns `False` on any failure,
   applies an 8-second timeout, and never logs the token.

## 1. Conventions (the same for every bot)

| field | rule |
|---|---|
| `bot` | the bot's repo-style name, lowercase (`spy-bps-bot`); alert ids are `{bot}-{YYYYMMDDTHHMMSSZ}-{n}`. Use a stable `n` per alert in a run (1, 2, 3 for a ladder) so a retry is idempotent. |
| `strategy` | your own label (`S1`, `S2`, `H8`, …); reports group by it. |
| `net_price` | **per share, credit positive, debit negative** (a $0.90 credit spread = `0.90`; a $42 straddle = `-42.0`). |
| `max_profit`, `max_loss` | **dollars per contract**, positive numbers. `None` if unknown (e.g. long straddle max profit). |
| `profit_target_pct`, `stop_multiple` | target = % of |net_price| gained; stop = loss of `stop_multiple` × |net_price| (so `2.0` = lose 2× the credit). `dte_exit` = calendar days before expiry; `time_exit` = ISO UTC string or integer days held. |
| `confidence` | your 1–100 score, or `None`. |
| `gate` | `regime_gate_state` string such as `"on"` (entries allowed) / `"off"`, or `None`. |
| `basis` | how `net_price` was priced: `mid`, `bid_ask`, `last`, `model`. |

Structure enum: `put_credit_spread, call_credit_spread, iron_condor, long_straddle, long_strangle, long_call, long_put, short_put, covered_call, equity_long, equity_short, other`.
The shadow tracker needs **option legs with expiry and strike** and a single expiry; equity alerts are journaled (take rate, slippage) but are `unmarkable` by design.

## 2. Per bot

> Common import for every snippet: `from odj_client import build_alert, leg, send_alert`

### 2.1 Options scanner `Daily-Stock-Options-Alert` – S1 (RSI-pullback bull put spread) and S2 (IV-rank-gated laddered bull put spread, SPY/QQQ)
*Internals unknown – generic variables.* Structure `put_credit_spread`. S2 emits one alert per ladder rung (`n=i`); put the IV-rank gate in `gate`.
**Place:** immediately after the line that posts the spread to Discord.
```python
send_alert(build_alert(
    bot="daily-stock-options-alert", strategy="S1", underlying=sym, structure="put_credit_spread",
    legs=[leg("sell", "put", short_k, exp), leg("buy", "put", long_k, exp)],   # exp = "YYYY-MM-DD"
    net_price=credit, basis="mid", contracts=qty,
    max_profit=round(credit * 100, 2), max_loss=round((short_k - long_k - credit) * 100, 2),
    profit_target_pct=50, stop_multiple=2.0, dte_exit=21,                      # use the bot's actual rules
    gate=None, notes=f"RSI={rsi:.1f}", n=1))
# S2 ladder: for i, (short_k, long_k, credit, qty) in enumerate(rungs, start=1): ... strategy="S2", gate="on" if iv_rank_ok else "off", n=i, meta={"iv_rank": iv_rank}
```
Env: `JOURNAL_REPO`, `JOURNAL_DISPATCH_TOKEN`.

### 2.2 `spy-bps-bot`
Structure `put_credit_spread`, `bot="spy-bps-bot"`, `underlying="SPY"`. Pass the `vol-regime-gate` state as `gate`. Same snippet as 2.1 with `strategy="BPS"`.
**Place:** where the bot sends its Discord embed.

### 2.3 `Premium-Sleeve` (consolidated SPY short-premium sleeve)
Map each sleeve leg to its structure: `put_credit_spread`, `call_credit_spread`, `iron_condor` (4 legs: sell put, buy put, sell call, buy call) or `short_put`.
`strategy` = the sleeve's own name for the trade type; `gate` = the gate state it checked.
```python
legs = [leg("sell","put",sp,exp), leg("buy","put",lp,exp), leg("sell","call",sc,exp), leg("buy","call",lc,exp)]
send_alert(build_alert(bot="premium-sleeve", strategy=sleeve_name, underlying="SPY", structure="iron_condor", legs=legs,
    net_price=total_credit, contracts=qty, max_profit=round(total_credit*100,2),
    max_loss=round((max(sp-lp, lc-sc) - total_credit)*100, 2), profit_target_pct=50, stop_multiple=2.0, dte_exit=14, gate=gate_state))
```

### 2.4 `tier1-bot` (H8)
*Internals unknown.* If H8 trades a defined-risk spread use the mapping above; if it is anything else use `structure="other"` with its option legs (or `equity_*` for shares) – the journal still tracks take rate, latency and slippage.
Include its confidence/score as `confidence` if it has one.
```python
send_alert(build_alert(bot="tier1-bot", strategy="H8", underlying=sym, structure=structure_enum, legs=legs, net_price=net, contracts=qty,
    max_loss=max_loss_usd_per_contract, confidence=score_or_None, gate=gate_state, notes=reason))
```

### 2.5 Condor / `daytrading_bot`
Structure `iron_condor`, four legs, usually 0–1 DTE. Intraday positions are poorly served by twice-daily marks: expect many `unmarkable` or coarse outcomes (the report says so). Put `time_exit` as an ISO UTC string for the intended exit time and `dte_exit=0`.
```python
send_alert(build_alert(bot="daytrading-bot", strategy="IC-0DTE", underlying="SPY", structure="iron_condor", legs=legs, net_price=credit,
    contracts=qty, max_profit=round(credit*100,2), max_loss=round((wing - credit)*100,2), profit_target_pct=40, stop_multiple=1.5, dte_exit=0,
    time_exit=exit_time_utc_iso, gate=gate_state))
```

### 2.6 Pre-earnings long-straddle scanner
Structure `long_straddle` (same strike & expiry, buy call + buy put) or `long_strangle`. **Debit ⇒ negative `net_price`.** `max_loss` = debit × 100, `max_profit=None`.
Names outside SPY/QQQ/IWM are captured automatically as *pin-only* snapshots from the day the alert arrives (just those two contracts).
```python
send_alert(build_alert(bot="pre-earnings-straddle", strategy="PE-straddle", underlying=sym, structure="long_straddle",
    legs=[leg("buy","call",k,exp), leg("buy","put",k,exp)], net_price=-debit, basis="mid", contracts=qty,
    max_loss=round(debit*100,2), max_profit=None, time_exit=day_after_earnings_close_utc_iso, notes=f"earnings {earn_date}"))
```

### 2.7 `vol-regime-gate`
It does not emit trade alerts, so there is nothing to `send_alert` for it. Integrate it by **passing its state into the downstream bots' `gate=`** (above).
To measure the gate's effect you need outcomes on days the gate said *no*: have the short-premium bots also send the alert they *would* have posted with `gate="off"` and
`strategy="S1-gated"` (a separate strategy label keeps those hypothetical alerts out of the real take-rate).

### 2.8 Apex Chronos (intraday options alerts with a 1–100 score)
Structure `long_call` / `long_put` (or the spread it alerts). Always pass the score: this is what the calibration report evaluates.
```python
send_alert(build_alert(bot="apex-chronos", strategy="chronos", underlying=sym, structure="long_call", legs=[leg("buy","call",k,exp)],
    net_price=-premium, contracts=qty, max_loss=round(premium*100,2), confidence=score, gate=gate_state,
    profit_target_pct=100, stop_multiple=0.5, time_exit=exit_by_utc_iso, notes=signal_name))
```
Intraday signals + twice-daily marks = coarse shadow outcomes; slippage, take rate and score calibration (vs. the theoretical outcome where markable) still work.

### 2.9 IBS callout bot
Equity alert: `structure="equity_long"` (or `equity_short`), one `stock` leg, `net_price = -entry_price` (debit negative; shares), `contracts` = shares. Shadow tracking is `unmarkable` for equities by design; you still get take rate, slippage vs the called price, and latency.
```python
send_alert(build_alert(bot="ibs-bot", strategy="IBS", underlying=ticker, structure="equity_long", legs=[leg("buy","stock")],
    net_price=-called_price, basis="last", contracts=shares, notes=f"IBS={ibs:.2f}"))
```

### 2.10 `frontier5-bot`
*Internals unknown.* Use the generic mapping: spread ⇒ its structure; single option ⇒ `long_call`/`long_put`/`short_put`; anything else ⇒ `other` with the option legs.
```python
send_alert(build_alert(bot="frontier5-bot", strategy=strategy_label, underlying=sym, structure=structure_enum, legs=legs, net_price=net,
    contracts=qty, max_loss=max_loss_usd_per_contract, confidence=score_or_None, gate=gate_state_or_None))
```

## 3. How to test the integration without placing a real alert

* **Dry run (validates and prints, sends nothing, writes nothing):**
  * in code: `send_alert(alert, dry_run=True)`
  * or set `ODJ_DRY_RUN=1` in the bot's environment and run the bot normally
  * or from a shell with a saved alert: `python odj_client.py --dry-run alert.json`
* **Full schema validation** (the authoritative check, run in this repo): `odj alert validate --json alert.json`, or `cat alert.json | odj ingest-alert --dry-run`.
* **End-to-end with a record you can delete:** build the alert with `test=True` (`build_alert(..., test=True)`), send it for real, check Actions → *ingest-alert* and the Discord confirmation,
  then in this repo `odj reset-test-data --yes` removes only `test: true` records.
* An **invalid** alert is rejected twice: locally by the client (`False` + a line on stderr) and, if it gets through, by the journal (Discord failure message, nothing written, workflow red).

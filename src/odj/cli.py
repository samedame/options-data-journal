"""`odj` command line."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any

from odj.common import heartbeat
from odj.common.config import find_root, journal_cfg, tickers_cfg
from odj.common.discord import post as discord_post
from odj.common.schema import ValidationFailed, validate
from odj.common.timeutil import compact_ts, now_utc, parse_ts

log = logging.getLogger("odj")


# ----------------------------------------------------------------------------------- helpers


def _ask(label: str, value: Any, *, required: bool = True, default: Any = None) -> Any:
    if value not in (None, ""):
        return value
    if sys.stdin.isatty():
        raw = input(f"{label}{f' [{default}]' if default is not None else ''}: ").strip()
        if raw:
            return raw
        if default is not None:
            return default
    if required and default is None:
        raise SystemExit(
            f"error: missing required value: {label} (pass the flag or run interactively)"
        )
    return default


def _emit(rec: dict[str, Any], kind: str, args: argparse.Namespace, root: Path) -> int:
    from odj.journal import store

    try:
        if args.dry_run:
            norm = validate(kind, rec, root)
            print(json.dumps(norm, indent=2, sort_keys=True))
            print(f"dry-run: valid {kind}, not written")
            return 0
        stored, created = store.append(root, kind, rec)
    except (ValidationFailed, store.JournalError) as exc:
        errs = exc.errors if isinstance(exc, ValidationFailed) else [str(exc)]
        print("rejected, nothing written:\n  - " + "\n  - ".join(errs), file=sys.stderr)
        return 2
    print(f"{'wrote' if created else 'already present (no change)'}: {kind} {stored['id']}")
    if created:
        try:
            from odj.journal.summary import write_summary

            write_summary(root)
        except Exception as exc:
            log.warning("summary refresh failed: %s", exc)
    return 0


def _common_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--test", action="store_true", help="tag record test:true (removable via reset-test-data)"
    )
    p.add_argument("--dry-run", action="store_true", help="validate and print; write nothing")
    p.add_argument("--notes")
    p.add_argument("--ts", help="ISO-8601 timestamp with timezone (default: now)")


def _tag(rec: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    if args.test:
        rec["test"] = True
    if args.notes:
        rec["notes"] = args.notes
    return rec


# ----------------------------------------------------------------------------------- commands


def cmd_snapshot(args: argparse.Namespace) -> int:
    from odj.snapshot.providers.base import Provider
    from odj.snapshot.run import run_snapshot

    root = find_root()
    provider: Provider
    if os.environ.get("ODJ_PROVIDER") == "synthetic":
        if os.environ.get("ODJ_ALLOW_SYNTHETIC") != "1":
            print("refusing synthetic provider without ODJ_ALLOW_SYNTHETIC=1", file=sys.stderr)
            return 2
        from odj.snapshot.providers.synthetic import SyntheticProvider

        provider = SyntheticProvider()
    else:
        from odj.snapshot.providers import tradier_provider as tp
        from odj.snapshot.providers.yfinance_provider import YFinanceProvider

        cfg = tickers_cfg(root)["snapshot"]["retries"]
        attempts, delay = int(cfg["attempts"]), float(cfg["base_delay_seconds"])
        yf_provider = YFinanceProvider(attempts=attempts, base_delay=delay)
        # ODJ_PROVIDER: auto (default: Tradier first when TRADIER_API_KEY is set, Yahoo otherwise or on any
        # Tradier failure) | tradier (key required, Yahoo only as per-call fallback) | yfinance (Tradier never used).
        choice = os.environ.get("ODJ_PROVIDER", "auto").strip().lower() or "auto"
        if choice not in ("auto", "tradier", "yfinance"):
            print(
                f"unknown ODJ_PROVIDER={choice!r} (use auto, tradier, yfinance or synthetic)",
                file=sys.stderr,
            )
            return 2
        if choice == "tradier" and not tp.configured():
            print("ODJ_PROVIDER=tradier but TRADIER_API_KEY is empty or missing", file=sys.stderr)
            return 2
        if choice != "yfinance" and tp.configured():
            provider = tp.TradierProvider(fallback=yf_provider, attempts=attempts, base_delay=delay)
            print(f"snapshot provider: {provider.feed} first, yfinance fallback")
        else:
            provider = yf_provider
            print("snapshot provider: yfinance (Yahoo; may be delayed)")
    res = run_snapshot(
        root,
        provider=provider,
        slot=args.slot,
        underlyings=args.underlying or None,
        force=args.force,
        dry_run=args.dry_run,
        ignore_window=args.ignore_window or args.dry_run,
        notify=not args.dry_run,
    )
    if res.skipped_reason:
        print(f"snapshot: nothing to do ({res.skipped_reason})")
        return 0
    print(f"snapshot {res.date} slot={res.slot} dry_run={res.dry_run}")
    for w in res.warnings:
        print(f"  warning: {w}")
    for r in res.results:
        extra = ""
        if r.status in ("ok", "dry_run"):
            extra = (
                f" rows={r.rows} usable={r.usable_pct}% parquet={r.parquet_bytes} bytes "
                f"pinned_rows={r.pinned_rows} flags={r.flag_counts}"
            )
        print(f"  {r.underlying}: {r.status}{extra}{' ERROR ' + r.error if r.error else ''}")
    return 0 if res.ok else 1


def cmd_compact(args: argparse.Namespace) -> int:
    from odj.snapshot.compact import compact_all

    res = compact_all(find_root(), month=args.month, keep_daily=args.keep_daily)
    for r in res:
        print(r)
    if not res:
        print("nothing to compact")
    return 0


def cmd_size_check(args: argparse.Namespace) -> int:
    from odj.snapshot.compact import size_report

    rep = size_report(find_root())
    print(json.dumps(rep, indent=2))
    if rep["over_limit"] and args.notify:
        discord_post(
            f"repo size {rep['total_mb']:.0f} MB exceeds {rep['limit_mb']} MB "
            f"(data/ {rep['data_mb']:.0f}, .git {rep['git_mb']:.0f}).",
            "warn",
        )
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    from odj.snapshot.export import export_chains

    out, n = export_chains(
        args.underlying,
        args.start,
        args.end,
        args.format,
        Path(args.out) if args.out else None,
        usable_only=not args.all_rows,
        dte_min=args.dte_min,
        dte_max=args.dte_max,
    )
    print(f"exported {n} rows -> {out}")
    return 0


def cmd_backfill(args: argparse.Namespace) -> int:
    from odj.snapshot.backfill import run_backfill

    res = run_backfill(
        date.fromisoformat(args.start),
        date.fromisoformat(args.end),
        args.underlying or None,
        force=args.force,
    )
    print(res)
    return 0  # never fails CI


def cmd_alert(args: argparse.Namespace) -> int:
    from odj.journal.issue_forms import FormError, parse_legs

    root = find_root()
    if args.action == "validate":
        data = json.load(sys.stdin if args.json == "-" else open(args.json, encoding="utf-8"))
        try:
            validate("alert", data, root)
        except ValidationFailed as exc:
            print("invalid:\n  - " + "\n  - ".join(exc.errors), file=sys.stderr)
            return 2
        print("valid")
        return 0
    if args.json:
        rec = json.load(sys.stdin if args.json == "-" else open(args.json, encoding="utf-8"))
    else:
        bot = _ask("bot", args.bot)
        try:
            legs = parse_legs(
                str(
                    _ask("legs ('sell put 540 2026-11-20; buy put 535 2026-11-20')", args.legs)
                ).replace(";", "\n")
            )
        except FormError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        ts = parse_ts(args.ts) if args.ts else now_utc()
        rules = {
            k: v
            for k, v in {
                "profit_target_pct_of_credit": args.profit_target,
                "stop_loss_multiple_of_credit": args.stop,
                "dte_exit": args.dte_exit,
                "time_exit": args.time_exit,
            }.items()
            if v is not None
        }
        rec = {
            "id": args.id or f"{bot}-{compact_ts(ts)}-1",
            "ts_utc": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "bot": bot,
            "strategy": _ask("strategy", args.strategy),
            "underlying": str(_ask("underlying", args.underlying)).upper(),
            "structure": _ask("structure", args.structure),
            "legs": legs,
            "contracts_suggested": args.contracts,
            "alert_net_price": float(_ask("net price per share (credit +, debit -)", args.net)),
            "alert_price_basis": args.basis,
            "max_profit": args.max_profit,
            "max_loss": args.max_loss,
            "exit_rules": rules,
            "confidence_score": args.confidence,
            "regime_gate_state": args.gate,
        }
    return _emit(_tag(rec, args), "alert", args, root)


def cmd_fill(args: argparse.Namespace) -> int:
    from odj.journal import store
    from odj.journal.issue_forms import FormError, parse_legs

    root = find_root()
    alert_id = args.alert_id or None
    alert = store.get(root, "alert", alert_id) if alert_id else None
    if alert_id and alert is None:
        print(f"error: unknown alert {alert_id}", file=sys.stderr)
        return 2
    try:
        legs = parse_legs(args.legs.replace(";", "\n")) if args.legs else []
    except FormError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not legs and alert:
        legs = [
            {k: leg[k] for k in ("side", "type", "strike", "expiry", "ratio") if k in leg}
            for leg in alert["legs"]
        ]
    if not legs:
        print("error: provide --legs or an --alert-id to copy legs from", file=sys.stderr)
        return 2
    ts = parse_ts(args.ts) if args.ts else now_utc()
    rec = {
        "id": args.id or f"fill-{compact_ts(ts)}-{os.getpid() % 1000}",
        "alert_id": alert_id,
        "ts_utc": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "broker": args.broker,
        "legs_actual": legs,
        "contracts": int(_ask("contracts", args.contracts)),
        "net_price": float(_ask("net price per share (credit +, debit -)", args.net)),
        "fees_total": args.fees,
        "order_type": args.order_type,
        "limit_price": args.limit,
        "source": "cli",
    }
    rc = _emit(_tag(rec, args), "fill", args, root)
    if rc == 0 and alert:
        from odj.journal.slippage import entry_slippage, multiplier_for_legs

        s = entry_slippage(
            float(alert["alert_net_price"]),
            rec["net_price"],
            multiplier=multiplier_for_legs(legs),
            max_loss_usd=alert.get("max_loss"),
        )
        print(f"entry slippage vs alert: {s}")
    return rc


def cmd_exit(args: argparse.Namespace) -> int:
    root = find_root()
    ts = parse_ts(args.ts) if args.ts else now_utc()
    rec = {
        "id": args.id or f"exit-{compact_ts(ts)}-{os.getpid() % 1000}",
        "fill_id": _ask("fill id", args.fill_id),
        "ts_utc": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "contracts": int(_ask("contracts", args.contracts)),
        "net_price": float(
            _ask("net price per share (closing cash flow: debit negative; worthless = 0)", args.net)
        ),
        "fees_total": args.fees,
        "reason": _ask("reason", args.reason, default="manual"),
        "source": "cli",
    }
    return _emit(_tag(rec, args), "exit", args, root)


def cmd_skip(args: argparse.Namespace) -> int:
    root = find_root()
    ts = parse_ts(args.ts) if args.ts else now_utc()
    rec = {
        "id": args.id or f"skip-{compact_ts(ts)}-{os.getpid() % 1000}",
        "alert_id": _ask("alert id", args.alert_id),
        "ts_utc": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "reason": _ask("reason", args.reason, default="other"),
        "source": "cli",
    }
    return _emit(_tag(rec, args), "skip", args, root)


def cmd_ingest_alert(args: argparse.Namespace) -> int:
    from odj.journal.ingest import ingest_alert_event, ingest_alert_payload, write_github_output

    root = find_root()
    if args.event:
        out = ingest_alert_event(root, Path(args.event), notify=not args.no_notify)
    else:
        out = ingest_alert_payload(
            root, sys.stdin.read(), notify=not args.no_notify, dry_run=args.dry_run
        )
    print(out.message)
    write_github_output({"ok": str(out.ok).lower(), "created": str(out.created).lower()})
    return 0 if out.ok else 2


def cmd_ingest_issue(args: argparse.Namespace) -> int:
    from odj.journal.ingest import ingest_issue_event, write_github_output

    root = find_root()
    event = json.loads(Path(args.event).read_text(encoding="utf-8"))
    out = ingest_issue_event(root, event, notify=not args.no_notify)
    ignored = not out.ok and out.errors == ["not owner"]
    if args.comment_file and not ignored:
        Path(args.comment_file).write_text(out.message + "\n", encoding="utf-8")
    write_github_output(
        {
            "ok": str(out.ok).lower(),
            "ignored": str(ignored).lower(),
            "created": str(out.created).lower(),
        }
    )
    print(out.message)
    return 0 if (out.ok or ignored) else 2


def cmd_import_robinhood(args: argparse.Namespace) -> int:
    from odj.journal.robinhood import RobinhoodFormatError, import_csv

    try:
        rep = import_csv(find_root(), Path(args.path), dry_run=args.dry_run)
    except RobinhoodFormatError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(rep.summary())
    for n in rep.notes[:20]:
        print(f"  note: {n}")
    if not args.dry_run:
        try:
            from odj.journal.summary import write_summary

            write_summary(find_root())
        except Exception as exc:
            log.warning("summary refresh failed: %s", exc)
    return 0


def cmd_unmatched(args: argparse.Namespace) -> int:
    from odj.journal.robinhood import load_unmatched, resolve_unmatched
    from odj.journal.store import JournalError

    root = find_root()
    if args.action == "list":
        items = load_unmatched(root)
        for u in items:
            print(f"{u['id']}  [{u['kind']}]  {u['reason']}  candidates={u['candidates']}")
        print(f"{len(items)} item(s)")
        return 0
    try:
        stored = resolve_unmatched(
            root, args.id, alert_id=args.alert_id, fill_id=args.fill_id, no_alert=args.no_alert
        )
    except (JournalError, ValidationFailed) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"journaled {stored['id']}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    from odj.journal.reports import generate_weekly, week_label, weekly_window_ok

    root = find_root()
    if not args.weekly:
        print("error: only --weekly is supported", file=sys.stderr)
        return 2
    now = now_utc()
    if args.require_window and not weekly_window_ok(now, journal_cfg(root)):
        print("report: outside the Sunday-evening window; nothing to do")
        return 0
    res = generate_weekly(
        root, now=now, mark_mode=args.mark_mode, notify=not args.no_notify, force=args.force
    )
    print({k: v for k, v in res.items() if k != "metrics"})
    if res["status"] == "written":
        heartbeat.record(root, "weekly_report", status="ok", slot_key=week_label(now))
        try:
            from odj.journal.summary import write_summary

            write_summary(root)
        except Exception as exc:
            log.warning("summary refresh failed: %s", exc)
    return 0


def cmd_summary(_: argparse.Namespace) -> int:
    from odj.journal.summary import write_summary

    doc = write_summary(find_root())
    print(
        f"wrote data/summary/latest.json ({len(doc['bots'])} bots, {len(doc['open_positions'])} open positions)"
    )
    return 0


def cmd_shadow(args: argparse.Namespace) -> int:
    from odj.journal import store
    from odj.journal.tracking import track_alerts

    root = find_root()
    a = store.get(root, "alert", args.alert_id)
    if a is None:
        print(f"unknown alert {args.alert_id}", file=sys.stderr)
        return 2
    res = track_alerts(root, [a], args.mark_mode)[a["id"]]
    print(json.dumps(res.to_dict(), indent=2))
    return 0


def cmd_watchdog(args: argparse.Namespace) -> int:
    from odj.journal.watchdog import check

    problems = check(find_root())
    if not problems:
        print("watchdog: all jobs on schedule")
        return 0
    for p in problems:
        print(f"PROBLEM: {p}")
    if not args.no_notify:
        discord_post("watchdog:\n- " + "\n- ".join(problems), "fail")
    return 1


def cmd_reset_test_data(args: argparse.Namespace) -> int:
    from odj.journal import store

    root = find_root()
    counts = store.remove_test_records(root, dry_run=True)
    total = sum(counts.values())
    print(f"records tagged test:true -> {counts}")
    if args.dry_run or total == 0:
        return 0
    if not args.yes:
        if (
            not sys.stdin.isatty()
            or input(f"remove {total} test record(s)? [y/N] ").strip().lower() != "y"
        ):
            print("aborted (pass --yes to confirm)")
            return 1
    store.remove_test_records(root)
    print("removed.")
    try:
        from odj.journal.summary import write_summary

        write_summary(root)
    except Exception as exc:
        log.warning("summary refresh failed: %s", exc)
    return 0


def cmd_ci(args: argparse.Namespace) -> int:
    from odj.ci import PublishError, publish

    root = find_root()

    def regen() -> None:
        from odj.journal.summary import write_summary

        write_summary(root)

    try:
        res = publish(
            root, args.message, regen=None if args.no_regen else regen, push=not args.no_push
        )
    except PublishError as exc:
        discord_post(f"commit/push failed: {exc}", "fail")
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(res)
    return 0


def cmd_heartbeat(args: argparse.Namespace) -> int:
    heartbeat.record(
        find_root(), args.job, status=args.status, slot_key=args.slot_key, detail=args.detail
    )
    return 0


def cmd_notify_failure(args: argparse.Namespace) -> int:
    discord_post(args.message, "fail")
    return 0


def cmd_selftest(_: argparse.Namespace) -> int:
    from odj.selftest import run_selftest

    return run_selftest()


# ----------------------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="odj", description="options-data-journal")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("snapshot", help="capture option-chain snapshots")
    s.add_argument("--underlying", action="append")
    s.add_argument(
        "--slot", help="open|close|auto (default auto = whichever slot window we are in)"
    )
    s.add_argument(
        "--force", action="store_true", help="overwrite an existing snapshot for the slot"
    )
    s.add_argument(
        "--dry-run", action="store_true", help="fetch + enrich, report stats, write nothing"
    )
    s.add_argument(
        "--ignore-window",
        action="store_true",
        help="run even outside the slot window / on non-trading days",
    )
    s.set_defaults(fn=cmd_snapshot)

    s = sub.add_parser("compact", help="merge a month of daily snapshots per underlying")
    s.add_argument("--month", help="YYYY-MM (default: all completed months)")
    s.add_argument("--keep-daily", action="store_true")
    s.set_defaults(fn=cmd_compact)

    s = sub.add_parser("size-check")
    s.add_argument("--notify", action="store_true")
    s.set_defaults(fn=cmd_size_check)

    s = sub.add_parser("export", help="export stored chains")
    s.add_argument("--format", required=True, choices=["csv", "parquet"])
    s.add_argument("--underlying", required=True)
    s.add_argument("--start", required=True)
    s.add_argument("--end", required=True)
    s.add_argument("--out")
    s.add_argument("--all-rows", action="store_true", help="include rows flagged unusable")
    s.add_argument("--dte-min", type=int)
    s.add_argument("--dte-max", type=int)
    s.set_defaults(fn=cmd_export)

    s = sub.add_parser(
        "backfill", help="optional Alpha Vantage historical chains (needs ALPHAVANTAGE_API_KEY)"
    )
    s.add_argument("--start", required=True)
    s.add_argument("--end", required=True)
    s.add_argument("--underlying", action="append")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_backfill)

    a = sub.add_parser("alert", help="add/validate alerts")
    asub = a.add_subparsers(dest="action", required=True)
    for name in ("add", "validate"):
        x = asub.add_parser(name)
        x.add_argument("--json", help="alert JSON file ('-' for stdin)")
        if name == "add":
            _common_flags(x)
            x.add_argument("--id")
            x.add_argument("--bot")
            x.add_argument("--strategy")
            x.add_argument("--underlying")
            x.add_argument("--structure")
            x.add_argument("--legs", help="'sell put 540 2026-11-20; buy put 535 2026-11-20'")
            x.add_argument("--net", type=float, help="per share; credit +, debit -")
            x.add_argument("--basis", default="mid", choices=["mid", "bid_ask", "last", "model"])
            x.add_argument("--contracts", type=int)
            x.add_argument("--max-profit", type=float)
            x.add_argument("--max-loss", type=float)
            x.add_argument("--confidence", type=float)
            x.add_argument("--gate")
            x.add_argument("--profit-target", type=float, help="% of credit")
            x.add_argument("--stop", type=float, help="multiple of credit")
            x.add_argument("--dte-exit", type=int)
            x.add_argument("--time-exit")
    a.set_defaults(fn=cmd_alert)

    f = sub.add_parser("fill", help="log an entry fill")
    fsub = f.add_subparsers(dest="action", required=True)
    x = fsub.add_parser("add")
    _common_flags(x)
    x.add_argument("--id")
    x.add_argument("--alert-id")
    x.add_argument("--legs", help="omit to copy the alert's legs")
    x.add_argument("--contracts", type=int)
    x.add_argument("--net", type=float, help="per share; credit +, debit -")
    x.add_argument("--fees", type=float, default=0.0)
    x.add_argument("--order-type", default="limit", choices=["limit", "market"])
    x.add_argument("--limit", type=float)
    x.add_argument("--broker", default="robinhood")
    f.set_defaults(fn=cmd_fill)

    e = sub.add_parser("exit", help="log an exit")
    esub = e.add_subparsers(dest="action", required=True)
    x = esub.add_parser("add")
    _common_flags(x)
    x.add_argument("--id")
    x.add_argument("--fill-id")
    x.add_argument("--contracts", type=int)
    x.add_argument(
        "--net", type=float, help="closing cash flow per share: debit negative, worthless 0"
    )
    x.add_argument("--fees", type=float, default=0.0)
    x.add_argument(
        "--reason",
        choices=[
            "profit_target",
            "stop",
            "dte",
            "expiry_worthless",
            "assignment",
            "manual",
            "other",
        ],
    )
    e.set_defaults(fn=cmd_exit)

    k = sub.add_parser("skip", help="log a skipped alert")
    ksub = k.add_subparsers(dest="action", required=True)
    x = ksub.add_parser("add")
    _common_flags(x)
    x.add_argument("--id")
    x.add_argument("--alert-id")
    x.add_argument(
        "--reason",
        choices=["did_not_see", "risk_gate", "price_moved", "capital", "discretionary", "other"],
    )
    k.set_defaults(fn=cmd_skip)

    s = sub.add_parser("ingest-alert", help="(workflow) ingest a repository_dispatch alert")
    s.add_argument(
        "--event", help="path to $GITHUB_EVENT_PATH; omit to read the alert JSON from stdin"
    )
    s.add_argument("--no-notify", action="store_true")
    s.add_argument("--dry-run", action="store_true", help="stdin mode: validate only")
    s.set_defaults(fn=cmd_ingest_alert)

    s = sub.add_parser("ingest-issue", help="(workflow) ingest an issue-form log entry")
    s.add_argument("--event", required=True)
    s.add_argument("--comment-file")
    s.add_argument("--no-notify", action="store_true")
    s.set_defaults(fn=cmd_ingest_issue)

    s = sub.add_parser("import-robinhood", help="import a Robinhood activity CSV (UNVERIFIED-LIVE)")
    s.add_argument("path")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_import_robinhood)

    s = sub.add_parser("unmatched", help="review journal/unmatched.json")
    usub = s.add_subparsers(dest="action", required=True)
    usub.add_parser("list")
    x = usub.add_parser("resolve")
    x.add_argument("id")
    x.add_argument("--alert-id")
    x.add_argument("--fill-id")
    x.add_argument("--no-alert", action="store_true")
    s.set_defaults(fn=cmd_unmatched)

    s = sub.add_parser("report")
    s.add_argument("--weekly", action="store_true")
    s.add_argument("--mark-mode", choices=["mid", "conservative"])
    s.add_argument("--no-notify", action="store_true")
    s.add_argument("--force", action="store_true")
    s.add_argument(
        "--require-window",
        action="store_true",
        help="exit quietly unless inside the Sunday-evening ET window",
    )
    s.set_defaults(fn=cmd_report)

    sub.add_parser("summary").set_defaults(fn=cmd_summary)

    s = sub.add_parser("shadow", help="show the shadow-tracker outcome for one alert")
    s.add_argument("alert_id")
    s.add_argument("--mark-mode", default="mid", choices=["mid", "conservative"])
    s.set_defaults(fn=cmd_shadow)

    s = sub.add_parser("watchdog")
    s.add_argument("--no-notify", action="store_true")
    s.set_defaults(fn=cmd_watchdog)

    s = sub.add_parser("reset-test-data", help="remove ONLY records tagged test:true")
    s.add_argument("--yes", action="store_true")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_reset_test_data)

    s = sub.add_parser("ci", help="(workflow) commit + push with retries")
    csub = s.add_subparsers(dest="action", required=True)
    x = csub.add_parser("publish")
    x.add_argument("--message", required=True)
    x.add_argument("--no-regen", action="store_true")
    x.add_argument("--no-push", action="store_true")
    s.set_defaults(fn=cmd_ci)

    s = sub.add_parser("heartbeat")
    s.add_argument("job")
    s.add_argument("--status", default="ok")
    s.add_argument("--slot-key")
    s.add_argument("--detail")
    s.set_defaults(fn=cmd_heartbeat)

    s = sub.add_parser("notify-failure", help="(workflow) post a failure line to Discord")
    s.add_argument("message")
    s.set_defaults(fn=cmd_notify_failure)

    sub.add_parser("selftest", help="offline end-to-end run on synthetic fixtures").set_defaults(
        fn=cmd_selftest
    )
    return p


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr
    )
    args = build_parser().parse_args(argv)
    try:
        return int(args.fn(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

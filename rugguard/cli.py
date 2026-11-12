"""Command-line entry point.

    rugguard init-db
    rugguard collect wrap.near [--pools] [--transfers-pages 4] [--no-nearblocks]
    rugguard collect-seeds seeds/tokens.txt
    rugguard surface wrap.near            # live upgrade-surface check only (a few RPC calls)
    rugguard report wrap.near             # latest stored snapshot + scores
    rugguard export holders --out holders.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from .analysis import upgrade_surface
from .analysis.outcome import LABELS, OutcomeRules, cohen_kappa
from .analysis.transfers import from_db_rows
from .collect import CollectOptions, Collector
from .config import Settings
from .http import FetchError
from .postmortem import collect_history, discover_tokens, not_a_token, run_outcome
from .scoring import evaluate, run_score
from .report import run_stamp, write_report
from .analysis.score import ScoreRules
from .sources.rpc import EMPTY_CODE_HASH
from .validate import InvalidAccountId, account_id
from . import logs
from .storage.db import Database

EXPORTABLE = {"tokens", "snapshots", "account_state", "access_keys", "contract_code", "holders",
              "token_stats", "ft_transfers", "deployments", "pools", "risk_scores",
              "history_runs", "outcomes", "outcome_reviews"}

BAND_MARK = {"low": "LOW", "medium": "MEDIUM", "high": "HIGH", "unknown": "UNKNOWN"}


def _open(settings: Settings) -> Database:
    db = Database(settings.db_path)
    db.init()
    return db


def _print_score(title: str, s: dict) -> None:
    print(f"  {title}: {BAND_MARK.get(s['band'], s['band'])}")
    for reason in s["reasons"]:
        print(f"    - {reason}")


def cmd_init(settings: Settings, _: argparse.Namespace) -> int:
    _open(settings)
    print(f"Database ready at {settings.db_path}")
    return 0


def cmd_collect(settings: Settings, args: argparse.Namespace) -> int:
    db = _open(settings)
    collector = Collector(settings, db)
    opts = CollectOptions(transfers_pages=args.transfers_pages, with_pools=args.pools,
                          with_nearblocks=not args.no_nearblocks, nearblocks_stats=args.stats,
                          classify_holders=args.classify_holders)
    pace = settings.min_interval_s.get("nearblocks", 0)
    if pace >= 5 and not args.no_nearblocks:
        print(f"NearBlocks free tier: pacing calls {pace:.0f}s apart "
              "(set NEARBLOCKS_API_KEY or RUGGUARD_NEARBLOCKS_MIN_INTERVAL_S to change).")
    tokens = args.tokens
    if getattr(args, "seed_file", None):
        if not Path(args.seed_file).exists():
            raise UsageError(f"Seed file {args.seed_file} not found.")
        tokens = [t.split()[0] for t in Path(args.seed_file).read_text().splitlines()
                  if t.strip() and not t.strip().startswith("#")]
    tokens = _valid(tokens)
    worst = 0
    for token in tokens:
        res = collector.collect(token, opts)
        print(f"\n{token}  (snapshot {res.snapshot_id}, counts {json.dumps(res.counts)})")
        _print_score("Upgrade surface", res.surface)
        _print_score("Holder concentration", res.concentration)
        if res.errors:
            print(f"  {len(res.errors)} source error(s):")
            for e in res.errors:
                print(f"    ! {e['step']}: {e['error'][:160]}")
            worst = max(worst, 2)
    return worst


def cmd_surface(settings: Settings, args: argparse.Namespace) -> int:
    args.token = _valid([args.token])[0]
    db = _open(settings)
    c = Collector(settings, db)
    errors: list = []
    account = c._try(errors, "view_account", lambda: c.rpc.view_account(args.token))
    keys = c._try(errors, "view_access_key_list", lambda: c.rpc.view_access_key_list(args.token))
    code = c.code_analysis(args.token, account, errors) if account else None
    report = upgrade_surface.assess(args.token, account, keys, code).as_dict()
    if args.json:
        print(json.dumps({**report, "errors": errors}, indent=2))
    else:
        print(args.token)
        _print_score("Upgrade surface", report)
        for e in errors:
            print(f"  ! {e['step']}: {e['error'][:160]}")
    return 0 if not errors else 2


def describe_keys(account: str, keys: list[dict]) -> tuple[str, list[str]]:
    """What an account's keys allow, in plain words, and a one-line verdict for an AI agent's account."""
    lines, full = [], 0
    for k in keys:
        if k["is_full"]:
            full += 1
            lines.append(f"{k['public_key'][:20]}...  FULL ACCESS: can move all funds, deploy code, add keys")
            continue
        methods = ", ".join(k["method_names"]) or "any non-payable method"
        allowance = k.get("allowance")
        gas = "unlimited gas allowance" if allowance is None else f"gas allowance {int(allowance) / 10**24:g} NEAR"
        lines.append(f"{k['public_key'][:20]}...  {k['permission']}: only {methods} on {k['receiver_id']}; "
                     f"{gas}; cannot attach deposits or transfer NEAR")
    if not keys:
        verdict = "No keys: nobody can sign for this account."
    elif full:
        verdict = f"{full} full-access key(s): whoever holds one controls everything on {account}."
    else:
        targets = sorted({k["receiver_id"] for k in keys if k["receiver_id"]})
        verdict = (f"No full-access key: {account} can only call {', '.join(targets)} as listed, "
                   "and cannot move its funds or deploy code on its own.")
    return verdict, lines


def cmd_keys(settings: Settings, args: argparse.Namespace) -> int:
    args.account = _valid([args.account])[0]
    c = Collector(settings, _open(settings))
    errors: list = []
    keys = c._try(errors, "view_access_key_list", lambda: c.rpc.view_access_key_list(args.account))
    if keys is None:
        print(f"Could not read the keys of {args.account}: {errors[0]['error'][:200] if errors else ''}")
        return 2
    verdict, lines = describe_keys(args.account, keys)
    if args.json:
        print(json.dumps({"account": args.account, "network": settings.network, "verdict": verdict,
                          "keys": keys}, indent=2))
        return 0
    print(f"{args.account} ({settings.network}): {len(keys)} key(s)")
    for line in lines:
        print("  " + line)
    print(verdict)
    return 0


def cmd_timeline(settings: Settings, args: argparse.Namespace) -> int:
    """Pool activity in order, with seed and pull marked, for human labelling. Reads stored data only."""
    from .analysis.transfers import from_db_rows
    from . import timeline
    args.token = _valid([args.token])[0]
    db = _open(settings)
    transfers = from_db_rows(db.transfers_for(args.token))
    if not transfers:
        print(f"No stored transfers for {args.token}: run `rugguard outcome {args.token}` first.")
        return 1
    tok = db.query("SELECT decimals, symbol FROM tokens WHERE token_id = ?", (args.token,))
    decimals = tok[0]["decimals"] if tok and tok[0]["decimals"] is not None else None
    if decimals is None:
        print("(Token decimals not stored; amounts are raw units. `rugguard score TOKEN` stores them.)")
    tl = timeline.build(transfers, args.pool, args.min_share, args.pull_share)
    print(f"{args.token}" + (f" ({tok[0]['symbol']})" if tok and tok[0]["symbol"] else ""))
    print(timeline.render(tl, decimals or 0, args.limit))
    return 0


def cmd_report(settings: Settings, args: argparse.Namespace) -> int:
    args.token = _valid([args.token])[0]
    db = _open(settings)
    snap = db.latest_snapshot(args.token)
    if snap is None:
        print(f"No snapshot for {args.token}; run `rugguard collect {args.token}` first.")
        return 1
    print(f"{args.token}  snapshot {snap['snapshot_id']} at {snap['taken_at']} (block {snap['block_height']})")
    for row in db.latest_scores(args.token):
        _print_score(row["kind"], {"band": row["band"], "reasons": json.loads(row["reasons_json"])})
    errs = json.loads(snap["errors_json"])
    if errs:
        print(f"  {len(errs)} source error(s) in this snapshot")
    return 0


def cmd_export(settings: Settings, args: argparse.Namespace) -> int:
    if args.table not in EXPORTABLE:
        print(f"Unknown table. Choose from: {', '.join(sorted(EXPORTABLE))}")
        return 1
    db = _open(settings)
    rows = db.query(f"SELECT * FROM {args.table}")  # table name validated above
    out = open(args.out, "w", newline="") if args.out else sys.stdout
    writer = csv.writer(out)
    if rows:
        writer.writerow(rows[0].keys())
        writer.writerows(tuple(r) for r in rows)
    if args.out:
        out.close()
        print(f"Wrote {len(rows)} rows to {args.out}")
    return 0


class UsageError(Exception):
    pass


def _valid(tokens: list[str]) -> list[str]:
    try:
        return [account_id(t) for t in tokens]
    except InvalidAccountId as exc:
        raise UsageError(str(exc))


def _apply_source(settings: Settings, args: argparse.Namespace) -> None:
    if getattr(args, "source", None):
        settings.history_source = args.source


def _tokens_from(args: argparse.Namespace) -> list[str]:
    tokens = list(args.tokens or [])
    if getattr(args, "seeds", None):
        path = Path(args.seeds)
        if not path.exists():
            raise UsageError(f"Seed file {path} not found. Create it with one token ID per line, "
                             f"or generate one: rugguard discover --out {path}")
        tokens += [t.split()[0] for t in path.read_text().splitlines()
                   if t.strip() and not t.strip().startswith("#")]
    if not tokens:
        raise UsageError("No tokens given: pass token IDs or --seeds FILE.")
    return _valid(tokens)


def _not_a_token(c: Collector, token: str) -> str | None:
    return not_a_token(c, token)


def cmd_rules(settings: Settings, args: argparse.Namespace) -> int:
    print(json.dumps({"score": ScoreRules().as_dict(), "outcome": OutcomeRules.load(args.rules).as_dict()},
                     indent=2))
    return 0


def cmd_score(settings: Settings, args: argparse.Namespace) -> int:
    _apply_source(settings, args)
    db = _open(settings)
    c = Collector(settings, db)
    rules = ScoreRules(cut_hours=args.cut_hours)
    print(f"Score {rules.version}: signals use data up to {rules.cut_hours:g} h after launch")
    tokens = _tokens_from(args)
    for i, token in enumerate(tokens, 1):
        logs.progress(f"[{i}/{len(tokens)}] {token}: scoring...")
        problem = not_a_token(c, token)
        if problem:
            print(f"\n{problem} Skipped; nothing stored.")
            continue
        try:
            r, errors = run_score(c, token, rules, args.page_size, args.max_pages)
        except Exception as exc:          # one bad token must not end a batch
            logs.event("score_crash", token=token, error=repr(exc))
            print(f"\n{token}: ERROR {type(exc).__name__}: {exc}. Skipped; nothing stored.")
            continue
        value = "n/a" if r["score"] is None else f"{r['score']:.0f}/100"
        print(f"\n{token}: {value}  {r['band'].upper()}  (coverage {r['coverage']:.0%})")
        for name, comp in r["facts"]["components"].items():
            sub = "  -  " if comp["subscore"] is None else f"{comp['subscore']:.2f}"
            print(f"  {name:<14} {sub}  x{comp['weight']:<3} {comp['reason']}")
        if r["facts"].get("note"):
            print(f"  note: {r['facts']['note']}")
        for e in errors:
            print(f"  ! {e['step']}: {e['error'][:160]}")
        # Stop only if the source the history comes from is refusing; a refused optional call
        # (e.g. NearBlocks deploy history while history comes from FastNEAR) is reported, not fatal.
        active = "fastnear_tx" if settings.history_source == "fastnear" else "nearblocks"
        if c.client.rate_limited(active):
            name = "FastNEAR" if active == "fastnear_tx" else "NearBlocks (free plan: 6 calls a minute, 333 a day)"
            print(f"\nStopped after {i} of {len(tokens)} token(s): {name} is refusing calls now. Run the same "
                  "command later: everything fetched so far is cached, so it resumes where it stopped.")
            break
    return 0


def cmd_evaluate(settings: Settings, args: argparse.Namespace) -> int:
    db = _open(settings)
    only = set(_tokens_from(args)) if args.seeds else None
    ev = evaluate(db, only, use_human=not args.rules_only)
    if args.rules_only:
        print("Using the rules' labels only (human reviews ignored)")
    if only:
        print(f"Evaluating only the {len(only)} token(s) in {args.seeds}")
    if not ev["rows"]:
        print("Nothing to evaluate: run `outcome` and `score` on some tokens first.")
        return 1
    width = max(len(r["token_id"]) for r in ev["rows"])
    print(f"{'token':<{width}}  {'outcome':<13} {'score':>5}  band")
    for r in sorted(ev["rows"], key=lambda r: (r["score"] is None, -(r["score"] or 0))):
        sc = "  -" if r["score"] is None else f"{r['score']:>5.0f}"
        lbl = r["label"] + ("*" if r["label_source"] == "human" else "")
        print(f"{r['token_id']:<{width}}  {lbl:<13} {sc}  {r['band'] or ''}")
    print(f"\n* = human label. Judged {ev['judged']} token(s) labelled rugged/faded/alive with a score; "
          f"{ev['excluded']} excluded (pending, spoiled, needs_review, or not scored).")
    if ev["rugged"] and ev["judged"] > ev["rugged"]:
        print(f"Caught {ev['caught']} of {ev['rugged']} rugs at score >= {ev['threshold']:.0f} "
              f"(recall {ev['recall']:.0%}); false alarms {ev['false_alarms']}; "
              f"AUC {ev['auc']:.2f} (0.5 = coin flip, 1.0 = perfect ranking).")
        print("Mean score by outcome: " + ", ".join(f"{k} {v}" for k, v in ev["mean_score"].items()))
    else:
        print("Need at least one rugged and one non-rugged token to measure detection.")
    if not args.no_files:
        token_set = Path(args.seeds).stem if args.seeds else "all"
        stamp = run_stamp()
        stem = Path(args.out) if args.out else Path("runs") / f"eval-{token_set}-{stamp}"
        csv_path, md_path = write_report(db, ev, stem, token_set, "rugguard " + " ".join(args.argv), stamp)
        print(f"\nSaved {csv_path} (one row per token) and {md_path} (report, incl. simpler-rule baselines)")
    return 0


def cmd_guard(settings: Settings, args: argparse.Namespace) -> int:
    """Oracle and agent actions on the guard vault contract (testnet), via near-cli-rs."""
    import os
    from . import guard

    vault = args.vault or os.getenv("RUGGUARD_VAULT")
    if not vault:
        raise UsageError("Name the vault contract: --vault ACCOUNT or RUGGUARD_VAULT in .env")
    cli = guard.NearCli(network=args.network, dry_run=args.dry_run)
    try:
        if args.action == "post-score":
            signer = args.oracle or os.getenv("RUGGUARD_ORACLE")
            if not signer:
                raise UsageError("Name the oracle account: --oracle ACCOUNT or RUGGUARD_ORACLE in .env")
            posted = guard.post_score(cli, _open(settings), args.token, vault, signer, args.as_token)
            print(f"Posting {posted['token']}: {posted['score']}/100 ({posted['rules_version']}, {posted['band']})")
        elif args.action == "swap":
            signer = args.agent or os.getenv("RUGGUARD_AGENT")
            if not signer:
                raise UsageError("Name the agent account: --agent ACCOUNT or RUGGUARD_AGENT in .env")
            out = guard.request_swap(cli, vault, signer, args.token, args.amount, args.pool, args.min_out,
                                     args.key_file or os.getenv("RUGGUARD_AGENT_KEY_FILE"))
            print(out.strip())
        elif args.action == "check":
            print(guard.check(cli, vault, args.token, args.amount).strip())
        else:
            print(guard.config(cli, vault).strip())
    except (LookupError, ValueError, InvalidAccountId, RuntimeError) as exc:
        print(str(exc))
        return 2
    if args.dry_run:
        print("Dry run; would run:")
    for line in cli.printed:
        print(("  " if args.dry_run else "$ ") + line)
    return 0


def cmd_history(settings: Settings, args: argparse.Namespace) -> int:
    _apply_source(settings, args)
    db = _open(settings)
    c = Collector(settings, db)
    tokens = _tokens_from(args)
    for i, token in enumerate(tokens, 1):
        logs.progress(f"[{i}/{len(tokens)}] {token}: fetching history...")
        problem = _not_a_token(c, token)
        if problem:
            print(problem)
            continue
        try:
            h = collect_history(c, token, args.page_size, args.max_pages)
        except FetchError as exc:
            print(f"{token}: history failed: {exc}")
            continue
        if h.rows_fetched == 0 and not db.transfers_for(token):
            print(f"{token}: no transfers found on NearBlocks (never distributed, or not indexed).")
            continue
        state = "complete (reaches launch)" if h.complete else \
            f"INCOMPLETE after {h.pages} pages; re-run with --max-pages {h.pages * 2}"
        unit = "account(s) queried on FastNEAR" if settings.history_source == "fastnear" else "NearBlocks page(s)"
        if settings.history_source == "fastnear":
            state = "complete" if h.complete else "INCOMPLETE: some account histories were cut short"
        print(f"{token}: {h.rows_fetched} rows fetched, {h.rows_new} new, {h.pages} {unit}, {state}")
        sample = from_db_rows(db.transfers_for(token))
        if args.show_sample and sample:
            print("  parser check (oldest 3 transfers as read):")
            for t in sample[:3]:
                print(f"    {t.ts}  {t.sender} -> {t.receiver}  {t.amount}  cause={t.cause}")
    return 0


def cmd_outcome(settings: Settings, args: argparse.Namespace) -> int:
    _apply_source(settings, args)
    db = _open(settings)
    c = Collector(settings, db)
    rules = OutcomeRules.load(args.rules)
    tokens = _tokens_from(args)
    print(f"Rules {rules.version} (see `rugguard rules`)")
    for i, token in enumerate(tokens, 1):
        logs.progress(f"[{i}/{len(tokens)}] {token}: post-mortem...")
        problem = _not_a_token(c, token)
        if problem:
            print(f"\n{problem} Skipped; nothing stored.")
            continue
        try:
            result, errors = run_outcome(c, token, rules, args.page_size, args.max_pages)
        except Exception as exc:          # one bad token must not end a batch
            logs.event("outcome_crash", token=token, error=repr(exc))
            print(f"\n{token}: ERROR {type(exc).__name__}: {exc}. Skipped; nothing stored.")
            continue
        print(f"\n{token}: {result.label.upper()}")
        for line in result.evidence:
            print(f"  - {line}")
        for e in errors:
            print(f"  ! {e['step']}: {e['error'][:160]}")
        # Stop only if the source the history comes from is refusing; a refused optional call
        # (e.g. NearBlocks deploy history while history comes from FastNEAR) is reported, not fatal.
        active = "fastnear_tx" if settings.history_source == "fastnear" else "nearblocks"
        if c.client.rate_limited(active):
            name = "FastNEAR" if active == "fastnear_tx" else "NearBlocks (free plan: 6 calls a minute, 333 a day)"
            print(f"\nStopped after {i} of {len(tokens)} token(s): {name} is refusing calls now. Run the same "
                  "command later: everything fetched so far is cached, so it resumes where it stopped.")
            break
    return 0


def cmd_inspect(settings: Settings, args: argparse.Namespace) -> int:
    """Summarise the stored transfer history of a token. Uses only the local database: no API calls."""
    args.token = _valid([args.token])[0]
    db = _open(settings)
    rows = db.transfers_for(args.token)
    run = db.history_run(args.token)
    if not rows:
        print(f"No stored transfers for {args.token}. Run `rugguard history {args.token}` first.")
        return 1
    transfers = from_db_rows(rows)
    fmt = lambda ns: datetime.fromtimestamp(ns / 1e9, timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if ns else "?"
    print(f"{args.token}: {len(rows)} stored rows -> {len(transfers)} transfers after de-duplication")
    if run:
        print(f"  history {'complete' if run['complete'] else 'INCOMPLETE'}, {run['pages']} page(s) of {run['page_size']}")
    print(f"  oldest {fmt(transfers[0].ts)}   newest {fmt(transfers[-1].ts)}")
    mints = [t for t in transfers if t.sender is None]
    print(f"  mints {len(mints)}, burns {sum(t.receiver is None for t in transfers)}")
    volume: dict[str, int] = {}
    for t in transfers:
        for a in (t.sender, t.receiver):
            if a:
                volume[a] = volume.get(a, 0) + t.amount
    print("  accounts with the most volume (sent + received):")
    for a, v in sorted(volume.items(), key=lambda x: x[1], reverse=True)[:10]:
        print(f"    {a:<48} {v}")
    dexish = [a for a in volume if "ref-finance" in a or "ref-labs" in a or "rhea" in a or "dex" in a or "swap" in a
              or "pool" in a or "veax" in a or "intents" in a]
    print(f"  DEX-like accounts seen: {', '.join(sorted(dexish)) or 'none'}")
    print("  oldest 5 transfers as parsed:")
    for t in transfers[:5]:
        print(f"    {fmt(t.ts)}  {t.sender} -> {t.receiver}  {t.amount}  cause={t.cause}")
    raw = json.loads(rows[0]["body_json"])
    print(f"  raw row fields: {', '.join(sorted(raw))}")
    return 0


def cmd_rings(settings: Settings, args: argparse.Namespace) -> int:
    """Wallets that trade many of the stored tokens (local data only, no API calls)."""
    from .analysis.rings import find_rings
    db = _open(settings)
    tokens = [r["token_id"] for r in db.query("SELECT DISTINCT token_id FROM ft_transfers")]
    if len(tokens) < args.min_tokens:
        print(f"Need transfers for at least {args.min_tokens} tokens; run `outcome` on more tokens first.")
        return 1
    histories = {t: from_db_rows(db.transfers_for(t)) for t in tokens}
    contracts = {r["account_id"] for r in db.query(
        "SELECT DISTINCT account_id FROM account_state")} | set(tokens)
    rules = OutcomeRules()
    found = find_rings(histories, set(rules.dex_accounts), contracts, args.min_tokens)
    print(f"{found['ring_size']} wallet(s) traded {args.min_tokens} or more of the {len(tokens)} stored tokens.")
    for w in found["wallets"][:args.top]:
        print(f"  {w['wallet']:<44} {len(w['tokens'])} tokens: {', '.join(w['tokens'])}")
    print("\nShare of each token's traders that belong to this group:")
    for token, info in found["tokens"].items():
        if info["traders"]:
            print(f"  {token:<34} {info['ring_members']:>3} of {info['traders']:<4} traders  {info['ring_share']:.0%}")
    return 0


def cmd_review(settings: Settings, args: argparse.Namespace) -> int:
    args.token = _valid([args.token])[0]
    if args.label not in LABELS:
        print(f"Label must be one of: {', '.join(LABELS)}")
        return 1
    db = _open(settings)
    if not db.query("SELECT 1 FROM outcomes WHERE token_id = ?", (args.token,)):
        print(f"No automatic outcome for {args.token} yet; run `rugguard outcome {args.token}` first.")
        return 1
    db.put_review(args.token, args.label, args.reason, args.reviewer)
    print(f"Recorded human label {args.label} for {args.token}.")
    return 0


def cmd_forget(settings: Settings, args: argparse.Namespace) -> int:
    """Remove a token's outcomes and reviews (e.g. test runs on placeholder names)."""
    args.tokens = _valid(args.tokens)
    db = _open(settings)
    for token in args.tokens:
        n = sum(db.conn.execute(f"DELETE FROM {table} WHERE token_id = ?", (token,)).rowcount
                for table in ("outcome_reviews", "outcomes"))
        db.conn.commit()
        print(f"{token}: removed {n} outcome/review row(s).")
    return 0


def cmd_discover(settings: Settings, args: argparse.Namespace) -> int:
    """List tokens from the newest Rhea pools: pool IDs are sequential, so newest = latest launches."""
    db = _open(settings)
    c = Collector(settings, db)
    try:
        found = discover_tokens(c, args.recent, args.suffix)
    except FetchError as exc:
        print(f"Could not read Rhea pools: {exc}")
        return 2
    rows = [(r["token_id"], {"pool_id": r["pool_id"], "near": r["wnear"]}) for r in found]
    skip: set[str] = set()
    for path in args.exclude or []:
        if not Path(path).exists():
            raise UsageError(f"Exclude file {path} not found.")
        skip |= {t.split()[0] for t in Path(path).read_text().splitlines()
                 if t.strip() and not t.strip().startswith("#")}
    if skip:
        before = len(rows)
        rows = [r for r in rows if r[0] not in skip]
        print(f"Excluded {before - len(rows)} token(s) already in {', '.join(args.exclude)}")
    if args.min_wnear:
        rows = [r for r in rows if r[1]["near"] >= args.min_wnear]
    if args.limit:
        rows = rows[:args.limit]
    print(f"{len(rows)} token(s) paired with wNEAR in the newest {args.recent} pools (newest first)")
    print(f"{'pool':>6}  {'wNEAR':>10}  token")
    for token, info in rows:
        print(f"{info['pool_id']:>6}  {info['near']:>10.1f}  {token}")
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        with open(args.out, "w") as f:
            f.write(f"# rugguard discover --recent {args.recent}: token  pool_id  wNEAR_in_pool\n")
            for token, info in rows:
                f.write(f"{token}  # pool {info['pool_id']}, {info['near']:.1f} wNEAR\n")
        print(f"\nWrote {len(rows)} token(s) to {args.out}")
    return 0


def cmd_outcomes(settings: Settings, args: argparse.Namespace) -> int:
    db = _open(settings)
    rows = db.outcome_table()
    if not rows:
        print("No outcomes yet; run `rugguard outcome <token>` first.")
        return 1
    width = max(len(r["token_id"]) for r in rows)
    print(f"{'token':<{width}}  {'auto':<12}  {'human':<12}  agree")
    pairs = []
    for r in rows:
        human = r["human_label"] or ""
        mark = "" if not human else ("yes" if human == r["auto_label"] else "NO")
        if human:
            pairs.append((r["auto_label"], human))
        print(f"{r['token_id']:<{width}}  {r['auto_label']:<12}  {human:<12}  {mark}")
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["auto_label"]] = counts.get(r["auto_label"], 0) + 1
    print("\nAuto labels: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    if pairs:
        kappa = cohen_kappa(pairs)
        agree = sum(a == b for a, b in pairs) / len(pairs)
        print(f"Reviewed {len(pairs)}: raw agreement {agree:.0%}, Cohen's kappa {kappa:.2f} (all labels)")
        # What the evaluation depends on: rugged or not, among tokens both sides could judge.
        judged = [(a == "rugged", b == "rugged") for a, b in pairs
                  if a in ("rugged", "faded", "alive") and b in ("rugged", "faded", "alive")]
        if judged:
            bk = cohen_kappa([("rugged" if a else "not", "rugged" if b else "not") for a, b in judged])
            ba = sum(a == b for a, b in judged) / len(judged)
            print(f"Rugged vs not rugged, {len(judged)} judged by both: agreement {ba:.0%}, kappa {bk:.2f}"
                  f"{' (small sample)' if len(judged) < 20 else ''}")
    else:
        print("No human reviews yet: `rugguard review <token> <label> --reason \"...\"`")
    return 0


def cmd_demo_pack(settings: Settings, args: argparse.Namespace) -> int:
    from . import demo
    try:
        info = demo.pack(settings.db_path, Path(args.out), with_cache=not args.no_cache)
    except FileNotFoundError as exc:
        print(exc)
        return 1
    print(f"Packed {settings.db_path} -> {info['path']}: {info['tokens']} tokens, {info['outcomes']} outcomes, "
          f"{info['risk_scores']} scores, {info['raw_responses']} cached API responses; "
          f"{info['db_bytes'] / 1e6:.1f} MB -> {info['packed_bytes'] / 1e6:.1f} MB")
    if info["packed_bytes"] > 50e6:
        print("That is large for git (GitHub warns above 50 MB). Try --no-cache.")
    return 0


def cmd_demo(settings: Settings, args: argparse.Namespace) -> int:
    """Everything a reviewer needs, offline: stored results, the evaluation, the testnet guard run."""
    from . import demo
    try:
        fresh = demo.unpack(Path(args.pack), demo.DEMO_DB, args.reset)
    except FileNotFoundError as exc:
        print(f"{exc} The demo pack ships with the repository; re-clone, or build one with `rugguard demo-pack`.")
        return 1
    settings.db_path, settings.offline = demo.DEMO_DB, True
    db = _open(settings)
    labels = {r["label"]: r["n"] for r in db.query(
        "SELECT o.label, COUNT(*) AS n FROM outcomes o WHERE o.outcome_id IN "
        "(SELECT MAX(outcome_id) FROM outcomes GROUP BY token_id) GROUP BY o.label")}
    n_scored = db.query("SELECT COUNT(DISTINCT sn.token_id) AS n FROM risk_scores rs "
                        "JOIN snapshots sn ON sn.snapshot_id = rs.snapshot_id WHERE rs.kind = 'total'")[0]["n"]
    print(f"RugGuard offline demo ({'unpacked' if fresh else 'using'} {demo.DEMO_DB}; no network calls)")
    print(f"{sum(labels.values())} mainnet tokens judged by the post-mortem ("
          + ", ".join(f"{k} {v}" for k, v in sorted(labels.items())) + f"); {n_scored} scored")
    sets = [(name, Path(p)) for name, p in (("holdout", args.holdout), ("dev", args.dev)) if Path(p).exists()]
    for name, path in sets or [("all", None)]:
        only = None
        if path is not None:
            only = {line.split()[0] for line in path.read_text().splitlines()
                    if line.strip() and not line.strip().startswith("#")}
        ev = evaluate(db, only)
        title = {"holdout": "Holdout (never used for tuning)", "dev": "Dev set (rules tuned here)"}.get(name, "All")
        print(f"\n{title}: {ev['judged']} judged, {ev['excluded']} excluded")
        for tag, e in (("rule labels", evaluate(db, only, use_human=False)), ("after human review", ev)):
            if e["auc"] is None:
                continue
            reviewed = sum(r["label_source"] == "human" for r in e["rows"])
            if tag == "after human review" and not reviewed:
                continue
            print(f"  {tag:<19} caught {e['caught']} of {e['rugged']} rugs at score >= {e['threshold']:.0f}; "
                  f"false alarms {e['false_alarms']} of {e['judged'] - e['rugged']}; AUC {e['auc']:.2f}")
        stem = Path(args.runs) / f"demo-{name}"
        csv_path, md_path = write_report(db, ev, stem, name, "rugguard " + " ".join(args.argv))
        print(f"  report: {md_path} (per-token data: {csv_path})")
    print("\nGuard vault on NEAR testnet (the agent holds one function-call key):")
    for what, tx in demo.GUARD_TXS:
        print(f"  {what}\n    https://testnet.nearblocks.io/txns/{tx}")
    print("\nReproduce a stored score from the cached API responses, offline:")
    print("  RUGGUARD_DB=data/demo.db RUGGUARD_OFFLINE=1 python -m rugguard score bome.narrowcap6721.near")
    if args.serve:
        print(f"\nServing the UI on http://{args.host}:{args.port} (offline; Ctrl+C to stop)")
        return cmd_serve(settings, args)
    print("Browse it: python -m rugguard demo --serve   (needs pip install -e '.[api]')")
    return 0


def cmd_serve(settings: Settings, args: argparse.Namespace) -> int:
    try:
        import uvicorn
    except ImportError:
        print("The web service needs extra packages: pip install -e '.[api]'")
        return 1
    from .api import create_app
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(f"Warning: listening on {args.host} exposes RugGuard to your network. "
              "Prefer docker compose, which puts the Caddy proxy in front.")
    logs.setup(stderr=True)
    uvicorn.run(create_app(settings), host=args.host, port=args.port, log_level="warning",
                proxy_headers=False, server_header=False)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="rugguard", description="Rug-pull risk signals for NEAR tokens.")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="create the SQLite schema").set_defaults(fn=cmd_init)

    def add_collect_flags(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--pools", action="store_true", help="scan Rhea pools for the token")
        sp.add_argument("--transfers-pages", type=int, default=2, help="NearBlocks pages of 25 transfers")
        sp.add_argument("--no-nearblocks", action="store_true", help="skip NearBlocks (RPC + FastNEAR only)")
        sp.add_argument("--stats", action="store_true",
                        help="also call NearBlocks FT details and count endpoints (often 403 on free tier)")
        sp.add_argument("--classify-holders", type=int, default=15,
                        help="check this many top holders for contract code (0 = off)")

    c = sub.add_parser("collect", help="collect a snapshot for one or more tokens")
    c.add_argument("tokens", nargs="+")
    add_collect_flags(c)
    c.set_defaults(fn=cmd_collect)

    cs = sub.add_parser("collect-seeds", help="collect every token listed in a file")
    cs.add_argument("seed_file")
    add_collect_flags(cs)
    cs.set_defaults(fn=cmd_collect, tokens=[])

    s = sub.add_parser("surface", help="live upgrade-surface check for one token")
    s.add_argument("token")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_surface)

    dm = sub.add_parser("demo", help="offline demo: stored results, evaluation reports, testnet guard run")
    dm.add_argument("--pack", default=str(Path("demo/rugguard-demo.db.gz")))
    dm.add_argument("--holdout", default="seeds/holdout.txt")
    dm.add_argument("--dev", default="seeds/memecoins.txt")
    dm.add_argument("--reset", action="store_true", help="restore data/demo.db from the pack again")
    dm.add_argument("--runs", default="runs", help="folder for the evaluation reports (default runs/)")
    dm.add_argument("--serve", action="store_true", help="then start the web UI on the demo data")
    dm.add_argument("--host", default="127.0.0.1")
    dm.add_argument("--port", type=int, default=8000)
    dm.set_defaults(fn=cmd_demo)

    dp = sub.add_parser("demo-pack", help="pack the current database into demo/rugguard-demo.db.gz")
    dp.add_argument("--out", default=str(Path("demo/rugguard-demo.db.gz")))
    dp.add_argument("--no-cache", action="store_true", help="leave out cached API responses (smaller)")
    dp.set_defaults(fn=cmd_demo_pack)

    tl = sub.add_parser("timeline", help="a token's pool activity in order, with seed and pull marked "
                                         "(for human labelling; stored data only)")
    tl.add_argument("token")
    tl.add_argument("--limit", type=int, default=60, help="rows to print (default 60)")
    tl.add_argument("--min-share", type=float, default=0.01,
                    help="hide buys/sells smaller than this share of the seed (default 0.01 = 1%%)")
    tl.add_argument("--pull-share", type=float, default=0.5,
                    help="mark a pool->seeder transfer as PULL? from this share of the seed (default 0.5)")
    tl.add_argument("--pool", default="v2.ref-finance.near")
    tl.set_defaults(fn=cmd_timeline)

    k = sub.add_parser("keys", help="what an account's access keys allow (e.g. prove an AI agent has no full-access key)")
    k.add_argument("account")
    k.add_argument("--json", action="store_true")
    k.set_defaults(fn=cmd_keys)

    r = sub.add_parser("report", help="show the latest stored scores for a token")
    r.add_argument("token")
    r.set_defaults(fn=cmd_report)

    # ---- post-mortem
    ru = sub.add_parser("rules", help="print the outcome rules and thresholds in force")
    ru.add_argument("--rules", help="JSON file overriding any rule (see output for keys)")
    ru.set_defaults(fn=cmd_rules)

    def add_history_flags(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("tokens", nargs="*")
        sp.add_argument("--seeds", help="file with one token per line")
        sp.add_argument("--page-size", type=int, default=None,
                        help="NearBlocks rows per page (default 25 on the free plan, 100 with an API key)")
        sp.add_argument("--max-pages", type=int, default=60, help="pages to walk back towards launch")
        sp.add_argument("--source", choices=["fastnear", "nearblocks"],
                        help="where transfer history comes from (default fastnear, or RUGGUARD_HISTORY_SOURCE)")

    h = sub.add_parser("history", help="fetch a token's full transfer history (resumable)")
    add_history_flags(h)
    h.add_argument("--show-sample", action="store_true", help="print the oldest transfers as parsed")
    h.set_defaults(fn=cmd_history)

    o = sub.add_parser("outcome", help="label what happened to a token after launch, with evidence")
    add_history_flags(o)
    o.add_argument("--rules", help="JSON file overriding rule thresholds")
    o.set_defaults(fn=cmd_outcome)

    ins = sub.add_parser("inspect", help="summarise a token's stored transfer history (no API calls)")
    ins.add_argument("token")
    ins.set_defaults(fn=cmd_inspect)

    rg = sub.add_parser("rings", help="wallets trading many of the stored tokens (no API calls)")
    rg.add_argument("--min-tokens", type=int, default=3)
    rg.add_argument("--top", type=int, default=25)
    rg.set_defaults(fn=cmd_rings)

    rv = sub.add_parser("review", help="record a human label for a token")
    rv.add_argument("token")
    rv.add_argument("label", help="one of: " + ", ".join(LABELS))
    rv.add_argument("--reason", required=True)
    rv.add_argument("--reviewer")
    rv.set_defaults(fn=cmd_review)

    sub.add_parser("outcomes", help="automatic vs human labels, with agreement").set_defaults(fn=cmd_outcomes)

    sc = sub.add_parser("score", help="combined 0-100 risk score from data up to the launch cut-off")
    add_history_flags(sc)
    sc.add_argument("--cut-hours", type=float, default=1.0, help="hours after launch the score may see")
    sc.set_defaults(fn=cmd_score)

    ev = sub.add_parser("evaluate", help="scores against post-mortem outcomes: recall, false alarms, AUC")
    ev.add_argument("--seeds", help="evaluate only the tokens in this seeds file (e.g. the holdout set)")
    ev.add_argument("--rules-only", action="store_true", help="ignore human reviews; use the rules' labels")
    ev.add_argument("--out", help="file stem for the CSV and Markdown report (default runs/eval-<set>-<UTC time>)")
    ev.add_argument("--no-files", action="store_true", help="print only; do not write the CSV and report")
    ev.set_defaults(fn=cmd_evaluate, tokens=[])

    gd = sub.add_parser("guard", help="guard vault on testnet: post scores (oracle), request swaps (agent)")
    gsub = gd.add_subparsers(dest="action", required=True)
    for name, helptext in (("post-score", "post the latest stored score for TOKEN to the vault (oracle)"),
                           ("check", "dry-run: would the vault approve a swap into TOKEN now?"),
                           ("swap", "ask the vault to swap wNEAR into TOKEN (signed by the agent's key)"),
                           ("config", "show the vault's owner, oracle, agent, limits and today's spending")):
        g = gsub.add_parser(name, help=helptext)
        if name != "config":
            g.add_argument("token")
        if name == "post-score":
            g.add_argument("--as-token", help="post under this testnet token ID (a stand-in for a mainnet token)")
            g.add_argument("--oracle", help="oracle account that signs (default RUGGUARD_ORACLE)")
        if name in ("check", "swap"):
            g.add_argument("--amount", default="1", help="wNEAR to spend, in NEAR (default 1)")
        if name == "swap":
            g.add_argument("--pool", type=int, required=True, help="Rhea pool ID for wNEAR/TOKEN")
            g.add_argument("--min-out", default="1", help="minimum TOKEN out, raw units (default 1)")
            g.add_argument("--agent", help="agent account that signs (default RUGGUARD_AGENT)")
            g.add_argument("--key-file", help="sign with this access-key JSON file instead of the keychain "
                                              "(default RUGGUARD_AGENT_KEY_FILE)")
        g.add_argument("--vault", help="vault contract account (default RUGGUARD_VAULT)")
        g.add_argument("--network", default="testnet")
        g.add_argument("--dry-run", action="store_true", help="print the near command instead of running it")
        g.set_defaults(fn=cmd_guard)

    fg = sub.add_parser("forget", help="delete stored outcomes and reviews for tokens")
    fg.add_argument("tokens", nargs="+")
    fg.set_defaults(fn=cmd_forget)

    d = sub.add_parser("discover", help="list tokens from the newest Rhea pools (latest launches)")
    d.add_argument("--recent", type=int, default=300, help="how many of the newest pools to scan")
    d.add_argument("--suffix", help="keep only token IDs ending with this, e.g. a launchpad suffix")
    d.add_argument("--out", help="write the list as a seeds file")
    d.add_argument("--exclude", action="append", metavar="FILE",
                   help="skip tokens listed in this seeds file (repeatable), e.g. your dev set")
    d.add_argument("--limit", type=int, help="keep only the newest N tokens")
    d.add_argument("--min-wnear", type=float, default=0.0,
                   help="keep only tokens whose pool now holds at least this much wNEAR (0 keeps drained pools)")
    d.set_defaults(fn=cmd_discover)

    sv = sub.add_parser("serve", help="run the web UI and API (default http://127.0.0.1:8000)")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8000)
    sv.set_defaults(fn=cmd_serve)

    e = sub.add_parser("export", help="export a table as CSV")
    e.add_argument("table")
    e.add_argument("--out")
    e.set_defaults(fn=cmd_export)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.argv = list(sys.argv[1:] if argv is None else argv)
    settings = Settings.from_env()
    logs.setup()
    logs.PROGRESS = True
    # Print each result line as it is produced, even through a pipe such as `| tee`.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except AttributeError:
        pass
    try:
        return args.fn(settings, args)
    except UsageError as exc:
        print(exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Pipeline for the combined score and its evaluation against post-mortem outcomes."""

from __future__ import annotations

import time

from .analysis import transfers as tx
from .analysis import upgrade_surface
from .analysis.outcome import OutcomeRules
from .analysis.score import NS_PER_HOUR, ScoreInputs, ScoreRules, auc, score
from .collect import Collector
from .postmortem import choose_pool, collect_history, launch_wnear


def run_score(c: Collector, token_id: str, rules: ScoreRules | None = None, page_size: int | None = None,
              max_pages: int = 50, now_ns: int | None = None) -> tuple[dict, list[dict]]:
    rules = rules or ScoreRules()
    errors: list[dict] = []
    c._try(errors, "transfer_history", lambda: collect_history(c, token_id, page_size, max_pages))
    run = c.db.history_run(token_id)
    transfers = tx.from_db_rows(c.db.transfers_for(token_id))

    supply_raw = c._try(errors, "ft_total_supply", lambda: c.rpc.ft_total_supply(token_id))
    supply = int(supply_raw) if supply_raw not in (None, "None") else None
    meta = c._try(errors, "ft_metadata", lambda: c.rpc.ft_metadata(token_id))
    c.db.upsert_token(token_id, meta, str(supply) if supply is not None else None)

    account = c._try(errors, "view_account", lambda: c.rpc.view_account(token_id))
    keys = c._try(errors, "view_access_key_list", lambda: c.rpc.view_access_key_list(token_id))
    code = c.code_analysis(token_id, account, errors) if account else None
    surface = upgrade_surface.assess(token_id, account, keys, code)

    wnear = "wrap.testnet" if c.settings.network == "testnet" else "wrap.near"
    pools = c._try(errors, "rhea_pools", lambda: c.rhea.pools_with_token(token_id)) or []
    pool = choose_pool(pools, token_id, wnear)
    deploy_ts = [int(d["block_timestamp"]) for d in c.db.deployments_for(token_id)
                 if str(d["block_timestamp"] or "").isdigit()]

    # Classify the largest holders at the cut-off as contracts or wallets.
    contracts: set[str] = set()
    launch_t, _ = tx.find_launch(transfers, rules.launch_pool_account)
    now = now_ns or time.time_ns()
    cut = min(launch_t.ts + int(rules.cut_hours * NS_PER_HOUR), now) if launch_t else now
    if transfers:
        balance, _, _ = tx.replay([t for t in transfers if t.ts <= cut])
        big = sorted(((a, b) for a, b in balance.items() if b > 0 and a not in rules.dex_accounts),
                     key=lambda x: x[1], reverse=True)[:20]
        # The first holder is always checked: a launchpad may have handed out its whole balance by
        # the cut-off, and must not then be mistaken for a deployer wallet.
        first = next((t for t in transfers if t.sender is None and t.receiver), transfers[0])
        origin = first.receiver if first.sender is None else first.sender
        check = big + ([(origin, 0)] if origin and origin not in {a for a, _ in big} else [])
        if launch_t and launch_t.sender and launch_t.sender not in {a for a, _ in check}:
            check.append((launch_t.sender, 0))      # needed for the lp_control signal
        contracts = c.classify_holders([{"account_id": a, "balance": str(b)} for a, b in check], len(check), errors)

    result = score(ScoreInputs(
        token_id=token_id, transfers=transfers, history_complete=bool(run and run["complete"]),
        total_supply=supply, surface_band=surface.band, surface_facts=surface.facts,
        code_flags=(code or {}).get("flags"), pool=pool, contracts=contracts,
        deploy_timestamps=deploy_ts, now_ns=now, wnear_id=wnear,
        launch_near=launch_wnear(c, transfers, rules.launch_pool_account, wnear, errors)), rules)
    if errors:
        result["facts"]["source_errors"] = errors

    snapshot_id = c.db.new_snapshot(token_id, c.settings.network, (account or {}).get("block_height"))
    c.db.set_snapshot_meta(snapshot_id, (account or {}).get("block_height"), errors)
    c.db.put_score(snapshot_id, "total", result["score"], result["band"], result["reasons"],
                   result["facts"], rules.version)
    return result, errors


EVAL_LABELS = ("rugged", "faded", "alive")


def evaluate(db, only: set[str] | None = None) -> dict:
    """Join each token's latest total score with its outcome (the human label wins when present)."""
    rows = []
    for r in db.outcome_table():
        if only is not None and r["token_id"] not in only:
            continue
        label = r["human_label"] or r["auto_label"]
        s = db.query("""SELECT rs.score, rs.band, rs.facts_json FROM risk_scores rs
                        JOIN snapshots sn ON sn.snapshot_id = rs.snapshot_id
                        WHERE sn.token_id = ? AND rs.kind = 'total' ORDER BY rs.score_id DESC LIMIT 1""",
                     (r["token_id"],))
        rows.append({"token_id": r["token_id"], "label": label, "label_source": "human" if r["human_label"] else "auto",
                     "human_reason": r["reason"] if r["human_label"] else None,
                     "score": s[0]["score"] if s else None, "band": s[0]["band"] if s else None})
    judged = [r for r in rows if r["label"] in EVAL_LABELS and r["score"] is not None]
    rugged = [r["score"] for r in judged if r["label"] == "rugged"]
    others = [r["score"] for r in judged if r["label"] != "rugged"]
    threshold = ScoreRules().band_high
    tp = sum(s >= threshold for s in rugged)
    fp = sum(s >= threshold for s in others)
    return {
        "rows": rows,
        "judged": len(judged),
        "excluded": len(rows) - len(judged),
        "rugged": len(rugged),
        "auc": auc(rugged, others),
        "threshold": threshold,
        "caught": tp,                 # rugged tokens scored high before they rugged
        "false_alarms": fp,           # non-rugged tokens scored high
        "recall": tp / len(rugged) if rugged else None,
        "precision": tp / (tp + fp) if (tp + fp) else None,
        "mean_score": {lbl: round(sum(r["score"] for r in judged if r["label"] == lbl) /
                                  max(1, sum(r["label"] == lbl for r in judged)), 1)
                       for lbl in EVAL_LABELS if any(r["label"] == lbl for r in judged)},
    }

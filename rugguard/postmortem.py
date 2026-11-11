"""Post-mortem pipeline: gather what happened to a token, then hand it to the outcome rules.

Separate from collect.py (the pre-flight risk check) on purpose: see analysis/outcome.py.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .analysis import transfers as tx
from .analysis.outcome import OutcomeInputs, OutcomeResult, OutcomeRules, insider_candidates, label
from . import logs
from .analysis.outcome import NS_PER_HOUR
from .collect import Collector
from .http import FetchError
from .sources.fastnear import fastnear_event_key
from .sources.rpc import EMPTY_CODE_HASH


@dataclass
class HistoryResult:
    token_id: str
    rows_fetched: int
    rows_new: int
    complete: bool
    pages: int


def collect_history(c: Collector, token_id: str, page_size: int | None = None, max_pages: int = 50,
                    source: str | None = None) -> HistoryResult:
    """Fetch the token's transfer history into ft_transfers, from FastNEAR (default) or NearBlocks."""
    if (source or c.settings.history_source) == "fastnear":
        return collect_history_fastnear(c, token_id, max_pages=max_pages)
    return collect_history_nearblocks(c, token_id, page_size, max_pages)


def collect_history_fastnear(c: Collector, token_id: str, max_pages: int = 25, max_accounts: int = 40,
                             window_hours: float = 72, early_hours: float = 1) -> HistoryResult:
    """Rebuild a token's history from the accounts that matter, one FastNEAR query each.

    FastNEAR's Transfers API answers "transfers of account X", never "all transfers of a token". The
    post-mortem and the score only need some accounts' complete histories, so we query, in order:
      1. the launch pool account (launch, liquidity, every direct buy and sell);
      2. the launcher (the first account to send tokens into the pool);
      3. the largest sellers into DEX accounts within `window_hours` of launch;
      4. the first-hour buyers;
      5. the current top holders;
      6. wallets the launcher sent tokens to before launch (its seeded wallets).
    Each queried account's history is complete, so its balances and peaks are exact. Accounts never
    queried are seen only where they meet a queried one. `complete` means every query finished.
    """
    rules = OutcomeRules()
    dex = set(rules.dex_accounts)
    queried: set[str] = set()
    complete = True
    fetched = new = queries = 0

    def query(account: str) -> None:
        nonlocal complete, fetched, new, queries
        if account in queried or len(queried) >= max_accounts:
            return
        queried.add(account)
        rows, done, _ = c.fastnear.account_transfers(account, token_id, max_pages=max_pages)
        queries += 1
        complete = complete and done
        fetched += len(rows)
        if rows:
            stored = [{"event_key": fastnear_event_key(r), "block_timestamp": str(r.get("block_timestamp") or ""),
                       "from_account": None, "to_account": None, "amount_raw": str(r.get("amount") or ""),
                       "cause": r.get("method_name"), "tx_hash": r.get("transaction_id"), "body": r} for r in rows]
            new += max(c.db.put_transfers(token_id, stored), 0)

    def current() -> list:
        return tx.from_db_rows(c.db.transfers_for(token_id))

    query(rules.launch_pool_account)
    transfers = current()
    launch, _ = tx.find_launch(transfers, rules.launch_pool_account)
    if launch is not None:
        if launch.sender:
            query(launch.sender)
        end = launch.ts + int(window_hours * NS_PER_HOUR)
        early = launch.ts + int(early_hours * NS_PER_HOUR)
        sells: dict[str, int] = {}
        buys: dict[str, int] = {}
        for t in transfers:
            if launch.ts <= t.ts <= end and t.receiver in dex and t.sender and t.sender not in dex:
                sells[t.sender] = sells.get(t.sender, 0) + t.amount
            if launch.ts <= t.ts <= early and t.sender in dex and t.receiver and t.receiver not in dex:
                buys[t.receiver] = buys.get(t.receiver, 0) + t.amount
        for a, _ in sorted(sells.items(), key=lambda x: x[1], reverse=True)[:10]:
            query(a)
        for a, _ in sorted(buys.items(), key=lambda x: x[1], reverse=True)[:10]:
            query(a)
    try:
        for h in c.fastnear.top_holders(token_id)[:15]:
            if h["account_id"] not in dex:
                query(h["account_id"])
    except FetchError:
        complete = False
    if launch is not None and launch.sender:
        seeded = [t.receiver for t in current()
                  if t.sender == launch.sender and t.ts < launch.ts and t.receiver and t.receiver not in dex]
        for a in dict.fromkeys(seeded):
            query(a)

    stored = current()
    c.db.put_history_run(token_id, queries, 0, len(stored), complete,
                         str(stored[0].ts) if stored else None, str(stored[-1].ts) if stored else None)
    logs.event("history.fastnear", token=token_id, accounts=queries, transfers=len(stored), complete=complete)
    return HistoryResult(token_id, fetched, new, complete, queries)


def collect_history_nearblocks(c: Collector, token_id: str, page_size: int | None = None,
                               max_pages: int = 50) -> HistoryResult:
    """Every transfer of the token from NearBlocks (resumable, cached; free plan: 333 calls a day)."""
    prev = c.db.history_run(token_id)
    known = None
    if prev is not None and prev["complete"]:
        known = {r["event_key"] for r in c.db.transfers_for(token_id)}
    page_size = page_size or c.settings.nearblocks_page_size
    rows, complete, pages = c.nearblocks.ft_transfer_history(token_id, page_size, max_pages, known)
    new = c.db.put_transfers(token_id, rows) if rows else 0
    stored = tx.from_db_rows(c.db.transfers_for(token_id))
    c.db.put_history_run(token_id, pages, page_size, len(stored), complete,
                         str(stored[0].ts) if stored else None, str(stored[-1].ts) if stored else None)
    return HistoryResult(token_id, len(rows), max(new, 0), complete, pages)


def launch_wnear(c: Collector, transfers: list, pool_account: str, wnear_id: str, errors: list,
                 window_minutes: int = 30) -> int | None:
    """wNEAR the launcher sent to the pool account within ±30 min of the launch (raw units), via FastNEAR."""
    launch, _ = tx.find_launch(transfers, pool_account)
    if launch is None or not launch.sender:
        return None
    return c._try(errors, "launch_wnear", lambda: c.fastnear.deposit_around(
        launch.sender, wnear_id, pool_account, launch.ts, window_minutes * 60 * 10**9))


def choose_pool(pools: list[dict], token_id: str, wnear_id: str) -> dict | None:
    """The token/wNEAR pool with the most wNEAR in it (the main trading venue)."""
    pairs = [p for p in pools if token_id in p.get("token_account_ids", [])
             and wnear_id in p.get("token_account_ids", [])]
    if not pairs:
        return None
    return max(pairs, key=lambda p: int(p["amounts"][p["token_account_ids"].index(wnear_id)]))


def run_outcome(c: Collector, token_id: str, rules: OutcomeRules, page_size: int | None = None,
                max_pages: int = 50, now_ns: int | None = None) -> tuple[OutcomeResult, list[dict]]:
    errors: list[dict] = []
    hist = c._try(errors, "transfer_history", lambda: collect_history(c, token_id, page_size, max_pages))
    run = c.db.history_run(token_id)
    complete = bool(run and run["complete"])
    transfers = tx.from_db_rows(c.db.transfers_for(token_id))

    supply_raw = c._try(errors, "ft_total_supply", lambda: c.rpc.ft_total_supply(token_id))
    supply = int(supply_raw) if supply_raw not in (None, "None") else None

    pools = c._try(errors, "rhea_pools", lambda: c.rhea.pools_with_token(token_id)) or []
    pool = choose_pool(pools, token_id, rules.wnear_id)

    deploys = c._try(errors, "nearblocks_deployments", lambda: c.nearblocks.contract_deployments(token_id))
    if deploys:
        c.db.put_deployments(token_id, deploys)
    deploy_ts = [int(d["block_timestamp"]) for d in c.db.deployments_for(token_id)
                 if str(d["block_timestamp"] or "").isdigit()]

    # Which large holders are contracts (escrows, launchpads, lockups) rather than wallets?
    contracts: set[str] = set()
    if supply and transfers:
        launch_t, _ = tx.find_launch(transfers, rules.launch_pool_account)
        launch_ts = launch_t.ts if launch_t else None
        cands = insider_candidates(transfers, rules, launch_ts, supply)[: rules.insider_top_n + 10]
        contracts = c.classify_holders([{"account_id": a, "balance": str(p)} for a, p in cands],
                                       len(cands), errors)

    launch_near = launch_wnear(c, transfers, rules.launch_pool_account, rules.wnear_id, errors)

    inputs = OutcomeInputs(
        token_id=token_id, transfers=transfers, history_complete=complete, total_supply=supply,
        pool=pool, pools_found=len(pools), contracts=contracts, deploy_timestamps=deploy_ts,
        now_ns=now_ns or time.time_ns(), launch_near=launch_near,
    )
    result = label(inputs, rules)
    if hist is not None:
        result.facts["history_pages_this_run"] = hist.pages
    if errors:
        result.facts["source_errors"] = errors
        if not complete and any(e["step"] == "transfer_history" and "rate limit" in e["error"] for e in errors):
            result.evidence = ["NearBlocks rate limit: the transfer history could not be fetched yet. "
                               "Re-run later; pages already fetched are cached and are not fetched again."]
        if result.label not in ("needs_review",) and any(e["step"] in ("rhea_pools", "ft_total_supply")
                                                          for e in errors):
            result.evidence.append("A required source failed; label downgraded to needs_review.")
            result.label = "needs_review"
    c.db.put_outcome(token_id, result.label, rules.version, result.evidence, result.facts)
    return result, errors


def not_a_token(c: Collector, token: str) -> str | None:
    """Cheap pre-check so a typo or placeholder fails clearly instead of producing odd labels."""
    try:
        view = c.rpc.view_account(token, ttl_s=86400)
    except FetchError as exc:
        if "UNKNOWN_ACCOUNT" in str(exc):
            return f"{token}: account does not exist on {c.settings.network}."
        return f"{token}: could not check the account ({exc})."
    if view.get("code_hash") in (None, "", EMPTY_CODE_HASH) and not view.get("global_contract_hash") \
            and not view.get("global_contract_account_id"):
        return f"{token}: account exists but has no contract, so it is not a token."
    try:
        c.rpc.ft_metadata(token)
    except FetchError:
        return f"{token}: has a contract but no ft_metadata method, so it is not an NEP-141 token."
    return None


def discover_tokens(c: Collector, recent: int, suffix: str | None = None) -> list[dict]:
    """Tokens paired with wNEAR in the newest `recent` Rhea pools, newest first, deepest pool per token."""
    wnear = "wrap.testnet" if c.settings.network == "testnet" else "wrap.near"
    best: dict[str, dict] = {}
    for p in c.rhea.recent_pools(recent):
        ids = p.get("token_account_ids", [])
        if len(ids) != 2 or wnear not in ids:
            continue
        token = ids[0] if ids[1] == wnear else ids[1]
        near = int(p["amounts"][ids.index(wnear)]) / 10**24
        if token not in best or near > best[token]["wnear"]:
            best[token] = {"token_id": token, "pool_id": p["pool_id"], "wnear": near}
    rows = sorted(best.values(), key=lambda r: r["pool_id"], reverse=True)
    if suffix:
        rows = [r for r in rows if r["token_id"].endswith(suffix)]
    return rows

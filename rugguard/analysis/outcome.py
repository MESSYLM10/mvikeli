"""Outcome labeller: what actually happened to a token after launch ("post-mortem").

This is deliberately separate from the risk scorer. The scorer may only use what was
knowable shortly after launch; this module judges what happened next. Keeping them apart
is what stops RugGuard from grading its own homework.

Every rule and threshold lives in `OutcomeRules` (printed by `rugguard rules`). Every label
comes with the evidence that produced it, so a human audits conclusions instead of making them.

Labels, checked in this order:
  needs_review  history incomplete, price unknown, or the data contradicts itself
  spoiled       never reached a trading pool (e.g. meme.cooking auction under soft cap)
  rugged        liquidity removed; or price collapsed AND (insider exit OR code changed after launch)
  pending       launched too recently to judge
  faded         price collapsed with no insider exit found: died of lost interest
  alive         price still at or above the collapse line

Price method: Rhea pools are constant-product (x·y = k). With liquidity unchanged
(meme.cooking locks LP for 69 years), price_now / price_launch = (token_reserve_launch /
token_reserve_now)², so only token-side flows are needed. The evidence states the method,
and the result is approximate if liquidity was added or removed after launch.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .transfers import Transfer, find_launch, replay

LABELS = ("needs_review", "spoiled", "rugged", "pending", "faded", "alive")
NS_PER_HOUR = 3600 * 10**9


@dataclass
class OutcomeRules:
    version: str = "outcome-rules-v2"
    collapse_ratio: float = 0.10          # price below 10% of launch = collapsed
    min_age_hours: float = 72             # younger than this = pending (unless already rugged)
    exit_window_hours: float = 72         # insider sells counted within this window after launch
    insider_min_share: float = 0.01       # a wallet whose peak balance reached ≥1% of supply
    insider_top_n: int = 10               # ...and is among the 10 largest such peaks
    exit_sell_fraction: float = 0.50      # sold ≥50% of its peak to a DEX = exited
    min_exit_supply_share: float = 0.02   # exits must total ≥2% of supply to count
    liquidity_removed_near: float = 1.0   # pool wNEAR side below 1 NEAR = liquidity removed
    liquidity_drained_fraction: float = 0.20  # pool keeps <20% of the NEAR put in at launch = liquidity drained
    max_launch_age_days: float = 90       # older tokens are not judged as launches (migrations, extra pools)
    max_estimate_ratio: float = 20.0      # a constant-product estimate above 20x means liquidity changed
    dex_accounts: list[str] = field(default_factory=lambda: ["v2.ref-finance.near", "dclv2.ref-labs.near", "aggregatedex.near"])
    launch_pool_account: str = "v2.ref-finance.near"
    wnear_id: str = "wrap.near"
    wnear_decimals: int = 24

    @classmethod
    def load(cls, path: str | Path | None) -> "OutcomeRules":
        if not path:
            return cls()
        data = json.loads(Path(path).read_text())
        unknown = set(data) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"unknown rule keys: {sorted(unknown)}")
        return cls(**data)

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class OutcomeInputs:
    token_id: str
    transfers: list[Transfer]          # oldest first, de-duplicated
    history_complete: bool
    total_supply: int | None
    pool: dict | None                  # chosen Rhea pool {pool_id, token_account_ids, amounts}
    pools_found: int
    contracts: set[str]                # holder accounts known to have contract code
    deploy_timestamps: list[int]       # ns
    now_ns: int
    launch_near: int | None = None     # raw wNEAR the launcher deposited into the pool at launch


@dataclass
class OutcomeResult:
    label: str
    evidence: list[str]
    facts: dict

    def as_dict(self) -> dict:
        return {"label": self.label, "evidence": self.evidence, "facts": self.facts}


def _hours(ns: int) -> float:
    return ns / NS_PER_HOUR


def _pct(x: float) -> str:
    return f"{x:.1%}" if x < 0.1 else f"{x:.0%}"


def insider_candidates(transfers: list[Transfer], rules: OutcomeRules, launch_ts: int | None,
                       supply: int) -> list[tuple[str, int]]:
    """Accounts whose peak balance (up to the end of the exit window) reached the insider share."""
    end = launch_ts + int(rules.exit_window_hours * NS_PER_HOUR) if launch_ts else None
    _, peak, _ = replay(transfers, end)
    dex = set(rules.dex_accounts)
    big = [(a, p) for a, p in peak.items() if a not in dex and p >= rules.insider_min_share * supply]
    return sorted(big, key=lambda x: x[1], reverse=True)


def label(inp: OutcomeInputs, rules: OutcomeRules) -> OutcomeResult:
    ev: list[str] = []
    facts: dict = {"rules_version": rules.version, "transfers": len(inp.transfers),
                   "history_complete": inp.history_complete}
    dex = set(rules.dex_accounts)

    def result(lbl: str) -> OutcomeResult:
        return OutcomeResult(lbl, ev, facts)

    if not inp.history_complete:
        ev.append("Transfer history does not reach the launch yet; re-run with a higher --max-pages.")
        return result("needs_review")
    if not inp.total_supply:
        ev.append("Total supply unknown; shares cannot be computed.")
        return result("needs_review")
    supply = inp.total_supply

    # ---- launch = first transaction that left tokens in the pool (refunded deposits skipped)
    launch, launch_reserve = find_launch(inp.transfers, rules.launch_pool_account)
    to_pool = [launch] if launch else []
    if not to_pool and inp.pools_found == 0:
        ev.append(f"No transfer ever reached {rules.launch_pool_account} and no Rhea pool holds the token: "
                  "it never launched for trading (a 'spoiled' auction, or never listed).")
        return result("spoiled")
    if not to_pool:
        ev.append("A pool lists the token but no transfer into it is in the history; check the data.")
        return result("needs_review")

    age_h = _hours(inp.now_ns - launch.ts)
    facts.update({"launch_ts": launch.ts, "launch_tx": launch.tx, "age_hours": round(age_h, 1),
                  "launch_token_reserve": str(launch_reserve)})
    ev.append(f"Launched {age_h / 24:.1f} days ago: first transfer into {rules.launch_pool_account} "
              f"(tx {launch.tx}).")
    if age_h / 24 > rules.max_launch_age_days:
        ev.append(f"Not judged: RugGuard judges launches, and this token first traded more than "
                  f"{rules.max_launch_age_days:.0f} days ago. A pool emptied this late is more likely a migration "
                  "or an abandoned extra pool than a launch rug.")
        return result("needs_review")

    # ---- liquidity
    if inp.pool is None:
        ev.append("Token reached the DEX but no wNEAR pool was found now; liquidity may have moved or been removed.")
        return result("needs_review")
    tokens = inp.pool.get("token_account_ids", [])
    amounts = [int(a) for a in inp.pool.get("amounts", [])]
    if inp.token_id not in tokens or rules.wnear_id not in tokens:
        ev.append(f"Pool {inp.pool.get('pool_id')} is not a {inp.token_id}/{rules.wnear_id} pair; price unknown.")
        return result("needs_review")
    tok_now = amounts[tokens.index(inp.token_id)]
    near_now = amounts[tokens.index(rules.wnear_id)] / 10 ** rules.wnear_decimals
    facts.update({"pool_id": inp.pool.get("pool_id"), "pool_near": round(near_now, 4),
                  "pool_token_reserve": str(tok_now)})
    if inp.launch_near:
        # Relative to what was put in: removing liquidity takes both sides out in proportion, so the
        # price can stay flat while holders lose the ability to sell (smithii.near series, Nov 2026).
        kept = amounts[tokens.index(rules.wnear_id)] / inp.launch_near
        facts["near_kept_fraction"] = round(kept, 4)
        drained = kept < rules.liquidity_drained_fraction
        facts["liquidity_drained"] = drained
        if drained:
            # Recorded now, decided at the end, so the price and insider evidence is still gathered.
            ev.append(f"Liquidity drained: pool {inp.pool.get('pool_id')} now holds {near_now:,.2f} NEAR, "
                      f"{_pct(kept)} of the {inp.launch_near / 10 ** rules.wnear_decimals:,.2f} NEAR put in at launch "
                      f"(rule: below {_pct(rules.liquidity_drained_fraction)}).")
    elif near_now < rules.liquidity_removed_near or tok_now == 0:
        ev.append(f"Liquidity removed: pool {inp.pool.get('pool_id')} now holds {near_now:.2f} NEAR "
                  f"(rule: below {rules.liquidity_removed_near} NEAR; the launch deposit was not found).")
        return result("rugged")

    near_now_raw = amounts[tokens.index(rules.wnear_id)]
    if tok_now == 0 or not launch_reserve:
        # An empty token side (or no launch reserve) leaves the price undefined; a drained pool is
        # still judged on the drain below (crashed the 11 Nov holdout run on an implicit-account token).
        facts.update({"price_ratio": None, "collapsed": None})
        ev.append(f"Price cannot be measured: the pool holds {tok_now} tokens now and {launch_reserve} at launch.")
        if not facts.get("liquidity_drained"):
            return result("needs_review")
        return result("rugged")
    if inp.launch_near:
        # Measured: pool price now (Rhea reserves) vs the price the launcher set (its two deposits).
        ratio = (near_now_raw / tok_now) / (inp.launch_near / launch_reserve)
        method = (f"pool now {near_now:,.2f} NEAR for {tok_now} tokens; at launch {launch.sender} put in "
                  f"{inp.launch_near / 10 ** rules.wnear_decimals:,.2f} NEAR for {launch_reserve} tokens")
        facts["price_method"] = "measured"
    else:
        ratio = (launch_reserve / tok_now) ** 2
        method = (f"constant-product estimate from the pool's token reserve: {launch_reserve} at launch vs "
                  f"{tok_now} now; the launch NEAR deposit was not found")
        facts["price_method"] = "estimated"
        if ratio > rules.max_estimate_ratio:
            facts["price_ratio_estimate"] = round(ratio, 2)
            ev.append(f"The price estimate ({_pct(ratio)} of launch) is only possible if liquidity changed after "
                      "launch, so it cannot be trusted, and the launch NEAR deposit was not found to measure it.")
            return result("needs_review")
    collapsed = ratio < rules.collapse_ratio
    facts.update({"price_ratio": round(ratio, 6), "collapsed": collapsed})
    ev.append(f"Price now ≈ {_pct(ratio)} of launch price ({method}); collapse line is {_pct(rules.collapse_ratio)}.")

    # ---- did the launcher take its liquidity back?
    window_end_pull = launch.ts + int(rules.exit_window_hours * NS_PER_HOUR)
    pulled = sum(t.amount for t in inp.transfers if t.sender == rules.launch_pool_account
                 and t.receiver == launch.sender and t.tx != launch.tx and launch.ts < t.ts <= window_end_pull)
    # Recorded for review only: tokens the launcher received from the DEX also include its own buys,
    # so this cannot tell a liquidity withdrawal from trading (it read 1,083% on meili-5.smithii.near).
    facts["launcher_received_from_dex"] = str(pulled)

    # ---- insider exits within the window
    window_end = launch.ts + int(rules.exit_window_hours * NS_PER_HOUR)
    candidates = [(a, p) for a, p in insider_candidates(inp.transfers, rules, launch.ts, supply)
                  if a not in inp.contracts][: rules.insider_top_n]
    sold: dict[str, int] = {a: 0 for a, _ in candidates}
    sells: dict[str, list[Transfer]] = {a: [] for a, _ in candidates}
    for t in inp.transfers:
        if t.ts > window_end:
            break
        if t.sender in sold and t.receiver in dex:
            sold[t.sender] += t.amount
            sells[t.sender].append(t)
    exits = [(a, p) for a, p in candidates if sold[a] >= rules.exit_sell_fraction * p]
    exit_share = sum(sold[a] for a, _ in exits) / supply
    insider_exit = bool(exits) and exit_share >= rules.min_exit_supply_share
    facts.update({"insiders_checked": len(candidates), "insiders_exited": len(exits),
                  "exit_supply_share": round(exit_share, 4), "insider_exit": insider_exit})
    for a, p in exits[:5]:
        s = sells[a]
        ev.append(f"Insider exit: {a} peaked at {_pct(p / supply)} of supply and sold {_pct(sold[a] / p)} of it "
                  f"to the DEX in {len(s)} transfer(s) within {_hours(s[-1].ts - launch.ts):.1f} h of launch "
                  f"(first tx {s[0].tx}).")
    if candidates and not exits:
        ev.append(f"Checked {len(candidates)} large wallet(s); none sold "
                  f"{_pct(rules.exit_sell_fraction)} of its peak within {rules.exit_window_hours:.0f} h.")

    # ---- code changes after launch
    redeploys = [ts for ts in inp.deploy_timestamps if ts > launch.ts]
    facts["redeploys_after_launch"] = len(redeploys)
    if redeploys:
        ev.append(f"Contract code was redeployed {len(redeploys)} time(s) after launch.")

    # ---- decision
    if facts.get("liquidity_drained") or (collapsed and (insider_exit or redeploys)):
        return result("rugged")
    if age_h < rules.min_age_hours:
        ev.append(f"Only {age_h:.0f} h old (rule: judge after {rules.min_age_hours:.0f} h).")
        return result("pending")
    if collapsed:
        return result("faded")
    if insider_exit:
        ev.append("Insiders exited, but the price held above the collapse line.")
    return result("alive")


def cohen_kappa(pairs: list[tuple[str, str]]) -> float | None:
    """Agreement between automatic and human labels, corrected for chance."""
    n = len(pairs)
    if n == 0:
        return None
    cats = sorted({x for p in pairs for x in p})
    observed = sum(a == b for a, b in pairs) / n
    expected = sum((sum(a == c for a, _ in pairs) / n) * (sum(b == c for _, b in pairs) / n) for c in cats)
    if expected == 1:
        return 1.0
    return (observed - expected) / (1 - expected)

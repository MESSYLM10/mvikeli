"""A readable timeline of a token's pool activity, for human labelling.

It lays out what happened and leaves the verdict to the reviewer:

* seed     the launch: the first deposit the pool kept (size S), and the wallet that made it (the seeder)
* pull     the pool sends the seeder at least `pull_share` of S (liquidity removal looks like this)
* seeder buy / seeder in   smaller moves between the pool and the seeder
* buy / sell               pool -> wallet, wallet -> pool
* round trip               a wallet sells back what it bought within `round_trip_minutes` (bots)
* fee                      small transfers to fee accounts (intear wallet, dex aggregators)

Rows smaller than `min_share` of S are counted, not printed, unless the seeder is involved, so a token
with thousands of transfers comes down to the moves that matter.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone

from .analysis.transfers import Transfer, find_launch

POOL = "v2.ref-finance.near"
# Other DEX contracts that hold the token's liquidity or route trades (Rhea DCL, aggregators).
OTHER_DEX = ("dclv2.ref-labs.near", "aggregatedex.near")
FEE_HINTS = ("wallet.intear.near", "dex-aggregator", "aggregatedex.near")
NS_MIN = 60 * 10**9


@dataclass
class Row:
    ts: int
    kind: str
    sender: str | None
    receiver: str | None
    amount: int
    share_of_seed: float | None
    tx: str | None


def _is_fee(account: str | None) -> bool:
    return bool(account) and any(h in account for h in FEE_HINTS)


def _short(dex: str) -> str:
    return dex.split(".")[0]


def arbitrage_legs(transfers: list[Transfer], wallet: str | None, dexes: set, window_ns: int = 5 * NS_MIN) -> int:
    """Times `wallet` received tokens from one DEX and sent at least 90% of them to another within minutes.

    An arbitrage bot does this all day; a liquidity provider does not. Found on kinu-3.nearlytrade.near,
    where the first depositor into the v2 pool was moving tokens between the v2 and DCL pools.
    """
    if not wallet:
        return 0
    legs = 0
    ins = [t for t in transfers if t.receiver == wallet and t.sender in dexes]
    outs = [t for t in transfers if t.sender == wallet and t.receiver in dexes]
    for i in ins:
        if any(o.receiver != i.sender and 0 <= o.ts - i.ts <= window_ns and o.amount >= 0.9 * i.amount
               for o in outs):
            legs += 1
    return legs


def build(transfers: list[Transfer], pool: str = POOL, min_share: float = 0.01, pull_share: float = 0.5,
          round_trip_minutes: int = 30) -> dict:
    dexes = {pool, *OTHER_DEX}
    launch, seed = find_launch(transfers, pool)
    seeder = launch.sender if launch else None
    rows: list[Row] = []
    hidden = Counter()
    # Round trips: a pool->wallet buy matched by the same wallet sending (about) the same amount back soon.
    buys: dict[str, list[Transfer]] = {}
    round_trip_tx: set = set()
    for t in transfers:
        if t.sender == pool and t.receiver and t.receiver != seeder:
            buys.setdefault(t.receiver, []).append(t)
        elif t.receiver == pool and t.sender in buys:
            for b in buys[t.sender]:
                if 0 <= t.ts - b.ts <= round_trip_minutes * NS_MIN and abs(t.amount - b.amount) <= 0.01 * b.amount:
                    round_trip_tx.update({id(b), id(t)})
                    buys[t.sender].remove(b)
                    break

    for t in transfers:
        share = t.amount / seed if seed else None
        if launch is not None and t is launch:
            kind = "SEED"
        elif _is_fee(t.receiver):
            hidden["fee"] += 1
            continue
        elif t.sender is None and share is not None and share < min_share:
            hidden["small mint"] += 1
            continue
        elif (t.sender in dexes or t.receiver in dexes) and pool not in (t.sender, t.receiver):
            other = t.sender if t.sender in dexes else t.receiver
            if seeder in (t.sender, t.receiver):
                kind = f"seeder {'<-' if t.sender in dexes else '->'} {_short(other)}"
            elif share is not None and share < min_share:
                hidden[f"small {_short(other)} trade"] += 1
                continue
            else:
                kind = f"{'buy' if t.sender in dexes else 'sell'} via {_short(other)}"
        elif pool not in (t.sender, t.receiver):
            if t.sender is None:
                kind = "mint"
            elif seeder in (t.sender, t.receiver):
                kind = "seeder wallet-to-wallet"
            else:
                hidden["wallet-to-wallet"] += 1
                continue
        elif id(t) in round_trip_tx:
            hidden["round trip"] += 1
            continue
        elif t.sender == pool and t.receiver == seeder:
            kind = "PULL?" if share is not None and share >= pull_share else "seeder buy"
        elif t.receiver == pool and t.sender == seeder:
            kind = "seeder in"
        elif t.sender == pool:
            kind = "buy"
        else:
            kind = "sell"
        if kind in ("buy", "sell") and share is not None and share < min_share:
            hidden[f"small {kind}"] += 1
            continue
        rows.append(Row(t.ts, kind, t.sender, t.receiver, t.amount, share, t.tx))

    days = Counter(datetime.fromtimestamp(t.ts / 1e9, tz=timezone.utc).date().isoformat() for t in transfers
                   if pool in (t.sender, t.receiver))
    pulls = [r for r in rows if r.kind == "PULL?"]
    return {"arbitrage_legs": arbitrage_legs(transfers, seeder, dexes), "seed": seed, "seeder": seeder, "launch_ts": launch.ts if launch else None, "rows": rows,
            "hidden": dict(hidden), "pool_days": dict(sorted(days.items())), "pulls": pulls,
            "last_ts": transfers[-1].ts if transfers else None, "total": len(transfers)}


def _ago(ns: int, since: int | None) -> str:
    if since is None:
        return ""
    d = (ns - since) / 1e9
    sign = "-" if d < 0 else "+"
    d = abs(d)
    return f"{sign}{d / 60:.0f}m" if d < 3600 else f"{sign}{d / 3600:.1f}h" if d < 172800 else f"{sign}{d / 86400:.1f}d"


def render(tl: dict, decimals: int, limit: int = 60) -> str:
    scale = 10 ** decimals

    def amt(x: int) -> str:
        v = x / scale
        return f"{v:,.0f}" if v >= 100 else f"{v:,.4g}"

    out = []
    if not tl["seeder"]:
        out.append("No launch found in the stored transfers: run `rugguard outcome TOKEN` first.")
        return "\n".join(out)
    out.append(f"Seed: {tl['seeder']} put {amt(tl['seed'])} tokens into {POOL} (S). Times are since the seed.")
    out.append(f"{'when':>8}  {'what':<24} {'amount':>18}  {'of S':>6}  from -> to")
    rows = tl["rows"]
    shown = rows if len(rows) <= limit else rows[:limit // 2] + [None] + rows[-limit // 2:]
    for r in shown:
        if r is None:
            out.append(f"{'...':>8}  ({len(rows) - limit} more rows; use --limit)")
            continue
        share = "" if r.share_of_seed is None else f"{r.share_of_seed:.0%}"
        what = r.kind + ("  <==" if r.kind in ("SEED", "PULL?") else "")
        out.append(f"{_ago(r.ts, tl['launch_ts']):>8}  {what:<24} {amt(r.amount):>18}  {share:>6}  "
                   f"{r.sender or 'mint'} -> {r.receiver or 'burn'}")
    if tl["hidden"]:
        out.append("Not shown: " + ", ".join(f"{v} {k}" for k, v in sorted(tl["hidden"].items())))
    days = tl["pool_days"]
    if days:
        peak = max(days.values())
        out.append("Pool activity per day (transfers in or out of the pool):")
        for day, n in days.items():
            out.append(f"  {day}  {'#' * max(1, round(30 * n / peak)):<30} {n}")
    if tl["pulls"]:
        p = max(tl["pulls"], key=lambda r: r.amount)
        out.append(f"Largest pool -> seeder transfer: {amt(p.amount)} ({p.share_of_seed:.0%} of S) at "
                   f"{_ago(p.ts, tl['launch_ts'])}, tx {p.tx}. Check on NearBlocks that wNEAR went to "
                   f"{tl['seeder']} in the same transaction (liquidity removal returns both sides).")
    else:
        out.append("No pool -> seeder transfer of half the seed or more.")
    if tl.get("arbitrage_legs", 0) >= 3:
        out.append(f"Caution: {tl['seeder']} moved tokens from one DEX to another within minutes "
                   f"{tl['arbitrage_legs']} times. That is how an arbitrage bot trades, not how a liquidity "
                   "provider behaves: its first deposit may not be the launch, and a PULL? may be an arbitrage leg.")
    out.append(f"{tl['total']} stored transfers. Stored history covers the pool, the seeder, early buyers "
               "and top holders; it may not be every transfer of the token.")
    return "\n".join(out)

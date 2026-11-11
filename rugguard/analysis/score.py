"""Combined pre-flight risk score, 0-100, computed only from what was knowable at the cut-off.

The cut-off is launch + `cut_hours` (default 1 h). Transfer-based signals replay history only up to
the cut-off, so evaluating this score against the post-mortem label (outcome.py) is a real prediction,
not hindsight. For a token younger than the cut-off, everything seen so far is used.

Signals and weights (ScoreRules, printed by `rugguard rules`):

  upgrade        25  can anyone change the code? (full-access keys, global binding, upgrade methods)
  lp_control     20  was liquidity added by a wallet (which then holds the pool shares and can withdraw
                     them) rather than a launchpad or locker contract? (score-v2: in the original design,
                     missing from v1; amd.amemedog.near was drained this way)
  concentration  15  top-10 *wallet* share of supply at the cut-off (contracts, pools, burn excluded)
  liquidity      10  how little of the supply sits in the pool at the cut-off, and how few NEAR
  early_buyers   10  first-hour buyers linked to the deployer, or a few buyers taking most of the volume
  deployer       10  share held by the deployer and the wallets it seeded before launch
  owner_powers   10  mint, pause, blacklist, ownership transfer exported by the code

Red-flag floors. A weighted average dilutes conditions that are enough on their own for a rug
(a token whose code cannot change can still be drained by one wallet). So two conditions set a minimum:
  * someone can replace the code (upgrade sub-score 1)                      -> at least `floor_upgrade`
  * one wallet cluster controls the supply (deployer or concentration = 1)
    AND the pool is thin at the cut-off (liquidity >= 0.7)                  -> at least `floor_drain`
A floor never lowers a score, and every applied floor is listed in the result's reasons and facts.

Each signal gives a sub-score from 0 (safe) to 1 (worst). A signal that cannot be computed is left out
and the remaining weights are rescaled; `coverage` reports the share of total weight actually used,
so "72/100 at 60% coverage" never pretends to know more than it does.

Known limits, stated in every result's facts:
  * code signals use the code deployed *now*; a redeploy after the cut-off is flagged, not reversed;
  * liquidity in NEAR at the cut-off assumes constant-product with unchanged liquidity (as outcome.py);
  * "linked" early buyers are linked through token transfers only; NEAR funding trails are not traced.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .transfers import Transfer, find_launch, replay

NS_PER_HOUR = 3600 * 10**9


@dataclass
class ScoreRules:
    version: str = "score-v2"
    cut_hours: float = 1.0
    weights: dict = field(default_factory=lambda: {
        "upgrade": 25, "lp_control": 20, "concentration": 15, "liquidity": 10,
        "early_buyers": 10, "deployer": 10, "owner_powers": 10})
    floor_upgrade: float = 60
    floor_drain: float = 70
    band_high: float = 60
    band_elevated: float = 30
    # liquidity: pool share of supply at cut-off, 30%+ = 0, 2% or less = 1
    pool_share_safe: float = 0.30
    pool_share_worst: float = 0.02
    thin_pool_near: float = 10.0          # fewer NEAR than this in the pool at cut-off = at least 0.7
    # deployer cluster share: 5% = 0, 30%+ = 1
    deployer_share_safe: float = 0.05
    deployer_share_worst: float = 0.30
    # early buyers: linked share of first-hour volume 10% = 0, 50%+ = 1
    linked_safe: float = 0.10
    linked_worst: float = 0.50
    dex_accounts: list[str] = field(default_factory=lambda: ["v2.ref-finance.near", "dclv2.ref-labs.near", "aggregatedex.near"])
    launch_pool_account: str = "v2.ref-finance.near"

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class ScoreInputs:
    token_id: str
    transfers: list[Transfer]          # oldest first
    history_complete: bool
    total_supply: int | None
    surface_band: str | None           # upgrade_surface.assess(...).band
    surface_facts: dict | None
    code_flags: dict | None            # analyse_wasm(...)["flags"]
    pool: dict | None                  # current chosen pool (for k = x*y), may be None
    contracts: set[str]                # holders known to be contracts
    deploy_timestamps: list[int]
    now_ns: int
    wnear_id: str = "wrap.near"
    launch_near: int | None = None     # raw wNEAR the launcher deposited at launch (measured)


@dataclass
class Component:
    name: str
    subscore: float | None             # None = could not be computed
    reason: str
    facts: dict = field(default_factory=dict)


def _ramp(x: float, safe: float, worst: float) -> float:
    """0 at `safe`, 1 at `worst`, linear in between; works for either direction."""
    if safe == worst:
        return 1.0 if x >= worst else 0.0
    t = (x - safe) / (worst - safe)
    return max(0.0, min(1.0, t))


def _pct(x: float) -> str:
    return f"{x:.1%}" if 0 < x < 0.1 else f"{x:.0%}"


# ------------------------------------------------------------------ signals

def upgrade_component(band: str | None, facts: dict | None) -> Component:
    if band is None or band == "unknown":
        return Component("upgrade", None, "Upgrade surface unknown.")
    facts = facts or {}
    if band == "high":
        return Component("upgrade", 1.0, "Someone can still replace the code (full-access key or global binding).",
                         {"band": band})
    flags = facts.get("code_flags", {})
    if flags.get("upgrade_method") or flags.get("add_key_method"):
        return Component("upgrade", 0.5, "The code exposes an upgrade or add-key method.", {"band": band})
    return Component("upgrade", 0.0, "No way to change the code was found.", {"band": band})


def owner_powers_component(flags: dict | None) -> Component:
    if flags is None:
        return Component("owner_powers", None, "Code not analysed.")
    s, found = 0.0, []
    for key, weight in (("mint_method", 0.6), ("blacklist_method", 0.4), ("pause_method", 0.4),
                        ("owner_method", 0.2), ("withdraw_method", 0.3), ("burn_from_method", 0.3)):
        if flags.get(key):
            s += weight
            found += flags[key]
    s = min(1.0, s)
    reason = ("Owner can " + ", ".join(sorted(set(found))) + ".") if found else "No owner-power methods exported."
    return Component("owner_powers", s, reason, {"methods": sorted(set(found))})


def deployer_cluster(transfers: list[Transfer], launch_ts: int | None, excluded: set[str]) -> tuple[str | None, set[str]]:
    """The first wallet to hold supply, plus the wallets it (or they) seeded before launch.

    If the first holder is a contract (a launchpad), there is no deployer wallet to follow: return None.
    """
    first = next((t for t in transfers if t.sender is None and t.receiver), None) or \
        next((t for t in transfers), None)
    if first is None:
        return None, set()
    origin = first.receiver if first.sender is None else first.sender
    if origin is None or origin in excluded:
        return None, set()
    cluster = {origin}
    for t in transfers:
        if launch_ts is not None and t.ts >= launch_ts:
            break
        if t.sender in cluster and t.receiver and t.receiver not in excluded:
            cluster.add(t.receiver)
    return origin, cluster


def score(inp: ScoreInputs, rules: ScoreRules) -> dict:
    supply = inp.total_supply or 0
    dex = set(rules.dex_accounts)
    excluded = inp.contracts | dex
    launch, _launch_reserve = find_launch(inp.transfers, rules.launch_pool_account)
    cut = min(launch.ts + int(rules.cut_hours * NS_PER_HOUR), inp.now_ns) if launch else inp.now_ns
    facts: dict = {"rules_version": rules.version, "cut_hours": rules.cut_hours, "cut_off_ns": cut,
                   "launched": launch is not None,
                   "history_complete": inp.history_complete,
                   "limits": ["code signals use the current code", "liquidity in NEAR assumes constant product",
                              "early buyers linked via token transfers only"]}
    comps: list[Component] = [upgrade_component(inp.surface_band, inp.surface_facts),
                              owner_powers_component(inp.code_flags)]

    usable = inp.history_complete and supply > 0 and bool(inp.transfers)
    if not usable:
        why = "history incomplete" if not inp.history_complete else "no supply or transfers"
        for name in ("concentration", "liquidity", "early_buyers", "deployer", "lp_control"):
            comps.append(Component(name, None, f"Not computed: {why}."))
    else:
        before = [t for t in inp.transfers if t.ts <= cut]
        balance, _, _ = replay(before)

        # concentration at cut-off (wallets only)
        wallets = sorted(((a, b) for a, b in balance.items() if a not in excluded and b > 0),
                         key=lambda x: x[1], reverse=True)
        top10 = sum(b for _, b in wallets[:10]) / supply
        comps.append(Component("concentration", _ramp(top10, 0.20, 0.80),
                               f"At the cut-off the top 10 wallets held {_pct(top10)} of supply.",
                               {"top10_wallet_share": round(top10, 4)}))

        # deployer cluster share at cut-off
        origin, cluster = deployer_cluster(inp.transfers, launch.ts if launch else None, excluded)
        if origin is None:
            comps.append(Component("deployer", None, "Supply started in a contract (e.g. a launchpad); "
                                                     "no deployer wallet to follow."))
        else:
            share = sum(max(0, balance.get(a, 0)) for a in cluster) / supply
            comps.append(Component("deployer", _ramp(share, rules.deployer_share_safe, rules.deployer_share_worst),
                                   f"The deployer {origin} and {len(cluster) - 1} wallet(s) it seeded before launch "
                                   f"held {_pct(share)} at the cut-off.",
                                   {"origin": origin, "cluster_size": len(cluster), "share": round(share, 4)}))

        if launch is None:
            comps.append(Component("lp_control", None, "Never reached a pool."))
            comps.append(Component("liquidity", None, "Never reached a pool."))
            comps.append(Component("early_buyers", None, "Never reached a pool."))
        else:
            # Who can withdraw the liquidity? Whoever added it holds the pool shares, unless that is a
            # launchpad or locker contract. Known at the cut-off: no later data is used.
            if launch.sender and launch.sender in inp.contracts:
                comps.append(Component("lp_control", 0.0, f"Liquidity was added by the contract {launch.sender} "
                                                           "(a launchpad or locker), not a person's wallet.",
                                       {"launcher": launch.sender, "launcher_is_contract": True}))
            elif launch.sender:
                comps.append(Component("lp_control", 1.0, f"Liquidity was added by the wallet {launch.sender}, which "
                                                           "holds the pool shares and can withdraw them at any time.",
                                       {"launcher": launch.sender, "launcher_is_contract": False}))
            else:
                comps.append(Component("lp_control", None, "Could not tell who added the liquidity."))
            # liquidity at cut-off: token side from flows; NEAR side from k = x*y of the current pool
            net = sum(t.amount if t.receiver == rules.launch_pool_account else -t.amount
                      for t in before if rules.launch_pool_account in (t.sender, t.receiver))
            tok_cut = max(net, 0)
            pool_share = tok_cut / supply
            s = _ramp(pool_share, rules.pool_share_safe, rules.pool_share_worst)
            lf = {"pool_share": round(pool_share, 4)}
            reason = f"At the cut-off the pool held {_pct(pool_share)} of supply"
            if inp.launch_near:
                # Measured, not inferred: the NEAR the launcher put into the pool at launch.
                near_launch = inp.launch_near / 10**24
                lf["pool_near_at_launch"] = round(near_launch, 2)
                reason += f", seeded with {near_launch:,.2f} NEAR at launch"
                if near_launch < rules.thin_pool_near:
                    s = max(s, 0.7)
            comps.append(Component("liquidity", s, reason + ".", lf))

            # early buyers: bought from the DEX between launch and cut-off
            buys = [t for t in before if t.sender in dex and t.receiver and t.receiver not in excluded
                    and t.ts >= launch.ts]
            volume: dict[str, int] = {}
            for t in buys:
                volume[t.receiver] = volume.get(t.receiver, 0) + t.amount
            total = sum(volume.values())
            if not total:
                comps.append(Component("early_buyers", 0.0, "Nobody bought before the cut-off.", {"buyers": 0}))
            else:
                linked = set(cluster)
                for t in before:
                    if t.sender in dex or t.receiver in dex:
                        continue
                    if t.sender in cluster and t.receiver:
                        linked.add(t.receiver)
                    if t.receiver in cluster and t.sender:
                        linked.add(t.sender)
                linked_share = sum(v for a, v in volume.items() if a in linked) / total
                top3 = sum(sorted(volume.values(), reverse=True)[:3]) / total
                s_linked = _ramp(linked_share, rules.linked_safe, rules.linked_worst)
                s_top3 = 0.6 * _ramp(top3, 0.5, 0.9) if len(volume) >= 3 else 0.0
                reason = (f"{len(volume)} wallet(s) bought before the cut-off; {_pct(linked_share)} of that volume "
                          f"went to wallets linked to the deployer, and the top 3 buyers took {_pct(top3)}.")
                comps.append(Component("early_buyers", max(s_linked, s_top3), reason,
                                       {"buyers": len(volume), "linked_share": round(linked_share, 4),
                                        "top3_share": round(top3, 4)}))

    redeploys_after_cut = [ts for ts in inp.deploy_timestamps if ts > cut]
    if redeploys_after_cut:
        facts["note"] = (f"Code was redeployed {len(redeploys_after_cut)} time(s) after the cut-off; code signals "
                         "describe today's code, not the code at the cut-off.")

    # ---- combine
    w = rules.weights
    used = [c for c in comps if c.subscore is not None]
    total_w = sum(w.values())
    used_w = sum(w[c.name] for c in used)
    value = round(100 * sum(w[c.name] * c.subscore for c in used) / used_w, 1) if used_w else None
    sub = {c.name: c.subscore for c in comps}
    floors = []
    if value is not None:
        if sub.get("upgrade") == 1.0:
            floors.append((rules.floor_upgrade, "Red flag: someone can replace the token's code at any time."))
        if (sub.get("deployer") == 1.0 or sub.get("concentration") == 1.0) and (sub.get("liquidity") or 0) >= 0.7:
            floors.append((rules.floor_drain, "Red flag: one wallet group controls the supply and the pool is thin, "
                                              "so it can drain the pool by selling."))
        for floor, _ in floors:
            value = max(value, floor)
    facts["floors_applied"] = [why for _, why in floors]
    facts["weighted_score"] = round(100 * sum(w[c.name] * c.subscore for c in used) / used_w, 1) if used_w else None
    band = ("unknown" if value is None else "high" if value >= rules.band_high
            else "elevated" if value >= rules.band_elevated else "low")
    ranked = sorted(used, key=lambda c: w[c.name] * c.subscore, reverse=True)
    facts["coverage"] = round(used_w / total_w, 2)
    facts["components"] = {c.name: {"subscore": None if c.subscore is None else round(c.subscore, 3),
                                     "weight": w[c.name], "reason": c.reason, **c.facts} for c in comps}
    return {
        "score": value,
        "band": band,
        "coverage": facts["coverage"],
        "reasons": ([why for _, why in floors] + [c.reason for c in ranked if c.subscore and c.subscore > 0])[:4]
                   or ["No risk signal fired."],
        "facts": facts,
    }


def auc(scores_pos: list[float], scores_neg: list[float]) -> float | None:
    """Probability a random rugged token scores above a random non-rugged one (ties count half)."""
    if not scores_pos or not scores_neg:
        return None
    wins = sum((p > n) + 0.5 * (p == n) for p in scores_pos for n in scores_neg)
    return wins / (len(scores_pos) * len(scores_neg))

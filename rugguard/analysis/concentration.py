"""Holder concentration: share of supply held by the largest *wallets*.

A wallet (an account with no contract code) can sell whenever its owner likes. A contract
holder (an AMM pool, NEAR Intents, a bridge, a lockup) holds tokens under its code's rules, so
its balance is reported separately and does not count as dumping risk. The first mainnet run
showed why: intents.near held 48% of wNEAR and was wrongly named as a dump risk.

Sub-score scale: top-10 wallet share 20% → 0.0, 80% → 1.0.
"""

from __future__ import annotations

WEIGHTS_VERSION = "concentration-v2"

# Known non-dumping holders, used when contract detection is unavailable.
DEFAULT_EXCLUDED = {
    "v2.ref-finance.near",   # Rhea / Ref Finance AMM
    "dclv2.ref-labs.near",   # Rhea concentrated-liquidity pools
    "intents.near",          # NEAR Intents verifier (escrow for user deposits)
    "system",
    "0000000000000000000000000000000000000000000000000000000000000000",  # common burn target
}


def assess(holders: list[dict], total_supply: str | int | None,
           excluded: set[str] | None = None, contract_holders: set[str] | None = None) -> dict:
    """
    holders           [{"account_id", "balance"}] largest first or unsorted
    excluded          extra account IDs to treat as non-wallets
    contract_holders  holders found to have contract code deployed (see Collector)
    """
    contracts = (contract_holders or set()) | DEFAULT_EXCLUDED | (excluded or set())
    try:
        supply = int(total_supply or 0)
    except (TypeError, ValueError):
        supply = 0
    if supply <= 0 or not holders:
        return {"band": "unknown", "score": 0.5,
                "reasons": ["Holder or supply data missing; concentration unknown."], "facts": {}}

    ranked = sorted(holders, key=lambda h: int(h["balance"]), reverse=True)
    wallets = [h for h in ranked if h["account_id"] not in contracts]
    held_by_contracts = [h for h in ranked if h["account_id"] in contracts]
    top1 = int(wallets[0]["balance"]) / supply if wallets else 0.0
    top10 = sum(int(h["balance"]) for h in wallets[:10]) / supply
    contract_share = sum(int(h["balance"]) for h in held_by_contracts) / supply

    score = min(1.0, max(0.0, (top10 - 0.20) / 0.60))
    band = "high" if score >= 0.6 else "medium" if score >= 0.3 else "low"
    reasons = [f"Top 10 wallets hold {top10:.0%} of supply; the largest single wallet holds {top1:.0%}."]
    if wallets and top1 >= 0.2:
        reasons.append(f"{wallets[0]['account_id']} could move the price sharply by selling.")
    if held_by_contracts and contract_share >= 0.005:
        biggest = held_by_contracts[0]
        reasons.append(f"A further {contract_share:.0%} sits in contracts, pools or burn addresses "
                       f"(largest: {biggest['account_id']}) and is not counted as dumping risk.")
    return {"band": band, "score": round(score, 3), "reasons": reasons,
            "facts": {"top1_wallet_share": round(top1, 4), "top10_wallet_share": round(top10, 4),
                      "contract_share": round(contract_share, 4), "holders_seen": len(ranked),
                      "contract_holders": [h["account_id"] for h in held_by_contracts[:20]]}}

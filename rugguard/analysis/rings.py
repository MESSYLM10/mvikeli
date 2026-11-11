"""Wallet rings: the same wallets trading many different tokens.

Found on 11 Nov 2026: the smithii.near series (fuk-3, olccl-4, meili-5, mmli-6, dabi-7, sinu-8),
each with a different freshly-named deployer, was traded by largely the same wallets. A ring of
wallets that buys and sells every new token from one factory is what wash trading looks like:
volume and price moves that do not come from independent buyers.

Uses only stored transfers (no API calls). A wallet "trades" a token when it sends tokens to, or
receives tokens from, a DEX account. Contracts and DEX accounts are not counted as traders.
"""

from __future__ import annotations

from collections import defaultdict

from .transfers import Transfer


def find_rings(histories: dict[str, list[Transfer]], dex: set[str], exclude: set[str] | None = None,
               min_tokens: int = 3) -> dict:
    exclude = (exclude or set()) | dex
    traded: dict[str, set[str]] = defaultdict(set)      # wallet -> tokens it traded
    traders: dict[str, set[str]] = defaultdict(set)     # token -> wallets that traded it
    for token, transfers in histories.items():
        for t in transfers:
            wallet = t.sender if t.receiver in dex else t.receiver if t.sender in dex else None
            if wallet and wallet not in exclude:
                traded[wallet].add(token)
                traders[token].add(wallet)
    ring = {w: toks for w, toks in traded.items() if len(toks) >= min_tokens}
    per_token = {}
    for token, ws in traders.items():
        members = ws & set(ring)
        per_token[token] = {"traders": len(ws), "ring_members": len(members),
                            "ring_share": round(len(members) / len(ws), 3) if ws else 0.0}
    return {
        "min_tokens": min_tokens,
        "ring_size": len(ring),
        "wallets": sorted(({"wallet": w, "tokens": sorted(t)} for w, t in ring.items()),
                          key=lambda x: (-len(x["tokens"]), x["wallet"])),
        "tokens": dict(sorted(per_token.items(), key=lambda kv: -kv[1]["ring_share"])),
    }

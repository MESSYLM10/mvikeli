"""Rhea Finance (formerly Ref Finance) DEX pools, read through NEAR RPC view calls.

Docs: https://docs.near.org/primitives/dex
Contract: v2.ref-finance.near (mainnet). Methods: get_number_of_pools, get_pools(from_index, limit).
Pool shape: {pool_kind, token_account_ids, amounts, total_fee, shares_total_supply, amp}.
"""

from __future__ import annotations

from .rpc import NearRpc


class Rhea:
    def __init__(self, rpc: NearRpc, contract: str):
        self.rpc = rpc
        self.contract = contract

    def number_of_pools(self) -> int:
        return int(self.rpc.call_view(self.contract, "get_number_of_pools"))

    def recent_pools(self, n: int, page: int = 250) -> list[dict]:
        """The newest `n` pools. Pool IDs are assigned in creation order, so newest = latest launches."""
        total = self.number_of_pools()
        start = max(0, total - n)
        out = []
        for s in range(start, total, page):
            batch = self.rpc.call_view(self.contract, "get_pools",
                                       {"from_index": s, "limit": min(page, total - s)}) or []
            out.extend({"pool_id": s + i, **p} for i, p in enumerate(batch))
        return out

    def pools_with_token(self, token_id: str, page: int = 250) -> list[dict]:
        """Scans every pool page (responses are cached) and keeps pools that include the token."""
        total = self.number_of_pools()
        found = []
        for start in range(0, total, page):
            batch = self.rpc.call_view(self.contract, "get_pools", {"from_index": start, "limit": page}) or []
            for offset, pool in enumerate(batch):
                if token_id in pool.get("token_account_ids", []):
                    found.append({"pool_id": start + offset, **pool})
        return found

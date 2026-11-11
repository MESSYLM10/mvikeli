"""Normalise FT transfer events into (time, from, to, amount) and replay balances.

NearBlocks FT event rows describe one balance change each:
    affected_account_id   the account whose balance changed
    involved_account_id   the counterparty (None for mints and burns)
    delta_amount          signed: negative = affected sent, positive = affected received
A single transfer can therefore appear twice (once per side). We normalise every row
to a directed transfer and drop duplicates by (tx, from, to, amount).
Rows that already carry explicit from/to fields are accepted as well.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class Transfer:
    ts: int                 # block timestamp, nanoseconds
    sender: str | None      # None = minted
    receiver: str | None    # None = burned
    amount: int             # raw u128
    tx: str | None
    cause: str | None
    event: str | None = None   # identifies one transfer across both sides' rows (FastNEAR: receipt + log index)


def _first(row: dict, *keys: str) -> Any:
    for key in keys:
        cur: Any = row
        for part in key.split("."):
            cur = cur.get(part) if isinstance(cur, dict) else None
        if cur not in (None, ""):
            return cur
    return None


def _int(value: Any) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def normalise(row: dict) -> Transfer | None:
    if "other_account_id" in row and "account_id" in row and "amount" in row:
        # FastNEAR Transfers API: signed amount from the point of view of `account_id`.
        amount = _int(row.get("amount"))
        if not amount:
            return None
        me, other = row.get("account_id"), row.get("other_account_id") or None
        ts = _int(row.get("block_timestamp")) or 0
        event = f"{row.get('receipt_id')}:{row.get('log_index')}:{row.get('transfer_index')}"
        tx = row.get("transaction_id") or row.get("receipt_id")
        kind = row.get("method_name") or row.get("transfer_type")
        if amount < 0:
            return Transfer(ts, me, other, -amount, tx, kind, event)
        return Transfer(ts, other, me, amount, tx, kind, event)

    ts = _int(_first(row, "block_timestamp", "block.block_timestamp")) or 0
    tx = _first(row, "transaction_hash", "tx_hash", "receipt_id")
    cause = _first(row, "cause", "event_type")
    delta = _int(row.get("delta_amount"))
    if delta is not None:
        affected = _first(row, "affected_account_id")
        involved = _first(row, "involved_account_id")
        if delta == 0:
            return None
        if delta < 0:
            return Transfer(ts, affected, involved, -delta, tx, cause)
        return Transfer(ts, involved, affected, delta, tx, cause)
    amount = _int(_first(row, "amount", "value"))
    if not amount:
        return None
    sender = _first(row, "from", "from_account", "sender_id", "old_owner_id")
    receiver = _first(row, "to", "to_account", "receiver_id", "new_owner_id")
    return Transfer(ts, sender, receiver, amount, tx, cause)


def normalise_all(rows: Iterable[dict]) -> list[Transfer]:
    """Normalise, de-duplicate both-sides rows, and sort oldest first."""
    seen: set = set()
    out: list[Transfer] = []
    for row in rows:
        t = normalise(row)
        if t is None:
            continue
        # Two keys: the event id (when the source gives one) and the directed transfer itself.
        # A row matching either was already counted from the other side.
        keys = {("e", t.event)} if t.event else set()
        keys.add(("t", t.tx, t.sender, t.receiver, t.amount, t.ts if t.tx is None else 0))
        if keys & seen:
            continue
        seen |= keys
        out.append(t)
    out.sort(key=lambda t: t.ts)
    return out


def from_db_rows(rows: Iterable[Any]) -> list[Transfer]:
    """Rebuild from stored ft_transfers rows (re-normalises the raw body, so parser fixes apply)."""
    return normalise_all(json.loads(r["body_json"]) for r in rows)


def replay(transfers: list[Transfer], until_ts: int | None = None) -> tuple[dict, dict, dict]:
    """Replay balances. Returns (balance, peak, peak_ts) per account, up to `until_ts` inclusive."""
    balance: dict[str, int] = defaultdict(int)
    peak: dict[str, int] = defaultdict(int)
    peak_ts: dict[str, int] = {}
    for t in transfers:
        if until_ts is not None and t.ts > until_ts:
            break
        if t.sender:
            balance[t.sender] -= t.amount
        if t.receiver:
            balance[t.receiver] += t.amount
            if balance[t.receiver] > peak[t.receiver]:
                peak[t.receiver] = balance[t.receiver]
                peak_ts[t.receiver] = t.ts
    return dict(balance), dict(peak), peak_ts


def find_launch(transfers: list[Transfer], pool_account: str) -> tuple[Transfer | None, int]:
    """The first transaction that left tokens in the pool account, and how many it left.

    A failed `ft_transfer_call` sends the tokens straight back in the same transaction, so a deposit
    is only a launch if the pool keeps some of it: per transaction, tokens in from each sender minus
    tokens returned to that sender. (Found on sinu-8.smithii.near: a 10,000-token deposit that was
    refunded preceded the real 392-token launch.)
    """
    by_tx: dict = {}
    order: list = []
    for t in transfers:
        if pool_account not in (t.sender, t.receiver) or t.tx is None:
            continue
        if t.tx not in by_tx:
            by_tx[t.tx] = []
            order.append(t.tx)
        by_tx[t.tx].append(t)
    for txid in order:
        rows = by_tx[txid]
        inflows = [t for t in rows if t.receiver == pool_account and t.sender]
        if not inflows:
            continue
        net = 0
        for sender in {t.sender for t in inflows}:
            kept = sum(t.amount for t in inflows if t.sender == sender) - \
                sum(t.amount for t in rows if t.sender == pool_account and t.receiver == sender)
            net += max(kept, 0)
        if net > 0:
            return inflows[0], net
    return None, 0

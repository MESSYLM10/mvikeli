"""NearBlocks v3 REST API: FT details, holder counts, transfer history, deploy history.

Docs / OpenAPI: https://api.nearblocks.io/openapi.json
Notes from the spec:
  * GET only; optional `Authorization: Bearer <key>` raises rate limits.
  * Cursor paging: pass the response's `next_page` value as `?next=`.
  * Each 25 items of `limit` costs one credit, so we page in small, cached steps.
  * v1/v2 endpoints switch off on 22 Nov 2026 — this module uses v3 only.
  * Attribution required for non-personal use: "Powered by Nearblocks.io APIs".

Response envelopes are parsed defensively (`_items`, `_next_cursor`) because v3 field
names should be confirmed against live responses on first run; raw bodies are always stored.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterator
from urllib.parse import urlencode

from ..analysis.transfers import normalise
from .. import logs
from ..http import JsonClient

LIST_KEYS = ("data", "results", "items", "txns", "holders", "deployments", "keys", "fts")


def _items(body: Any) -> list[dict]:
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        for key in LIST_KEYS:
            val = body.get(key)
            if isinstance(val, list):
                return val
            if isinstance(val, dict):  # e.g. {"data": {"items": [...]}}
                inner = _items(val)
                if inner:
                    return inner
    return []


def _next_cursor(body: Any) -> str | None:
    if not isinstance(body, dict):
        return None
    for holder in (body, body.get("meta") or {}, body.get("cursor") or {}, body.get("data") or {}):
        if isinstance(holder, dict):
            for key in ("next_page", "next", "cursor"):
                val = holder.get(key)
                if isinstance(val, (str, int)) and val != "":
                    return str(val)
    return None


def _first(row: dict, *keys: str) -> Any:
    for key in keys:
        cur: Any = row
        for part in key.split("."):
            cur = cur.get(part) if isinstance(cur, dict) else None
        if cur not in (None, ""):
            return cur
    return None


def _count(body: Any) -> int | None:
    if isinstance(body, dict):
        for key in ("count", "total", "holders", "txns"):
            val = body.get(key)
            if isinstance(val, (int, str)) and str(val).isdigit():
                return int(val)
        for key in ("data", "result", "holders", "txns"):
            inner = body.get(key)
            if isinstance(inner, (dict, list)):
                got = _count(inner[0] if isinstance(inner, list) and inner else inner)
                if got is not None:
                    return got
    return None


def _row_key(row: dict) -> str:
    rid = _first(row, "event_index", "receipt_id", "id")
    if rid is not None:
        return str(rid)
    return hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()[:32]


class NearBlocks:
    source = "nearblocks"

    def __init__(self, client: JsonClient, base_url: str, api_key: str | None = None):
        self.client = client
        self.base = base_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    def _get(self, path: str, params: dict | None = None, ttl_s: int | None = None) -> Any:
        qs = f"?{urlencode({k: v for k, v in (params or {}).items() if v is not None})}" if params else ""
        return self.client.fetch(self.source, "GET", f"{self.base}{path}{qs}", headers=self.headers, ttl_s=ttl_s)

    def _paged(self, path: str, limit: int, max_pages: int, params: dict | None = None) -> Iterator[dict]:
        cursor = None
        for _ in range(max_pages):
            body = self._get(path, {**(params or {}), "limit": limit, "next": cursor})
            rows = _items(body)
            yield from rows
            cursor = _next_cursor(body)
            if not cursor or not rows:
                break

    # ------------------------------------------------------------------ FTs
    def ft_details(self, contract: str) -> Any:
        return self._get(f"/v3/fts/{contract}")

    def ft_holder_count(self, contract: str) -> int | None:
        return _count(self._get(f"/v3/fts/{contract}/holders/count"))

    def ft_transfer_count(self, contract: str) -> int | None:
        return _count(self._get(f"/v3/fts/{contract}/txns/count"))

    def ft_holders(self, contract: str, limit: int = 25, max_pages: int = 1) -> list[dict]:
        out = []
        for row in self._paged(f"/v3/fts/{contract}/holders", limit, max_pages):
            acct = _first(row, "account", "account_id", "holder")
            amt = _first(row, "amount", "balance")
            if acct and amt is not None:
                out.append({"account_id": acct, "balance": str(amt)})
        return out

    @staticmethod
    def _transfer_row(row: dict) -> dict:
        t = normalise(row)
        return {
            "event_key": _row_key(row),
            "block_timestamp": str(_first(row, "block_timestamp", "block.block_timestamp") or ""),
            "from_account": t.sender if t else None,
            "to_account": t.receiver if t else None,
            "amount_raw": str(t.amount) if t else "",
            "cause": _first(row, "cause", "event_type"),
            "tx_hash": _first(row, "transaction_hash", "tx_hash"),
            "body": row,
        }

    def ft_transfers(self, contract: str, limit: int = 25, max_pages: int = 4) -> list[dict]:
        """Newest-first FT transfer events (a recent window)."""
        return [self._transfer_row(r) for r in self._paged(f"/v3/fts/{contract}/txns", limit, max_pages)]

    def ft_transfer_history(self, contract: str, page_size: int = 25, max_pages: int = 50,
                            known_complete: set[str] | None = None) -> tuple[list[dict], bool, int]:
        """Page back from newest to the very first transfer.

        Returns (rows, complete, pages). `complete` is True when the launch is inside the data:
        either the API ran out of pages, or we reached rows from an earlier complete run
        (`known_complete` = their event keys), so older history is already stored.
        Older pages never change, so they are cached for a year; only the first page refreshes.
        """
        rows: list[dict] = []
        cursor = None
        for page in range(1, max_pages + 1):
            body = self._get(f"/v3/fts/{contract}/txns", {"limit": page_size, "next": cursor},
                             ttl_s=3600 if cursor is None else 365 * 86400)
            batch = [self._transfer_row(r) for r in _items(body)]
            rows.extend(batch)
            logs.progress(f"    {contract}: transfer history page {page}, {len(rows)} transfers so far")
            if known_complete and batch and any(r["event_key"] in known_complete for r in batch):
                return rows, True, page
            cursor = _next_cursor(body)
            if not cursor or not batch:
                return rows, True, page
        return rows, False, max_pages

    # ------------------------------------------------------------- accounts
    def contract_deployments(self, account: str) -> list[dict]:
        body = self._get(f"/v3/accounts/{account}/contract/deployments")
        return [{
            "event_key": _row_key(row),
            "tx_hash": _first(row, "transaction_hash", "tx_hash"),
            "block_timestamp": str(_first(row, "block_timestamp", "block.block_timestamp") or ""),
            "body": row,
        } for row in _items(body)]

    def account_keys(self, account: str, limit: int = 25) -> list[dict]:
        return list(self._paged(f"/v3/accounts/{account}/keys", limit, 1))

"""FastNEAR REST APIs: top token holders, and per-account transfer history.

Docs: https://docs.near.org/tools/ecosystem-apis/fastnear-api · https://docs.fastnear.com/transfers
  GET  /v1/ft/{token_id}/top            → {"accounts": [{"account_id", "balance"}]}  (raw balances)
  POST transfers.main.fastnear.com/v0/transfers
       {"account_id", "asset_id": "nep141:<token>", "desc": false, "limit", "resume_token"?}
       → {"transfers": [...], "resume_token"}
The Transfers API is account-centric: `account_id` is required, so a token's history is rebuilt
from the accounts that matter (see postmortem.collect_history_fastnear). It worked without an API
key when tested on 11 Nov 2026; FASTNEAR_API_KEY is sent as a Bearer token if set.
Each row carries a signed `amount` from the queried account's point of view, `other_account_id`,
`block_timestamp` (ns), `transaction_id`, and the account's start/end-of-block balance.
"""

from __future__ import annotations

from .. import logs
from ..http import FetchError, JsonClient


class FastNear:
    source = "fastnear"
    tx_source = "fastnear_tx"

    def __init__(self, client: JsonClient, base_url: str, transfers_url: str = "https://transfers.main.fastnear.com",
                 api_key: str | None = None):
        self.client = client
        self.base = base_url.rstrip("/")
        self.transfers_url = transfers_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    def top_holders(self, token_id: str) -> list[dict]:
        data = self.client.fetch(self.source, "GET", f"{self.base}/v1/ft/{token_id}/top")
        holders = []
        for row in data.get("accounts", []):
            bal = row.get("balance")
            if bal in (None, ""):
                continue
            holders.append({"account_id": row["account_id"], "balance": str(bal)})
        holders.sort(key=lambda h: int(h["balance"]), reverse=True)
        return holders

    def account_fts(self, account_id: str) -> list[dict]:
        data = self.client.fetch(self.source, "GET", f"{self.base}/v1/account/{account_id}/ft")
        return data.get("tokens", [])

    def account_transfers(self, account_id: str, token_id: str, limit: int = 200,
                          max_pages: int = 25) -> tuple[list[dict], bool, int]:
        """All transfers of `token_id` to or from `account_id`, oldest first.

        Returns (rows, complete, pages). The first page is cached for an hour, later pages longer.
        """
        url = f"{self.transfers_url}/v0/transfers"
        base = {"account_id": account_id, "asset_id": f"nep141:{token_id}", "desc": False, "limit": limit}
        rows: list[dict] = []
        resume = None
        for page in range(1, max_pages + 1):
            body = dict(base, **({"resume_token": resume} if resume else {}))
            try:
                data = self.client.fetch(self.tx_source, "POST", url, body=body, headers=self.headers,
                                         ttl_s=3600 if resume is None else 7 * 86400)
            except FetchError as exc:
                # A page size the server refuses: retry once smaller, then give up on this account.
                if exc.status == 400 and limit > 50 and page == 1:
                    return self.account_transfers(account_id, token_id, 50, max_pages)
                raise
            batch = data.get("transfers") or []
            rows.extend(batch)
            resume = data.get("resume_token")
            logs.progress(f"    {account_id}: {len(rows)} transfer(s) of {token_id}")
            if not resume or not batch:
                return rows, True, page
        return rows, False, max_pages


    def deposit_around(self, account_id: str, asset_token: str, to_account: str, around_ns: int,
                       window_ns: int, max_pages: int = 10, limit: int = 200) -> int | None:
        """Sum of `asset_token` that `account_id` sent to `to_account` within ±window of `around_ns`.

        Used to read the wNEAR a launcher put into the pool at launch. Pages newest-first and stops
        once it is past the window, so an old, busy account costs only a few pages.
        """
        url = f"{self.transfers_url}/v0/transfers"
        base = {"account_id": account_id, "asset_id": f"nep141:{asset_token}", "desc": True, "limit": limit}
        total, seen_window, resume = 0, False, None
        for _ in range(max_pages):
            body = dict(base, **({"resume_token": resume} if resume else {}))
            data = self.client.fetch(self.tx_source, "POST", url, body=body, headers=self.headers, ttl_s=7 * 86400)
            batch = data.get("transfers") or []
            for r in batch:
                ts = int(r.get("block_timestamp") or 0)
                if ts > around_ns + window_ns:
                    continue
                if ts < around_ns - window_ns:
                    return total if seen_window else None
                seen_window = True
                if r.get("other_account_id") == to_account and int(r.get("amount") or 0) < 0:
                    total += -int(r["amount"])
            resume = data.get("resume_token")
            if not resume or not batch:
                break
        return total if seen_window and total else None


def fastnear_event_key(row: dict) -> str:
    return ":".join(str(row.get(k, "")) for k in ("receipt_id", "log_index", "transfer_index", "account_id"))

"""NEAR JSON-RPC: account state, access keys, contract code and NEP-141 view calls.

Docs: https://docs.near.org/api/rpc/contracts  ·  https://docs.near.org/protocol/access-keys
All calls are read-only `query` requests at `final` finality.
"""

from __future__ import annotations

import base64
import json
from typing import Any

from ..http import FetchError, JsonClient

# view_account returns this code_hash when no contract is deployed on the account.
EMPTY_CODE_HASH = "11111111111111111111111111111111"

FULL_PERMISSIONS = {"FullAccess", "GasKeyFullAccess"}


class NearRpc:
    source = "rpc"

    def __init__(self, client: JsonClient, url: str):
        self.client = client
        self.url = url

    def _query(self, params: dict, ttl_s: int | None = None) -> dict:
        body = {"jsonrpc": "2.0", "id": "rugguard", "method": "query", "params": {"finality": "final", **params}}
        data = self.client.fetch(self.source, "POST", self.url, body=body, ttl_s=ttl_s)
        if "error" in data:
            err = data["error"]
            name = (err.get("cause") or {}).get("name") or err.get("name") or "RPC_ERROR"
            raise FetchError(self.source, f"{name}: {json.dumps(err)[:300]}")
        result = data.get("result", {})
        # Some node versions report query errors inside `result`.
        if isinstance(result, dict) and "error" in result:
            raise FetchError(self.source, f"query error: {result['error']}")
        return result

    # ------------------------------------------------------------- accounts
    def view_account(self, account_id: str, ttl_s: int | None = None) -> dict:
        return self._query({"request_type": "view_account", "account_id": account_id}, ttl_s=ttl_s)

    def view_access_key_list(self, account_id: str) -> list[dict]:
        result = self._query({"request_type": "view_access_key_list", "account_id": account_id})
        return [normalise_key(k) for k in result.get("keys", [])]

    def view_code(self, account_id: str) -> tuple[str, bytes]:
        """Returns (code_hash, wasm_bytes). Long TTL: code is addressed by hash."""
        result = self._query({"request_type": "view_code", "account_id": account_id}, ttl_s=30 * 86400)
        return result.get("hash", ""), base64.b64decode(result.get("code_base64", ""))

    def view_global_contract_code(self, code_hash: str) -> tuple[str, bytes]:
        result = self._query({"request_type": "view_global_contract_code", "code_hash": code_hash},
                             ttl_s=30 * 86400)
        return result.get("hash", code_hash), base64.b64decode(result.get("code_base64", ""))

    def view_global_contract_code_by_account_id(self, account_id: str) -> tuple[str, bytes]:
        result = self._query({"request_type": "view_global_contract_code_by_account_id",
                              "account_id": account_id})
        return result.get("hash", ""), base64.b64decode(result.get("code_base64", ""))

    # -------------------------------------------------------- view methods
    def call_view(self, account_id: str, method: str, args: dict | None = None) -> Any:
        args_b64 = base64.b64encode(json.dumps(args or {}).encode()).decode()
        result = self._query({"request_type": "call_function", "account_id": account_id,
                              "method_name": method, "args_base64": args_b64})
        raw = bytes(result.get("result", []))
        return json.loads(raw.decode()) if raw else None

    def ft_metadata(self, token_id: str) -> dict:
        return self.call_view(token_id, "ft_metadata") or {}

    def ft_total_supply(self, token_id: str) -> str:
        return str(self.call_view(token_id, "ft_total_supply"))

    def ft_balance_of(self, token_id: str, account_id: str) -> str:
        return str(self.call_view(token_id, "ft_balance_of", {"account_id": account_id}))


def normalise_key(entry: dict) -> dict:
    """Flatten one `view_access_key_list` entry.

    `permission` is either the string "FullAccess" or an object such as
    {"FunctionCall": {"allowance": ..., "receiver_id": ..., "method_names": [...]}},
    {"GasKeyFullAccess": {...}} or {"GasKeyFunctionCall": {...}}.
    """
    access = entry.get("access_key", {})
    perm = access.get("permission")
    detail: dict = {}
    if isinstance(perm, str):
        kind = perm
    elif isinstance(perm, dict) and perm:
        kind, detail = next(iter(perm.items()))
        detail = detail or {}
    else:
        kind = "Unknown"
    # Gas keys may nest the function-call details one level deeper.
    inner = detail.get("permission") if isinstance(detail.get("permission"), dict) else detail
    if isinstance(inner, dict) and "FunctionCall" in inner:
        inner = inner["FunctionCall"]
    return {
        "public_key": entry.get("public_key", ""),
        "permission": kind,
        "is_full": kind in FULL_PERMISSIONS,
        "receiver_id": inner.get("receiver_id") if isinstance(inner, dict) else None,
        "method_names": (inner.get("method_names") or []) if isinstance(inner, dict) else [],
        "allowance": inner.get("allowance") if isinstance(inner, dict) else None,
        "nonce": access.get("nonce"),
    }

"""Test helpers: a tiny WASM builder and a fake HTTP transport serving NEAR-shaped responses."""

from __future__ import annotations

import base64
import json


def _leb(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _name(s: str) -> bytes:
    raw = s.encode()
    return _leb(len(raw)) + raw


def _section(sid: int, payload: bytes) -> bytes:
    return bytes([sid]) + _leb(len(payload)) + payload


def build_wasm(imports: list[str], exports: list[str]) -> bytes:
    """A valid module importing `env.<name>` functions and exporting no-op functions."""
    types = _section(1, _leb(1) + b"\x60\x00\x00")
    imp = _leb(len(imports)) + b"".join(_name("env") + _name(i) + b"\x00" + _leb(0) for i in imports)
    # a memory import too, as real NEAR contracts have, to exercise non-function import kinds
    imp_section = _section(2, _leb(len(imports) + 1) + imp[len(_leb(len(imports))):]
                           + _name("env") + _name("memory") + b"\x02\x00" + _leb(1))
    funcs = _section(3, _leb(len(exports)) + b"".join(_leb(0) for _ in exports))
    exp = _section(7, _leb(len(exports)) + b"".join(
        _name(e) + b"\x00" + _leb(len(imports) + i) for i, e in enumerate(exports)))
    code = _section(10, _leb(len(exports)) + b"".join(b"\x02\x00\x0b" for _ in exports))
    return b"\x00asm\x01\x00\x00\x00" + types + imp_section + funcs + exp + code


NEP141 = ["ft_transfer", "ft_transfer_call", "ft_total_supply", "ft_balance_of", "ft_metadata",
          "storage_deposit", "storage_balance_of"]


def rpc_ok(result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": "rugguard", "result": result}


def view_bytes(value) -> list[int]:
    return list(json.dumps(value).encode())


class FakeChain:
    """Routes RPC/FastNEAR/NearBlocks requests for one or more fake tokens."""

    def __init__(self):
        self.tokens: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []
        self.contract_accounts: set[str] = set()   # non-token holders that have code (pools, escrows)

    def add_token(self, token_id: str, *, keys: list[dict], wasm: bytes, supply: int,
                  holders: list[tuple[str, int]], global_account: str | None = None,
                  code_hash: str = "CodeHash1111111111111111111111111", deployments: int = 1):
        self.tokens[token_id] = dict(keys=keys, wasm=wasm, supply=supply, holders=holders,
                                     global_account=global_account, code_hash=code_hash,
                                     deployments=deployments)

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url))
        if method == "POST":
            return 200, self._rpc(body["params"])
        if "fastnear" in url:
            token = url.split("/v1/ft/")[1].split("/")[0]
            t = self.tokens[token]
            return 200, {"token_id": token, "accounts": [{"account_id": a, "balance": str(b)}
                                                         for a, b in t["holders"]]}
        if "nearblocks" in url:
            return self._nearblocks(url)
        return 404, {"error": "not found"}

    def _rpc(self, p: dict) -> dict:
        rt = p["request_type"]
        acct = p.get("account_id")
        t = self.tokens.get(acct)
        if t is None and rt == "view_account":    # a holder account
            code = "HolderContract1111111111111111111" if acct in self.contract_accounts else \
                "11111111111111111111111111111111"
            return rpc_ok({"amount": "1", "locked": "0", "code_hash": code, "storage_usage": 1,
                           "block_height": 1000, "block_hash": "h"})
        if t is None and rt != "view_global_contract_code_by_account_id":
            return {"jsonrpc": "2.0", "id": "x", "error": {"name": "HANDLER_ERROR",
                                                          "cause": {"name": "UNKNOWN_ACCOUNT"}}}
        if rt == "view_account":
            if t["global_account"]:
                return rpc_ok({"amount": "1", "locked": "0", "code_hash": "11111111111111111111111111111111",
                               "storage_usage": 100, "block_height": 1000, "block_hash": "h",
                               "global_contract_account_id": t["global_account"],
                               "global_contract_hash": None})
            return rpc_ok({"amount": "1", "locked": "0", "code_hash": t["code_hash"], "storage_usage": 100,
                           "block_height": 1000, "block_hash": "h",
                           "global_contract_account_id": None, "global_contract_hash": None})
        if rt == "view_access_key_list":
            return rpc_ok({"keys": t["keys"], "block_height": 1000})
        if rt == "view_code":
            return rpc_ok({"code_base64": base64.b64encode(t["wasm"]).decode(), "hash": t["code_hash"]})
        if rt == "view_global_contract_code_by_account_id":
            owner = next(v for v in self.tokens.values() if v["global_account"] == acct)
            return rpc_ok({"code_base64": base64.b64encode(owner["wasm"]).decode(), "hash": "GlobalHash"})
        if rt == "call_function":
            m = p["method_name"]
            if m == "ft_metadata":
                return rpc_ok({"result": view_bytes({"spec": "ft-1.0.0", "name": acct.upper(),
                                                     "symbol": acct[:4].upper(), "decimals": 18})})
            if m == "ft_total_supply":
                return rpc_ok({"result": view_bytes(str(t["supply"]))})
        return {"jsonrpc": "2.0", "id": "x", "error": {"name": "UNSUPPORTED", "request": rt}}

    def _nearblocks(self, url: str):
        token = url.split("/v3/")[1].split("/")[1].split("?")[0]
        t = self.tokens.get(token, {})
        if url.endswith("/holders/count"):
            return 200, {"holders": [{"count": str(len(t.get("holders", [])))}]}
        if url.endswith("/txns/count"):
            return 200, {"txns": [{"count": "3"}]}
        if "/txns" in url:
            rows = [{"event_index": f"e{i}", "block_timestamp": f"17000000000000000{i:02d}",
                     "affected_account_id": "alice.near", "involved_account_id": "bob.near",
                     "delta_amount": "100", "cause": "TRANSFER", "transaction_hash": f"tx{i}"}
                    for i in range(3)]
            return 200, {"data": rows}  # no next_page → single page
        if "/contract/deployments" in url:
            return 200, {"deployments": [{"transaction_hash": f"dep{i}", "block_timestamp": str(i)}
                                         for i in range(t.get("deployments", 1))]}
        if url.split("?")[0].endswith(f"/v3/fts/{token}"):
            return 200, {"contracts": [{"contract": token}]}
        return 404, {"error": "not found"}

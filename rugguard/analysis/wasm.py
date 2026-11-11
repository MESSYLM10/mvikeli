"""Minimal WebAssembly reader: just the import and export sections.

NEAR contracts reach the protocol through host functions imported from the `env`
module. Which host functions a contract imports tells us what it is *capable* of
doing on-chain, e.g. `promise_batch_action_deploy_contract` = it can replace code
(its own, or a sub-account's). A capability is not proof of misuse: who may trigger
it depends on the method's access checks, which need manual review.

Spec: https://webassembly.github.io/spec/core/binary/modules.html
"""

from __future__ import annotations

import re

WASM_MAGIC = b"\x00asm"
SECTION_IMPORT = 2
SECTION_EXPORT = 7

# NEAR host functions that give a contract power over code or keys.
DEPLOY_IMPORTS = {
    "promise_batch_action_deploy_contract",
    "promise_batch_action_deploy_global_contract",
    "promise_batch_action_deploy_global_contract_by_account_id",
    "promise_batch_action_use_global_contract",
    "promise_batch_action_use_global_contract_by_account_id",
}
ADD_FULL_KEY_IMPORTS = {"promise_batch_action_add_key_with_full_access"}
ADD_FC_KEY_IMPORTS = {"promise_batch_action_add_key_with_function_call"}
DELETE_KEY_IMPORTS = {"promise_batch_action_delete_key"}
DELETE_ACCOUNT_IMPORTS = {"promise_batch_action_delete_account"}

# Exported method-name hints. Names can be disguised, so these are hints, not proof.
EXPORT_HINTS = {
    "upgrade_method": re.compile(r"(^|_)(upgrade|update_contract|update_code|set_code|deploy)(_|$)"),
    "mint_method": re.compile(r"(^|_)(mint|ft_mint|issue)(_|$)"),
    "burn_from_method": re.compile(r"(^|_)burn_from(_|$)"),
    "pause_method": re.compile(r"(^|_)(pause|freeze|halt|stop_transfers)(_|$)"),
    "blacklist_method": re.compile(r"(black|block|deny)list|ban_account|freeze_account"),
    "owner_method": re.compile(r"(set|change|transfer)_owner(ship)?"),
    "add_key_method": re.compile(r"add_(full_access_)?key"),
    "withdraw_method": re.compile(r"(^|_)(withdraw_all|rescue|sweep|drain)(_|$)"),
}

# Methods that only change display metadata (USDT's upgrade_icon, upgrade_name_symbol).
# They match the upgrade pattern by name but cannot change code, so they are reported apart.
METADATA = re.compile(r"(icon|name|symbol|metadata|decimals|reference|logo)")

# Bump when analysis logic changes: stored analyses from older versions are recomputed.
ANALYSER_VERSION = 3


# Read-only by name: a prefix such as get_/is_, or a suffix such as _cost/_price (unog.near: nft_mint_cost).
GETTER = re.compile(r"^(get|is|view|has|query)_|_(cost|price|fee|status|info|balance|supply)$")


class WasmParseError(ValueError):
    pass


class _Reader:
    def __init__(self, data: bytes, pos: int = 0, end: int | None = None):
        self.data = data
        self.pos = pos
        self.end = len(data) if end is None else end

    def byte(self) -> int:
        if self.pos >= self.end:
            raise WasmParseError("unexpected end of section")
        b = self.data[self.pos]
        self.pos += 1
        return b

    def u32(self) -> int:
        result, shift = 0, 0
        while True:
            b = self.byte()
            result |= (b & 0x7F) << shift
            if not b & 0x80:
                return result
            shift += 7
            if shift > 63:
                raise WasmParseError("LEB128 too long")

    def name(self) -> str:
        n = self.u32()
        if self.pos + n > self.end:
            raise WasmParseError("name runs past section end")
        raw = self.data[self.pos:self.pos + n]
        self.pos += n
        return raw.decode("utf-8", errors="replace")

    def limits(self) -> None:
        flags = self.byte()
        self.u32()
        if flags & 0x01:
            self.u32()


def read_imports_exports(wasm: bytes) -> tuple[list[dict], list[dict]]:
    if len(wasm) < 8 or wasm[:4] != WASM_MAGIC:
        raise WasmParseError("not a WebAssembly binary")
    imports: list[dict] = []
    exports: list[dict] = []
    top = _Reader(wasm, 8)
    while top.pos < top.end:
        section_id = top.byte()
        size = top.u32()
        start, end = top.pos, top.pos + size
        if end > len(wasm):
            raise WasmParseError("section runs past end of file")
        if section_id == SECTION_IMPORT:
            r = _Reader(wasm, start, end)
            for _ in range(r.u32()):
                module, field = r.name(), r.name()
                kind = r.byte()
                if kind == 0x00:      # function: type index
                    r.u32()
                elif kind == 0x01:    # table: reftype + limits
                    r.byte(); r.limits()
                elif kind == 0x02:    # memory: limits
                    r.limits()
                elif kind == 0x03:    # global: valtype + mutability
                    r.byte(); r.byte()
                elif kind == 0x04:    # tag: attribute + type index
                    r.byte(); r.u32()
                else:
                    raise WasmParseError(f"unknown import kind {kind}")
                imports.append({"module": module, "name": field, "kind": kind})
        elif section_id == SECTION_EXPORT:
            r = _Reader(wasm, start, end)
            for _ in range(r.u32()):
                name = r.name()
                kind = r.byte()
                r.u32()
                exports.append({"name": name, "kind": kind})
        top.pos = end
    return imports, exports


def analyse_wasm(wasm: bytes) -> dict:
    """Return imports, exported methods and capability flags for one contract binary."""
    result = {"size_bytes": len(wasm), "imports": [], "exports": [], "flags": {}, "parse_error": None}
    try:
        imports, exports = read_imports_exports(wasm)
    except WasmParseError as exc:
        result["parse_error"] = str(exc)
        return result

    host = {i["name"] for i in imports if i["module"] == "env" and i["kind"] == 0}
    methods = sorted(e["name"] for e in exports if e["kind"] == 0)
    result["imports"] = sorted(f"{i['module']}.{i['name']}" for i in imports if i["kind"] == 0)
    result["exports"] = methods

    flags = {
        "can_deploy_code": sorted(host & DEPLOY_IMPORTS),
        "can_add_full_access_key": sorted(host & ADD_FULL_KEY_IMPORTS),
        "can_add_function_call_key": sorted(host & ADD_FC_KEY_IMPORTS),
        "can_delete_key": sorted(host & DELETE_KEY_IMPORTS),
        "can_delete_account": sorted(host & DELETE_ACCOUNT_IMPORTS),
    }
    # Read-only getters (get_blacklist_status, is_paused) describe a power, they do not exercise it.
    actionable = [m for m in methods if not GETTER.match(m.lower())]
    for hint, pattern in EXPORT_HINTS.items():
        flags[hint] = [m for m in actionable if pattern.search(m.lower())]
    flags["metadata_method"] = [m for m in flags["upgrade_method"] if METADATA.search(m.lower())]
    flags["upgrade_method"] = [m for m in flags["upgrade_method"] if m not in flags["metadata_method"]]
    flags["analyser_version"] = ANALYSER_VERSION
    nep141 = {"ft_transfer", "ft_transfer_call", "ft_total_supply", "ft_balance_of"}
    flags["implements_nep141"] = nep141.issubset(methods)
    result["flags"] = flags
    return result

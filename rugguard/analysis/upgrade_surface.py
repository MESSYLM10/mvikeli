"""Upgrade-surface check: can anyone change this token's code or seize its account?

NEAR offers three routes to change deployed code (see docs.near.org/smart-contracts/release/upgrade):
  1. a full-access key on the account redeploys at will;
  2. the contract's own code deploys new code (self-update), or adds itself a key;
  3. an account bound to a global contract *by account ID* runs whatever that owner publishes.
This module combines all three, plus deploy history, into one band and plain-language reasons.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..sources.rpc import EMPTY_CODE_HASH

WEIGHTS_VERSION = "upgrade-surface-v1"

BAND_SCORE = {"low": 0.0, "medium": 0.5, "high": 1.0, "unknown": 0.5}


@dataclass
class SurfaceReport:
    band: str
    score: float
    reasons: list[str] = field(default_factory=list)
    facts: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"band": self.band, "score": self.score, "reasons": self.reasons, "facts": self.facts}


def assess(token_id: str, account: dict | None, keys: list[dict] | None,
           code: dict | None, deployments: list[dict] | None = None) -> SurfaceReport:
    """
    account      view_account result (needs code_hash, global_contract_*)
    keys         normalised keys from NearRpc.view_access_key_list
    code         analyse_wasm() output for the code the account runs (None if unknown)
    deployments  NearBlocks deploy history rows (optional)
    """
    reasons: list[str] = []
    medium: list[str] = []
    facts: dict = {"token_id": token_id}

    if account is None or keys is None:
        return SurfaceReport("unknown", BAND_SCORE["unknown"],
                             ["Account or key data could not be fetched; upgrade surface unknown."], facts)

    full_keys = [k for k in keys if k["is_full"]]
    self_fc_keys = [k for k in keys if not k["is_full"] and k.get("receiver_id") == token_id]
    open_self_fc = [k for k in self_fc_keys if not k.get("method_names")]
    facts.update({
        "full_access_keys": len(full_keys),
        "function_call_keys": len(keys) - len(full_keys),
        "self_function_call_keys_unrestricted": len(open_self_fc),
        "code_hash": account.get("code_hash"),
        "global_contract_hash": account.get("global_contract_hash"),
        "global_contract_account_id": account.get("global_contract_account_id"),
    })

    high: list[str] = []
    if full_keys:
        noun = "key" if len(full_keys) == 1 else "keys"
        high.append(f"{len(full_keys)} full-access {noun} on {token_id}: the holder can replace the "
                    "token contract or move the account's funds at any time.")
    if account.get("global_contract_account_id"):
        high.append(f"Code follows global contract owned by {account['global_contract_account_id']}: "
                    "that account can publish new code and this token will run it.")

    has_code = (account.get("code_hash") not in (None, "", EMPTY_CODE_HASH)) or account.get("global_contract_hash")
    if not has_code and not account.get("global_contract_account_id"):
        high.append(f"No contract code on {token_id}: it is not a working NEP-141 token.")

    if code is None:
        medium.append("Contract code could not be analysed; self-upgrade paths unchecked.")
    elif code.get("parse_error"):
        medium.append(f"Contract code could not be parsed ({code['parse_error']}); self-upgrade paths unchecked.")
    else:
        flags = code.get("flags", {})
        facts["code_flags"] = {k: v for k, v in flags.items() if v}
        # Calibration from the first mainnet run (wrap.near, REF, USDT, stNEAR): every contract
        # links the deploy and add-full-access-key host functions, because near-sdk builds import
        # them whether or not a method uses them. So an import alone carries no signal; it only
        # matters when the contract also exposes a method that could reach it.
        upgrade_methods = flags.get("upgrade_method", []) + flags.get("add_key_method", [])
        notes = []
        if flags.get("can_deploy_code") or flags.get("can_add_full_access_key"):
            notes.append("Code links deploy/add-key host functions (normal for near-sdk builds).")
        if flags.get("metadata_method"):
            notes.append("Owner can change display metadata: " + ", ".join(flags["metadata_method"]) + ".")
        if upgrade_methods and flags.get("can_deploy_code"):
            medium.append("Contract can replace its own code through exported method(s) "
                          + ", ".join(upgrade_methods) + "; check who is allowed to call them.")
        elif upgrade_methods:
            medium.append("Exports upgrade-style methods: " + ", ".join(upgrade_methods) + ".")
        if flags.get("add_key_method") and flags.get("can_add_full_access_key"):
            medium.append("Contract can add a full-access key to itself, a possible way back into a locked token.")
        facts["notes"] = notes
        owner_powers = (flags.get("mint_method", []) + flags.get("pause_method", []) +
                        flags.get("blacklist_method", []) + flags.get("owner_method", []) +
                        flags.get("withdraw_method", []))
        if owner_powers:
            medium.append("Exports owner-power methods: " + ", ".join(sorted(set(owner_powers))) + ".")
        if not flags.get("implements_nep141"):
            medium.append("Core NEP-141 methods missing from exports; token behaviour is non-standard.")

    if open_self_fc:
        medium.append(f"{len(open_self_fc)} function-call key(s) may call any method on the token itself.")

    # Calibration from the second mainnet run: wrap.near (locked, no upgrade method) was raised to
    # medium by deploy history alone. A past redeploy cannot recur once every upgrade path is closed,
    # so history is evidence only when an upgrade path is still open.
    facts.setdefault("notes", [])
    if deployments is not None:
        facts["deploy_count"] = len(deployments)
        if len(deployments) > 1:
            line = f"Contract code has been deployed {len(deployments)} times"
            if high or medium:
                medium.append(line + ", and an upgrade path is still open.")
            else:
                facts["notes"].append(line + "; no upgrade path remains, so this cannot recur.")

    if high:
        band = "high"
        reasons = high + medium
    elif medium:
        band = "medium"
        reasons = medium
    else:
        band = "low"
        reasons = ["No full-access keys, no global-contract binding by account, and no upgrade or "
                   "owner-power method exported: the token contract is effectively immutable."]
    return SurfaceReport(band, BAND_SCORE[band], reasons, facts)

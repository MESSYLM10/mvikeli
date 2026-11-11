from rugguard.analysis.upgrade_surface import assess
from rugguard.analysis.wasm import analyse_wasm
from rugguard.sources.rpc import normalise_key

from .fakes import NEP141, build_wasm

ACCOUNT = {"code_hash": "Abc123", "global_contract_hash": None, "global_contract_account_id": None}
FULL = normalise_key({"public_key": "ed25519:AAA", "access_key": {"nonce": 1, "permission": "FullAccess"}})
FC_OTHER = normalise_key({"public_key": "ed25519:BBB", "access_key": {"nonce": 1, "permission": {
    "FunctionCall": {"allowance": None, "receiver_id": "other.near", "method_names": ["swap"]}}}})
CLEAN = analyse_wasm(build_wasm(["input"], NEP141))


def test_normalise_key_shapes():
    assert FULL["is_full"] and FULL["permission"] == "FullAccess"
    assert not FC_OTHER["is_full"] and FC_OTHER["receiver_id"] == "other.near"
    gas = normalise_key({"public_key": "k", "access_key": {"permission": {"GasKeyFullAccess": {"balance": "1"}}}})
    assert gas["is_full"]


def test_full_access_key_is_high():
    r = assess("tok.near", ACCOUNT, [FULL], CLEAN)
    assert r.band == "high" and "full-access key" in r.reasons[0]


def test_locked_clean_token_is_low():
    r = assess("tok.near", ACCOUNT, [FC_OTHER], CLEAN)
    assert r.band == "low"


def test_locked_but_self_upgradable_is_medium():
    code = analyse_wasm(build_wasm(["promise_batch_action_deploy_contract"], NEP141 + ["update_contract"]))
    r = assess("tok.near", ACCOUNT, [], code)
    assert r.band == "medium"
    assert any("replace its own code" in x and "update_contract" in x for x in r.reasons)


def test_global_contract_by_account_is_high_even_without_keys():
    acct = {"code_hash": "11111111111111111111111111111111", "global_contract_hash": None,
            "global_contract_account_id": "owner.near"}
    r = assess("tok.near", acct, [], CLEAN)
    assert r.band == "high" and "owner.near" in r.reasons[0]


def test_redeploys_on_locked_clean_token_stay_low():
    r = assess("tok.near", ACCOUNT, [], CLEAN, deployments=[{}, {}, {}])
    assert r.band == "low" and r.facts["deploy_count"] == 3


def test_missing_data_is_unknown():
    assert assess("tok.near", None, None, None).band == "unknown"

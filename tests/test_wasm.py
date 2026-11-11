from rugguard.analysis.wasm import analyse_wasm, read_imports_exports

from .fakes import NEP141, build_wasm


def test_reads_imports_and_exports():
    wasm = build_wasm(["input", "promise_batch_action_deploy_contract"], ["ft_transfer", "update_contract"])
    imports, exports = read_imports_exports(wasm)
    names = {i["name"] for i in imports}
    assert {"input", "promise_batch_action_deploy_contract", "memory"} <= names
    assert [e["name"] for e in exports] == ["ft_transfer", "update_contract"]


def test_flags_self_upgrade_and_key_backdoor():
    wasm = build_wasm(["promise_batch_action_deploy_contract", "promise_batch_action_add_key_with_full_access"],
                      NEP141 + ["update_contract", "mint", "pause"])
    flags = analyse_wasm(wasm)["flags"]
    assert flags["can_deploy_code"] == ["promise_batch_action_deploy_contract"]
    assert flags["can_add_full_access_key"] == ["promise_batch_action_add_key_with_full_access"]
    assert flags["upgrade_method"] == ["update_contract"]
    assert flags["mint_method"] == ["mint"]
    assert flags["pause_method"] == ["pause"]
    assert flags["implements_nep141"] is True


def test_clean_token_has_no_flags():
    flags = analyse_wasm(build_wasm(["input", "storage_write"], NEP141))["flags"]
    assert not any(v for k, v in flags.items() if k not in ("implements_nep141", "analyser_version"))


def test_garbage_is_reported_not_raised():
    result = analyse_wasm(b"not wasm at all")
    assert result["parse_error"]

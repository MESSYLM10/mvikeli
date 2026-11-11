"""Regression tests for problems found in the first mainnet run (10 Nov 2026)."""

from rugguard.analysis import concentration
from rugguard.analysis.upgrade_surface import assess
from rugguard.analysis.wasm import analyse_wasm
from rugguard.collect import CollectOptions
from rugguard.config import Settings
from rugguard.http import FetchError, JsonClient
from rugguard.storage.db import Database

from .fakes import NEP141, FakeChain, build_wasm
from .test_collect import make

ACCOUNT = {"code_hash": "Abc123", "global_contract_hash": None, "global_contract_account_id": None}
SDK_IMPORTS = ["input", "promise_batch_action_deploy_contract", "promise_batch_action_add_key_with_full_access"]


def test_sdk_imports_alone_do_not_raise_the_band():
    """wrap.near and REF link these host functions but expose no upgrade method."""
    code = analyse_wasm(build_wasm(SDK_IMPORTS, NEP141))
    r = assess("wrap.near", ACCOUNT, [], code)
    assert r.band == "low"
    assert r.facts["notes"]


def test_upgrade_method_plus_deploy_import_is_medium():
    """usdt.tether-token.near: exports `upgrade`, links deploy."""
    code = analyse_wasm(build_wasm(SDK_IMPORTS, NEP141 + ["upgrade", "add_to_blacklist", "mint"]))
    r = assess("usdt.tether-token.near", ACCOUNT, [], code)
    assert r.band == "medium"
    assert any("replace its own code" in x and "upgrade" in x for x in r.reasons)
    assert any("add_to_blacklist" in x for x in r.reasons)


def test_getters_are_not_owner_powers():
    flags = analyse_wasm(build_wasm(["input"], NEP141 + ["get_blacklist_status", "is_paused"]))["flags"]
    assert flags["blacklist_method"] == [] and flags["pause_method"] == []


def test_contract_holders_are_not_dump_risk():
    supply = 1000
    holders = [{"account_id": "intents.near", "balance": "480"},
               {"account_id": "lockup.near", "balance": "300"},
               *({"account_id": f"w{i}.near", "balance": "10"} for i in range(20))]
    got = concentration.assess(holders, supply, contract_holders={"lockup.near"})
    assert got["band"] == "low"
    assert got["facts"]["contract_share"] == 0.78
    assert any("intents.near" in r for r in got["reasons"])


def test_collector_classifies_contract_holders(tmp_path):
    chain = FakeChain()
    chain.contract_accounts.add("escrow.near")
    chain.add_token("tok.near", keys=[], supply=100, wasm=build_wasm(["input"], NEP141),
                    holders=[("escrow.near", 60)] + [(f"w{i}.near", 2) for i in range(20)])
    _, _, collector = make(tmp_path, chain)
    res = collector.collect("tok.near", CollectOptions(with_nearblocks=False))
    assert res.counts["contract_holders"] == 1
    assert res.concentration["band"] == "low"
    assert res.errors == []


class Scripted:
    """Transport that replays a fixed list of (status, body) responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def __call__(self, method, url, headers, body, timeout):
        self.calls += 1
        return self.responses.pop(0)


def client(tmp_path, transport, sleeps, **settings):
    s = Settings(db_path=tmp_path / "c.db", **settings)
    db = Database(s.db_path)
    db.init()
    clock = iter(range(0, 10_000))
    return JsonClient(s, db, transport, sleep=sleeps.append, clock=lambda: next(clock))


def test_429_backs_off_long_then_succeeds(tmp_path):
    sleeps = []
    t = Scripted([(429, {}), (429, {}), (200, {"ok": True})])
    c = client(tmp_path, t, sleeps, max_retries=3)
    assert c.fetch("nearblocks", "GET", "https://api.nearblocks.io/v3/x") == {"ok": True}
    assert sleeps[:2] == [15, 30]


def test_403_unavailable_endpoint_is_skipped_for_rest_of_run(tmp_path):
    sleeps = []
    t = Scripted([(403, {"message": "This endpoint is currently unavailable."})])
    c = client(tmp_path, t, sleeps)
    url = "https://api.nearblocks.io/v3/fts/a.near/holders/count"
    for _ in range(2):
        try:
            c.fetch("nearblocks", "GET", url)
        except FetchError:
            pass
    assert t.calls == 1   # second attempt never left the process


def test_nearblocks_calls_are_paced(tmp_path):
    sleeps = []
    t = Scripted([(200, {"n": 1}), (200, {"n": 2})])
    c = client(tmp_path, t, sleeps, min_interval_s={"nearblocks": 10})
    c.fetch("nearblocks", "GET", "https://api.nearblocks.io/v3/a")
    c.fetch("nearblocks", "GET", "https://api.nearblocks.io/v3/b")
    assert sleeps and sleeps[0] > 0


# ---- second mainnet run (10 Nov 2026, afternoon) ----

def test_redeploy_history_alone_is_a_note_not_medium():
    """wrap.near: locked, no upgrade method, deployed twice -> low."""
    code = analyse_wasm(build_wasm(SDK_IMPORTS, NEP141))
    r = assess("wrap.near", ACCOUNT, [], code, deployments=[{}, {}])
    assert r.band == "low"
    assert any("cannot recur" in n for n in r.facts["notes"])


def test_redeploy_history_counts_when_upgrade_path_open():
    code = analyse_wasm(build_wasm(SDK_IMPORTS, NEP141 + ["upgrade"]))
    r = assess("meta-pool.near", ACCOUNT, [], code, deployments=[{}, {}])
    assert r.band == "medium"
    assert any("still open" in x for x in r.reasons)


def test_metadata_updates_are_not_code_upgrades():
    """usdt.tether-token.near: upgrade_icon / upgrade_name_symbol change display data only."""
    flags = analyse_wasm(build_wasm(SDK_IMPORTS, NEP141 + ["upgrade", "upgrade_icon", "upgrade_name_symbol"]))["flags"]
    assert flags["upgrade_method"] == ["upgrade"]
    assert flags["metadata_method"] == ["upgrade_icon", "upgrade_name_symbol"]


def test_stale_stored_analysis_is_recomputed(tmp_path):
    """Bug: analyses stored by an older analyser were reused, so get_blacklist_status
    still showed as an owner power after the getter fix."""
    chain = FakeChain()
    chain.add_token("tok.near", keys=[], supply=100, wasm=build_wasm(["input"], NEP141 + ["get_blacklist_status"]),
                    holders=[(f"w{i}.near", 5) for i in range(20)])
    _, db, collector = make(tmp_path, chain)
    stale = analyse_wasm(build_wasm(["input"], NEP141))
    stale["flags"] = {**stale["flags"], "blacklist_method": ["get_blacklist_status"]}
    del stale["flags"]["analyser_version"]
    db.put_contract_code("CodeHash1111111111111111111111111", stale)

    res = collector.collect("tok.near", CollectOptions(with_nearblocks=False))
    assert res.surface["band"] == "low"
    assert not any("get_blacklist_status" in r for r in res.surface["reasons"])


def test_unknown_holder_accounts_are_wallets_not_errors(tmp_path):
    chain = FakeChain()
    chain.add_token("tok.near", keys=[], supply=100, wasm=build_wasm(["input"], NEP141),
                    holders=[("0000000000000000000000000000000000000000000000000000000000000000", 50)]
                    + [(f"w{i}.near", 2) for i in range(25)])
    original = chain._rpc

    def rpc(p):
        if p["request_type"] == "view_account" and p["account_id"] == "w0.near":
            return {"jsonrpc": "2.0", "id": "x", "error": {"name": "HANDLER_ERROR",
                                                          "cause": {"name": "UNKNOWN_ACCOUNT"}}}
        return original(p)

    looked_up = []

    def tracking(p):
        if p["request_type"] == "view_account":
            looked_up.append(p["account_id"])
        return rpc(p)

    chain._rpc = tracking
    _, _, collector = make(tmp_path, chain)
    res = collector.collect("tok.near", CollectOptions(with_nearblocks=False))
    assert res.errors == []                        # UNKNOWN_ACCOUNT is an answer, not a failure
    assert not any(a.startswith("0000") for a in looked_up)   # burn address skipped entirely

import json

from rugguard.collect import CollectOptions, Collector
from rugguard.config import Settings
from rugguard.http import FetchError, JsonClient
from rugguard.storage.db import Database

from .fakes import NEP141, FakeChain, build_wasm

FULL_KEY = {"public_key": "ed25519:dev", "access_key": {"nonce": 5, "permission": "FullAccess"}}


def make(tmp_path, chain, **overrides):
    settings = Settings(db_path=tmp_path / "t.db", max_retries=0, **overrides)
    db = Database(settings.db_path)
    db.init()
    return settings, db, Collector(settings, db, transport=chain)


def chain_with_tokens():
    chain = FakeChain()
    supply = 1_000_000 * 10**18
    chain.add_token("rug.meme.near", keys=[FULL_KEY], supply=supply,
                    wasm=build_wasm(["input", "promise_batch_action_deploy_contract"], NEP141 + ["mint"]),
                    holders=[("dev.near", supply * 70 // 100), ("v2.ref-finance.near", supply * 20 // 100),
                             ("a.near", supply * 5 // 100)], deployments=3)
    chain.add_token("honest.meme.near", keys=[], supply=supply, code_hash="HonestHash11111111111111111111111",
                    wasm=build_wasm(["input"], NEP141),
                    holders=[(f"h{i}.near", supply // 100) for i in range(40)])
    return chain


def test_collect_end_to_end(tmp_path):
    settings, db, collector = make(tmp_path, chain_with_tokens())

    rug = collector.collect("rug.meme.near", CollectOptions(transfers_pages=1))
    assert rug.errors == []
    assert rug.surface["band"] == "high"
    assert rug.concentration["band"] == "high"
    assert rug.counts["keys"] == 1 and rug.counts["deployments"] == 3

    honest = collector.collect("honest.meme.near")
    assert honest.surface["band"] == "low"
    assert honest.concentration["band"] == "low"

    tok = db.query("SELECT * FROM tokens WHERE token_id = 'rug.meme.near'")[0]
    assert tok["decimals"] == 18 and tok["total_supply"] == str(1_000_000 * 10**18)
    keys = db.query("SELECT * FROM access_keys WHERE snapshot_id = ?", (rug.snapshot_id,))
    assert keys[0]["is_full"] == 1
    code = db.query("SELECT flags_json FROM contract_code")
    assert len(code) == 2
    assert db.query("SELECT COUNT(*) AS n FROM ft_transfers")[0]["n"] == 6
    scores = db.scores_for(rug.snapshot_id)
    assert {s["kind"] for s in scores} == {"upgrade_surface", "concentration"}


def test_one_failing_source_does_not_lose_the_snapshot(tmp_path):
    chain = chain_with_tokens()
    original = chain.__call__

    def flaky(method, url, headers, body, timeout):
        if "fastnear" in url:
            return 503, {"error": "down"}
        return original(method, url, headers, body, timeout)

    _, db, collector = make(tmp_path, flaky)
    res = collector.collect("honest.meme.near")
    assert res.surface["band"] == "low"
    assert res.concentration["band"] == "unknown"
    assert any(e["step"] == "fastnear_top_holders" for e in res.errors)
    stored = json.loads(db.latest_snapshot("honest.meme.near")["errors_json"])
    assert stored and stored[0]["step"] == "fastnear_top_holders"


def test_cache_then_offline_replay(tmp_path):
    chain = chain_with_tokens()
    settings, db, collector = make(tmp_path, chain)
    collector.collect("honest.meme.near")
    first_calls = len(chain.calls)

    settings.offline = True
    replay = Collector(settings, db, transport=chain).collect("honest.meme.near")
    assert len(chain.calls) == first_calls        # nothing went to the network
    assert replay.surface["band"] == "low"

    client = JsonClient(settings, db, transport=chain)
    try:
        client.fetch("rpc", "GET", "https://never.cached/")
        assert False, "offline fetch of uncached URL should fail"
    except FetchError:
        pass


def test_global_contract_binding_detected(tmp_path):
    chain = FakeChain()
    chain.add_token("follower.near", keys=[], supply=10, wasm=build_wasm(["input"], NEP141),
                    holders=[("x.near", 10)], global_account="factory.near")
    _, _, collector = make(tmp_path, chain)
    res = collector.collect("follower.near", CollectOptions(with_nearblocks=False))
    assert res.surface["band"] == "high"
    assert "factory.near" in res.surface["reasons"][0]

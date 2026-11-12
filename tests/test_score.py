from fastapi.testclient import TestClient

from rugguard.analysis.score import ScoreInputs, ScoreRules, auc, deployer_cluster, score
from rugguard.analysis.transfers import Transfer
from rugguard.analysis.wasm import analyse_wasm
from rugguard.api import create_app
from rugguard.config import Settings
from rugguard.storage.db import Database
from rugguard.scoring import evaluate

from .fakes import NEP141, build_wasm
from .test_cli import CliWorld
from .test_outcome import H, NEAR, POOL, T0, launch_and_distribution, t

SUPPLY = 1_000_000
RULES = ScoreRules()
CLEAN_FLAGS = analyse_wasm(build_wasm(["input"], NEP141))["flags"]
SMITHII_FLAGS = analyse_wasm(build_wasm(["input"], NEP141 + ["mint", "pause", "set_blacklisted",
                                                              "transfer_ownership"]))["flags"]


def pool(tok, near):
    return {"pool_id": 1, "token_account_ids": ["meme.near", "wrap.near"], "amounts": [str(tok), str(int(near * NEAR))]}


def inputs(transfers, *, flags=CLEAN_FLAGS, band="low", p=None, contracts=("pad.near",), complete=True,
           now_h=240, deploys=()):
    return ScoreInputs("meme.near", sorted(transfers, key=lambda x: x.ts), complete, SUPPLY, band,
                       {"code_flags": {k: v for k, v in flags.items() if v}}, flags, p, set(contracts),
                       list(deploys), T0 + int(now_h * H))


def sinu_like():
    """Like sinu-8.smithii.near: the deployer wallet mints everything and seeds a tiny pool."""
    return [t(-2, None, "thinos.near", SUPPLY, "mint"),
            t(0, "thinos.near", POOL, 400, "L"),
            t(0.5, POOL, "buyer.near", 200)]


def test_sinu_like_token_scores_high():
    r = score(inputs(sinu_like(), flags=SMITHII_FLAGS, band="medium", p=pool(200, 3), contracts=()), RULES)
    comps = r["facts"]["components"]
    assert r["band"] == "high" and r["score"] >= 60, r
    assert comps["deployer"]["subscore"] == 1.0 and comps["deployer"]["origin"] == "thinos.near"
    assert comps["liquidity"]["subscore"] >= 0.7
    assert comps["owner_powers"]["subscore"] == 1.0
    assert r["coverage"] == 1.0
    assert r["facts"]["weighted_score"] < 70          # the plain average is lifted by the floor
    assert any("drain the pool" in x for x in r["facts"]["floors_applied"])


def test_floors_never_lower_a_score_and_need_a_thin_pool():
    deep = score(inputs(sinu_like(), flags=SMITHII_FLAGS, band="medium", p=pool(200, 3), contracts=()), RULES)
    rules = ScoreRules(floor_drain=10, floor_upgrade=10)
    assert score(inputs(sinu_like(), flags=SMITHII_FLAGS, band="medium", p=pool(200, 3), contracts=()),
                 rules)["score"] == deep["facts"]["weighted_score"]


def test_launchpad_token_with_fair_distribution_scores_low():
    tr = launch_and_distribution()      # pad.near (a contract) seeds pool with 30%, spreads the rest
    tr = [x for x in tr if x.receiver not in ("dev.near", "whale.near")] + \
        [t(0.2, "pad.near", f"v{i}.near", 10_000) for i in range(30)]
    r = score(inputs(tr, p=pool(300_000, 5000)), RULES)
    comps = r["facts"]["components"]
    assert comps["deployer"]["subscore"] is None          # supply started in a contract
    assert r["band"] == "low", r
    assert r["coverage"] == 0.9


def test_dump_after_cut_off_does_not_change_the_score():
    """The honesty test: the score may only see data up to launch + cut_hours."""
    base = launch_and_distribution()
    before = score(inputs(base, p=pool(300_000, 4000)), RULES)
    after = score(inputs(base + [t(5, "dev.near", POOL, 190_000)], p=pool(300_000, 4000)), RULES)
    assert before["score"] == after["score"]


def test_first_hour_dump_by_linked_wallet_is_seen():
    tr = [t(-2, None, "dev.near", SUPPLY, "mint"),
          t(-1, "dev.near", "alt1.near", 100_000), t(-1, "dev.near", "alt2.near", 100_000),
          t(0, "dev.near", POOL, 300_000, "L"),
          t(0.2, POOL, "alt1.near", 50_000), t(0.3, POOL, "alt2.near", 40_000), t(0.4, POOL, "rando.near", 5_000)]
    r = score(inputs(tr, p=pool(205_000, 4000), contracts=()), RULES)
    eb = r["facts"]["components"]["early_buyers"]
    assert eb["linked_share"] > 0.9 and eb["subscore"] == 1.0


def test_deployer_cluster_follows_pre_launch_seeding_only():
    tr = [t(-2, None, "dev.near", 100, "m"), t(-1, "dev.near", "a.near", 10), t(-0.5, "a.near", "b.near", 5),
          t(0, "dev.near", POOL, 50, "L"), t(1, "dev.near", "late.near", 5)]
    origin, cluster = deployer_cluster(sorted(tr, key=lambda x: x.ts), T0, {POOL})
    assert origin == "dev.near" and cluster == {"dev.near", "a.near", "b.near"}


def test_incomplete_history_reduces_coverage_not_invents_risk():
    r = score(inputs(launch_and_distribution(), complete=False, flags=SMITHII_FLAGS, band="medium"), RULES)
    assert r["coverage"] == 0.35      # only the code signals (25 + 10) need no transfer history
    assert all(r["facts"]["components"][k]["subscore"] is None
               for k in ("concentration", "liquidity", "early_buyers", "deployer", "lp_control"))


def test_redeploy_after_cut_is_noted():
    r = score(inputs(launch_and_distribution(), p=pool(300_000, 4000), deploys=[T0 + 10 * H]), RULES)
    assert "redeployed" in r["facts"]["note"]


def test_auc():
    assert auc([90, 80], [10, 20]) == 1.0
    assert auc([10], [90]) == 0.0
    assert auc([50], [50]) == 0.5
    assert auc([], [1]) is None


def test_score_job_and_evaluation_via_api(tmp_path):
    s = Settings(db_path=tmp_path / "s.db", max_retries=0)
    app = create_app(s, transport=CliWorld(), start_worker=False)
    c = TestClient(app)
    for kind in ("outcome", "score"):
        assert c.post(f"/api/tokens/meme.near/{kind}").status_code == 202
    while app.state.jobs.run_one(timeout=0.01):
        pass
    view = c.get("/api/tokens/meme.near").json()
    total = next(x for x in view["scores"] if x["kind"] == "total")
    assert total["score"] is not None and set(total["facts"]["components"]) == set(RULES.weights)
    # pad.near had handed out its whole balance by the cut-off; it is a contract, not a deployer wallet
    assert total["facts"]["components"]["deployer"]["subscore"] is None
    ev = c.get("/api/evaluation").json()
    assert ev["rows"][0]["token_id"] == "meme.near" and ev["rows"][0]["score"] == total["score"]


def test_evaluate_counts_catches_and_false_alarms(tmp_path):
    db = Database(tmp_path / "e.db")
    db.init()
    for token, label, value in (("r1.near", "rugged", 80), ("r2.near", "rugged", 40),
                                ("f1.near", "faded", 20), ("a1.near", "alive", 70), ("p1.near", "pending", 90)):
        db.upsert_token(token, {}, "1")
        sid = db.new_snapshot(token, "mainnet", None)
        db.put_score(sid, "total", value, "x", [], {}, "score-v1")
        db.put_outcome(token, label, "outcome-rules-v1", [], {})
    ev = evaluate(db)
    assert (ev["judged"], ev["excluded"], ev["rugged"]) == (4, 1, 2)
    assert (ev["caught"], ev["false_alarms"]) == (1, 1)
    assert ev["auc"] == 0.75        # 3 of 4 rugged/non-rugged pairs ranked correctly
    db.put_review("r2.near", "faded", "arbitrage, not a pull", None)
    assert evaluate(db)["rugged"] == 1 and evaluate(db, use_human=False)["rugged"] == 2
    sub = evaluate(db, {"r1.near", "f1.near"})          # e.g. the holdout set only
    assert (sub["judged"], sub["caught"], sub["false_alarms"], sub["auc"]) == (2, 1, 0, 1.0)



def test_lp_control_wallet_vs_launchpad():
    """amd.amemedog.near: liquidity added by a wallet, later withdrawn; meme.cooking-style pads lock it."""
    wallet = score(inputs(sinu_like(), p=pool(200, 3), contracts=()), RULES)
    pad = score(inputs(launch_and_distribution(), p=pool(300_000, 4000)), RULES)   # pad.near is a contract
    assert wallet["facts"]["components"]["lp_control"]["subscore"] == 1.0
    assert pad["facts"]["components"]["lp_control"]["subscore"] == 0.0

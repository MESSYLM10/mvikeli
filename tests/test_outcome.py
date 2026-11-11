import base64
import json

import pytest

from rugguard.analysis.outcome import OutcomeInputs, OutcomeRules, cohen_kappa, label
from rugguard.analysis.transfers import Transfer, normalise, normalise_all, replay
from rugguard.collect import Collector
from rugguard.config import Settings
from rugguard.postmortem import run_outcome
from rugguard.storage.db import Database

H = 3600 * 10**9
T0 = 1_760_000_000 * 10**9          # launch time (ns)
SUPPLY = 1_000_000
POOL = "v2.ref-finance.near"
NEAR = 10**24
RULES = OutcomeRules()


def t(hours, sender, receiver, amount, tx=None):
    return Transfer(T0 + int(hours * H), sender, receiver, amount, tx or f"tx{hours}{sender}{receiver}", "TRANSFER")


def launch_and_distribution():
    """Launchpad (a contract) seeds the pool with 300k and hands out 700k."""
    return [
        t(-1, None, "pad.near", SUPPLY, "mint"),
        t(0, "pad.near", POOL, 300_000, "L"),
        t(0.1, "pad.near", "dev.near", 200_000),
        t(0.1, "pad.near", "whale.near", 100_000),
        *[t(0.2, "pad.near", f"u{i}.near", 10_000) for i in range(40)],
    ]


def pool(token_reserve, near):
    return {"pool_id": 7, "token_account_ids": ["meme.near", "wrap.near"],
            "amounts": [str(token_reserve), str(int(near * NEAR))]}


def inputs(transfers, pool_state, *, age_h=240, complete=True, pools_found=1, deploys=(), contracts=("pad.near",)):
    return OutcomeInputs("meme.near", sorted(transfers, key=lambda x: x.ts), complete, SUPPLY, pool_state,
                         pools_found, set(contracts), list(deploys), T0 + int(age_h * H))


# ------------------------------------------------------------------ transfers

def test_normalise_signed_delta_rows_and_dedupe_both_sides():
    rows = [
        {"affected_account_id": "a.near", "involved_account_id": "b.near", "delta_amount": "-50",
         "transaction_hash": "x", "block_timestamp": "10"},
        {"affected_account_id": "b.near", "involved_account_id": "a.near", "delta_amount": "50",
         "transaction_hash": "x", "block_timestamp": "10"},
        {"affected_account_id": "c.near", "involved_account_id": None, "delta_amount": "7",
         "cause": "MINT", "transaction_hash": "m", "block_timestamp": "5"},
    ]
    out = normalise_all(rows)
    assert [(x.sender, x.receiver, x.amount) for x in out] == [(None, "c.near", 7), ("a.near", "b.near", 50)]


def test_normalise_explicit_from_to():
    x = normalise({"from": "a", "to": "b", "amount": "9", "block_timestamp": "1"})
    assert (x.sender, x.receiver, x.amount) == ("a", "b", 9)


def test_replay_tracks_peaks():
    bal, peak, _ = replay([t(0, None, "a", 10), t(1, "a", "b", 7), t(2, None, "a", 1)])
    assert bal == {"a": 4, "b": 7} and peak["a"] == 10


# --------------------------------------------------------------------- labels

def test_spoiled_when_never_reached_a_pool():
    r = label(inputs([t(-1, None, "pad.near", SUPPLY)], None, pools_found=0), RULES)
    assert r.label == "spoiled"


def test_incomplete_history_needs_review():
    r = label(inputs(launch_and_distribution(), pool(300_000, 4000), complete=False), RULES)
    assert r.label == "needs_review" and "max-pages" in r.evidence[0]


def test_liquidity_removed_is_rugged():
    r = label(inputs(launch_and_distribution(), pool(10, 0.3)), RULES)
    assert r.label == "rugged" and any("Liquidity removed" in e for e in r.evidence)


def test_collapse_with_insider_exit_is_rugged_with_evidence():
    tr = launch_and_distribution() + [t(2, "dev.near", POOL, 120_000), t(5, "dev.near", POOL, 70_000)]
    r = label(inputs(tr, pool(1_200_000, 300)), RULES)        # (300k/1.2M)^2 = 6% of launch price
    assert r.label == "rugged"
    exit_line = next(e for e in r.evidence if e.startswith("Insider exit: dev.near"))
    assert "20% of supply" in exit_line and "95%" in exit_line and "5.0 h" in exit_line
    assert r.facts["insider_exit"] and r.facts["collapsed"]


def test_launchpad_contract_is_not_an_insider():
    tr = launch_and_distribution()          # pad.near sends 1M out: would look like a giant exit
    r = label(inputs(tr, pool(1_200_000, 300)), RULES)
    assert not any("pad.near" in e for e in r.evidence if e.startswith("Insider exit"))


def test_collapse_without_exit_is_faded():
    tr = launch_and_distribution() + [t(200, f"u{i}.near", POOL, 9_000) for i in range(40)]  # late, small sells
    r = label(inputs(tr, pool(1_200_000, 300)), RULES)
    assert r.label == "faded"
    assert any("none sold" in e for e in r.evidence)


def test_young_token_is_pending():
    r = label(inputs(launch_and_distribution(), pool(300_000, 4000), age_h=20), RULES)
    assert r.label == "pending"


def test_young_token_already_rugged_is_not_pending():
    tr = launch_and_distribution() + [t(1, "dev.near", POOL, 190_000)]
    r = label(inputs(tr, pool(1_200_000, 300), age_h=20), RULES)
    assert r.label == "rugged"


def test_redeploy_after_launch_plus_collapse_is_rugged():
    r = label(inputs(launch_and_distribution(), pool(1_200_000, 300), deploys=[T0 + 30 * H]), RULES)
    assert r.label == "rugged" and r.facts["redeploys_after_launch"] == 1


def test_healthy_token_is_alive():
    r = label(inputs(launch_and_distribution(), pool(350_000, 3500)), RULES)
    assert r.label == "alive" and r.facts["price_ratio"] > 0.7


def test_non_wnear_pool_needs_review():
    p = {"pool_id": 1, "token_account_ids": ["meme.near", "usdt.near"], "amounts": ["1", "1"]}
    assert label(inputs(launch_and_distribution(), p), RULES).label == "needs_review"


# ----------------------------------------------------------------------- rules

def test_rules_override_and_reject_unknown_keys(tmp_path):
    f = tmp_path / "r.json"
    f.write_text(json.dumps({"version": "strict", "collapse_ratio": 0.5}))
    assert OutcomeRules.load(f).collapse_ratio == 0.5
    f.write_text(json.dumps({"colapse_ratio": 0.5}))
    with pytest.raises(ValueError):
        OutcomeRules.load(f)


def test_cohen_kappa():
    assert cohen_kappa([("a", "a"), ("b", "b")]) == 1.0
    assert cohen_kappa([("a", "b"), ("b", "a")]) == -1.0
    k = cohen_kappa([("rugged", "rugged")] * 6 + [("faded", "faded")] * 3 + [("rugged", "faded")])
    assert 0.7 < k < 0.8
    assert cohen_kappa([]) is None


# ------------------------------------------------------------ pipeline (fakes)

class FakeWorld:
    """NearBlocks history in 3 pages + Rhea pools + RPC, for one launched-then-rugged token."""

    def __init__(self):
        self.rows = []
        self.transfers = launch_and_distribution() + [t(2, "dev.near", POOL, 190_000)]
        self.fastnear_queries: list[str] = []
        for tr in self.transfers:
            self.rows.append({"event_index": f"{tr.tx}-{tr.receiver}", "affected_account_id": tr.receiver,
                              "involved_account_id": tr.sender, "delta_amount": str(tr.amount),
                              "transaction_hash": tr.tx, "block_timestamp": str(tr.ts), "cause": "TRANSFER"})
        self.rows.reverse()                  # API is newest-first
        self.history_calls = 0

    def __call__(self, method, url, headers, body, timeout):
        if "transfers.main.fastnear.com" in url:
            return 200, self.fastnear_transfers(body)
        if "/v1/ft/" in url and url.endswith("/top"):
            bal: dict = {}
            for tr in self.transfers:
                if tr.sender:
                    bal[tr.sender] = bal.get(tr.sender, 0) - tr.amount
                if tr.receiver:
                    bal[tr.receiver] = bal.get(tr.receiver, 0) + tr.amount
            top = sorted(((a, b) for a, b in bal.items() if b > 0), key=lambda x: x[1], reverse=True)[:100]
            return 200, {"accounts": [{"account_id": a, "balance": str(b)} for a, b in top]}
        if method == "POST":
            return 200, self.rpc(body["params"])
        if "/txns" in url:
            self.history_calls += 1
            page = int(url.split("next=")[1].split("&")[0]) if "next=" in url else 0
            size = 20
            chunk = self.rows[page * size:(page + 1) * size]
            more = (page + 1) * size < len(self.rows)
            return 200, {"txns": chunk, **({"cursor": {"next": str(page + 1)}} if more else {})}
        if "/contract/deployments" in url:
            return 200, {"deployments": [{"transaction_hash": "d0", "block_timestamp": str(T0 - 5 * H)}]}
        return 404, {}

    LAUNCH_NEAR = 4000 * NEAR       # wNEAR pad.near put into the pool at launch

    def fastnear_transfers(self, body):
        """FastNEAR /v0/transfers: one account's transfers of one asset, paged, either order."""
        me = body["account_id"]
        if body.get("resume_token") is None:
            self.fastnear_queries.append(f"{me}|{body['asset_id']}")
        if body["asset_id"] == "nep141:wrap.near":
            rows = [{"account_id": me, "other_account_id": POOL, "amount": str(-self.LAUNCH_NEAR),
                     "block_timestamp": str(T0 + 60 * 10**9), "transaction_id": "LN", "receipt_id": "rn",
                     "log_index": 0, "transfer_index": 0}] if me == "pad.near" else []
            return {"transfers": rows, "resume_token": None}
        mine = []
        for i, tr in enumerate(self.transfers):
            if me not in (tr.sender, tr.receiver):
                continue
            mine.append({"account_id": me, "other_account_id": tr.receiver if tr.sender == me else tr.sender,
                         "amount": str(-tr.amount if tr.sender == me else tr.amount),
                         "block_timestamp": str(tr.ts), "transaction_id": tr.tx, "receipt_id": f"r{i}",
                         "log_index": 0, "transfer_index": i, "asset_id": body["asset_id"],
                         "method_name": "ft_transfer", "transfer_type": "FtTransfer"})
        if body.get("desc"):
            mine.reverse()
        start = int(body.get("resume_token") or 0)
        limit = body.get("limit", 200)
        chunk = mine[start:start + limit]
        more = start + limit < len(mine)
        return {"transfers": chunk, "resume_token": str(start + limit) if more else None}

    def rpc(self, p):
        def ok(r):
            return {"jsonrpc": "2.0", "id": "x", "result": r}

        def view(v):
            return ok({"result": list(json.dumps(v).encode())})
        if p["request_type"] == "call_function":
            m = p["method_name"]
            if m == "ft_total_supply":
                return view(str(SUPPLY))
            if m == "get_number_of_pools":
                return view(3)
            if m == "get_pools":
                return view([
                    {"pool_kind": "SIMPLE_POOL", "token_account_ids": ["x.near", "wrap.near"], "amounts": ["1", "1"]},
                    {"pool_kind": "SIMPLE_POOL", "token_account_ids": ["meme.near", "wrap.near"],
                     "amounts": ["1200000", str(300 * NEAR)]},
                    {"pool_kind": "SIMPLE_POOL", "token_account_ids": ["meme.near", "wrap.near"],
                     "amounts": ["5", str(1 * NEAR)]},
                ])
        if p["request_type"] == "view_account":
            code = "PadCode1111111111111111111111111" if p["account_id"] == "pad.near" else \
                "11111111111111111111111111111111"
            return ok({"code_hash": code, "amount": "1", "locked": "0", "storage_usage": 1})
        return {"jsonrpc": "2.0", "id": "x", "error": {"name": "UNSUPPORTED"}}


def make(tmp_path, world):
    s = Settings(db_path=tmp_path / "o.db", max_retries=0)
    db = Database(s.db_path)
    db.init()
    return db, Collector(s, db, transport=world)


def test_pipeline_rugged_end_to_end(tmp_path):
    world = FakeWorld()
    db, c = make(tmp_path, world)
    result, errors = run_outcome(c, "meme.near", RULES, page_size=20, max_pages=10, now_ns=T0 + 240 * H)
    assert errors == []
    assert result.label == "rugged"
    assert result.facts["pool_id"] == 1                    # picked the deep pool, not the dust pool
    assert any(e.startswith("Insider exit: dev.near") for e in result.evidence)
    assert db.history_run("meme.near")["complete"] == 1
    assert db.outcome_table()[0]["auto_label"] == "rugged"


def test_pipeline_rugged_end_to_end_via_nearblocks(tmp_path):
    world = FakeWorld()
    db, c = make(tmp_path, world)
    c.settings.history_source = "nearblocks"
    result, errors = run_outcome(c, "meme.near", RULES, page_size=20, max_pages=10, now_ns=T0 + 240 * H)
    assert errors == [] and result.label == "rugged"
    # NearBlocks supplies the history; FastNEAR is asked only for the launcher's wNEAR deposit.
    assert world.fastnear_queries == ["pad.near|nep141:wrap.near"]


def test_fastnear_history_queries_only_the_accounts_that_matter(tmp_path):
    world = FakeWorld()
    db, c = make(tmp_path, world)
    run_outcome(c, "meme.near", RULES, now_ns=T0 + 240 * H)
    q = [x.split("|")[0] for x in world.fastnear_queries if x.endswith("meme.near")]
    assert q[0] == POOL and q[1] == "pad.near"           # pool first, then the launcher
    assert "dev.near" in q                                # the seller that dumped
    assert len(q) < 25                                    # not one per holder (there are 42)


def test_pipeline_measures_launch_price_from_the_wnear_deposit(tmp_path):
    world = FakeWorld()
    db, c = make(tmp_path, world)
    result, _ = run_outcome(c, "meme.near", RULES, now_ns=T0 + 240 * H)
    # pool now 300 NEAR / 1.2M tokens vs launch 4,000 NEAR / 300k tokens -> 1.9% of launch price
    assert result.facts["price_method"] == "measured"
    assert abs(result.facts["price_ratio"] - 0.01875) < 1e-6


def test_pipeline_incomplete_history_then_resume(tmp_path):
    world = FakeWorld()
    db, c = make(tmp_path, world)
    c.settings.history_source = "nearblocks"
    first, _ = run_outcome(c, "meme.near", RULES, page_size=20, max_pages=1, now_ns=T0 + 240 * H)
    assert first.label == "needs_review"
    second, _ = run_outcome(c, "meme.near", RULES, page_size=20, max_pages=10, now_ns=T0 + 240 * H)
    assert second.label == "rugged"


def test_review_and_agreement(tmp_path):
    world = FakeWorld()
    db, c = make(tmp_path, world)
    run_outcome(c, "meme.near", RULES, page_size=20, max_pages=10, now_ns=T0 + 240 * H)
    db.put_review("meme.near", "rugged", "dev dumped 19% in 2 h", "johnson")
    row = db.outcome_table()[0]
    assert (row["auto_label"], row["human_label"]) == ("rugged", "rugged")



def test_refunded_deposit_is_not_the_launch():
    """sinu-8.smithii.near: a 10,000-token deposit refunded in the same transaction came first."""
    from rugguard.analysis.transfers import find_launch
    tr = [t(-1, "dev.near", POOL, 10_000, "R1"), t(-1, POOL, "dev.near", 10_000, "R1"),
          t(0, "dev.near", POOL, 392, "L")]
    launch, reserve = find_launch(sorted(tr, key=lambda x: x.ts), POOL)
    assert launch.tx == "L" and reserve == 392


def test_untrustworthy_estimate_goes_to_review():
    """sinu-8: the token-only estimate said 283,248% because liquidity changed; never report that."""
    r = label(inputs(launch_and_distribution(), pool(1_000, 20)), RULES)   # (300k/1k)^2 = 90,000x
    assert r.label == "needs_review" and "cannot be trusted" in r.evidence[-1]


def test_drained_liquidity_is_rugged_even_when_price_holds():
    """smithii.near series: 372 NEAR in at launch, 20 NEAR left, price roughly unchanged."""
    inp = inputs(launch_and_distribution(), pool(15_000, 200))      # both sides down to 5%: price unchanged
    inp.launch_near = 4000 * NEAR
    r = label(inp, RULES)
    assert r.facts["price_method"] == "measured" and not r.facts["collapsed"]
    assert r.label == "rugged" and any(e.startswith("Liquidity drained") for e in r.evidence)


def test_small_but_intact_pool_is_not_called_removed():
    """lumos.lumoscore.near: seeded with 0.30 NEAR and still holding 0.30 NEAR."""
    inp = inputs(launch_and_distribution(), pool(300_000, 0.3))
    inp.launch_near = int(0.3 * NEAR)
    assert label(inp, RULES).label == "alive"


def test_empty_token_side_does_not_crash():
    """11 Nov holdout: an implicit-account token whose pool held 0 tokens raised ZeroDivisionError."""
    inp = inputs(launch_and_distribution(), pool(0, 0.0))
    inp.launch_near = 4000 * NEAR
    r = label(inp, RULES)
    assert r.label == "rugged" and r.facts["price_ratio"] is None
    inp = inputs(launch_and_distribution(), pool(0, 5000))
    inp.launch_near = 4000 * NEAR
    assert label(inp, RULES).label == "needs_review"


def test_old_tokens_are_not_judged_as_launches():
    """nico131.tkn.near first traded 751 days ago; an emptied pool now is not a launch rug."""
    r = label(inputs(launch_and_distribution(), pool(10, 0.0), age_h=751 * 24), RULES)
    assert r.label == "needs_review" and "judges launches" in r.evidence[-1]




def test_rings_find_wallets_trading_a_series_of_tokens():
    from rugguard.analysis.rings import find_rings
    hist = {}
    for i, token in enumerate(["a-1.f.near", "b-2.f.near", "c-3.f.near", "solo.near"]):
        rows = [t(1, POOL, "bot1.near", 5), t(2, "bot2.near", POOL, 5), t(3, POOL, f"fresh{i}.near", 5)]
        if token == "solo.near":
            rows = [t(1, POOL, "indie.near", 5)]
        hist[token] = rows
    found = find_rings(hist, {POOL}, min_tokens=3)
    assert {w["wallet"] for w in found["wallets"]} == {"bot1.near", "bot2.near"}
    assert found["tokens"]["a-1.f.near"]["ring_share"] == round(2 / 3, 3)
    assert found["tokens"]["solo.near"]["ring_members"] == 0

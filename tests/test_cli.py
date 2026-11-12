import csv
"""CLI behaviour found missing in the first real use (10 Nov 2026, evening)."""

import json

import pytest

import rugguard.http
from rugguard import cli
from rugguard.config import Settings

from .test_outcome import NEAR, T0, H, FakeWorld


class CliWorld(FakeWorld):
    """FakeWorld plus account checks for unknown names, and pools for `discover`."""

    def rpc(self, p):
        acct = p.get("account_id")
        if p["request_type"] == "view_account" and acct in ("some.meme.near",):
            return {"jsonrpc": "2.0", "id": "x", "error": {"name": "HANDLER_ERROR",
                                                          "cause": {"name": "UNKNOWN_ACCOUNT"}}}
        if p["request_type"] == "view_account" and acct == "meme.near":
            return {"jsonrpc": "2.0", "id": "x", "result": {"code_hash": "MemeCode111111111111111111111111"}}
        if p["request_type"] == "call_function" and p["method_name"] == "ft_metadata":
            return {"jsonrpc": "2.0", "id": "x", "result": {"result": list(json.dumps({"symbol": "M"}).encode())}}
        return super().rpc(p)


@pytest.fixture
def run(tmp_path, monkeypatch):
    world = CliWorld()
    monkeypatch.setattr(rugguard.http, "requests_transport", world)
    monkeypatch.setattr(Settings, "from_env", classmethod(
        lambda cls: cls(db_path=tmp_path / "cli.db", max_retries=0)))

    def _run(*argv):
        return cli.main(list(argv))
    return _run


def test_placeholder_token_is_rejected_and_nothing_stored(run, capsys):
    assert run("outcome", "some.meme.near") == 0
    assert "account does not exist" in capsys.readouterr().out
    run("outcomes")
    assert "No outcomes yet" in capsys.readouterr().out


def test_missing_seed_file_is_a_clear_message(run, capsys):
    assert run("outcome", "--seeds", "nope.txt") == 1
    assert "rugguard discover --out nope.txt" in capsys.readouterr().out


def test_review_requires_an_outcome_first(run, capsys):
    assert run("review", "meme.near", "faded", "--reason", "x") == 1
    assert "run `rugguard outcome meme.near` first" in capsys.readouterr().out


def test_discover_writes_seeds_that_outcome_can_read(run, capsys, tmp_path):
    seeds = tmp_path / "s.txt"
    assert run("discover", "--recent", "3", "--out", str(seeds)) == 0
    out = capsys.readouterr().out
    assert "meme.near" in out and "x.near" in out
    assert seeds.read_text().count("meme.near") == 1          # deepest of its two pools kept
    assert "# pool 1, 300.0 wNEAR" in seeds.read_text()
    assert run("discover", "--recent", "3", "--suffix", "meme.near") == 0
    assert "x.near" not in capsys.readouterr().out.split("newest first")[1]


def test_discover_excludes_dev_set_and_limits(run, capsys, tmp_path):
    dev = tmp_path / "dev.txt"
    dev.write_text("# dev set\nmeme.near  # already judged\n")
    holdout = tmp_path / "h.txt"
    assert run("discover", "--recent", "3", "--exclude", str(dev), "--limit", "5", "--out", str(holdout)) == 0
    assert "Excluded 1" in capsys.readouterr().out
    assert "meme.near" not in holdout.read_text() and "x.near" in holdout.read_text()


def test_forget_removes_outcomes_and_reviews(run, capsys):
    run("outcome", "meme.near", "--page-size", "20")
    run("review", "meme.near", "rugged", "--reason", "dev dumped")
    capsys.readouterr()
    run("forget", "meme.near")
    assert "removed 2" in capsys.readouterr().out


# ---- NearBlocks free plan (11 Nov 2026: a 14-token run hit 429 after two tokens) ----

class RateLimitedWorld(CliWorld):
    """NearBlocks answers 429 to everything; RPC still works."""

    def __call__(self, method, url, headers, body, timeout):
        if method != "POST" and "nearblocks" in url:
            return 429, {"message": "rate limited"}
        return super().__call__(method, url, headers, body, timeout)


@pytest.fixture
def run_limited(tmp_path, monkeypatch):
    world = RateLimitedWorld()
    monkeypatch.setattr(rugguard.http, "requests_transport", world)
    monkeypatch.setattr(rugguard.http.JsonClient, "_throttle", lambda self, source: None)
    monkeypatch.setattr(Settings, "from_env", classmethod(
        lambda cls: cls(db_path=tmp_path / "rl.db", max_retries=0, history_source="nearblocks")))
    return lambda *argv: cli.main(list(argv)), world


def test_rate_limit_stops_the_batch_with_honest_message(run_limited, capsys, tmp_path):
    run, world = run_limited
    seeds = tmp_path / "s.txt"
    seeds.write_text("meme.near\nx2.near\nx3.near\n")
    run("outcome", "--seeds", str(seeds))
    out = capsys.readouterr().out
    assert "NearBlocks rate limit: the transfer history could not be fetched yet" in out
    assert "Stopped after 1 of 3 token(s)" in out
    assert "x2.near" not in out.split("Stopped after")[0]


def test_circuit_breaker_fails_fast_without_calling_again(tmp_path):
    from rugguard.http import FetchError, JsonClient
    from rugguard.storage.db import Database
    calls = []

    def always_429(method, url, headers, body, timeout):
        calls.append(url)
        return 429, {}
    s = Settings(db_path=tmp_path / "cb.db", max_retries=0)
    db = Database(s.db_path)
    db.init()
    c = JsonClient(s, db, always_429, sleep=lambda x: None)
    for _ in range(3):
        with pytest.raises(FetchError, match="rate limit"):
            c.fetch("nearblocks", "GET", "https://api.nearblocks.io/v3/x")
    assert len(calls) == 1 and c.rate_limited("nearblocks")


def test_free_plan_uses_25_row_pages():
    assert Settings().nearblocks_page_size == 25
    assert Settings(nearblocks_api_key="k").nearblocks_page_size == 100


def test_inspect_summarises_stored_history_without_api_calls(run, capsys):
    run("outcome", "meme.near")
    capsys.readouterr()
    assert run("inspect", "meme.near") == 0
    out = capsys.readouterr().out
    assert "history complete" in out and "v2.ref-finance.near" in out and "raw row fields:" in out


def test_evaluate_on_a_seeds_subset(run, capsys, tmp_path):
    run("outcome", "meme.near", "--page-size", "20")
    run("score", "meme.near", "--page-size", "20")
    seeds = tmp_path / "h.txt"
    seeds.write_text("meme.near\n")
    capsys.readouterr()
    stem = tmp_path / "runs" / "eval-h"
    assert run("evaluate", "--seeds", str(seeds), "--out", str(stem)) == 0
    out = capsys.readouterr().out
    assert "Evaluating only the 1 token(s)" in out and "meme.near" in out
    rows = list(csv.DictReader(open(stem.with_suffix(".csv"))))
    assert len(rows) == 1 and rows[0]["token_id"] == "meme.near" and rows[0]["token_set"] == "h"
    assert rows[0]["outcome_rules"].startswith("outcome-rules") and rows[0]["score_rules"].startswith("score")
    assert rows[0]["snapshot_id"] and rows[0]["verdict"] in ("caught", "missed", "false_alarm", "correct_clear",
                                                             "excluded")
    md = stem.with_suffix(".md").read_text()
    assert "# RugGuard evaluation: h" in md and "`meme.near`" in md and "eval-h.csv" in md
    assert "--seeds" in md                   # the command that produced it is recorded


def test_evaluate_report_counts_match(tmp_path):
    from rugguard.report import detail_rows, markdown
    from rugguard.scoring import evaluate
    from rugguard.storage.db import Database
    db = Database(tmp_path / "e.db")
    db.init()
    for token, label, value in (("r1.near", "rugged", 80), ("r2.near", "rugged", 40), ("a1.near", "alive", 70),
                                ("a2.near", "alive", 10), ("p1.near", "needs_review", 90)):
        db.upsert_token(token, {}, "1")
        sid = db.new_snapshot(token, "mainnet", None)
        db.put_score(sid, "total", value, "x", ["why"], {"coverage": 1.0}, "score-v2")
        db.put_outcome(token, label, "outcome-rules-v2", ["ev line"], {"launch_tx": "TX"})
    ev = evaluate(db)
    rows = detail_rows(db, ev, "t", "RUN")
    v = {r["token_id"]: r["verdict"] for r in rows}
    assert v == {"r1.near": "caught", "r2.near": "missed", "a1.near": "false_alarm", "a2.near": "correct_clear",
                 "p1.near": "excluded"}
    md = markdown(ev, rows, token_set="t", run_id="RUN", command="rugguard evaluate", csv_name="t.csv", commit="abc")
    assert "| Rugged | 1 caught | 1 missed |" in md and "## Misses and false alarms" in md
    assert "outcome needs review" in md
    assert "| Flag every token | 2 of 2 | 2 of 2 | 0.50 |" in md


# ---- guard vault (testnet) ----

def test_guard_post_score_dry_run_uses_the_stored_score(run, capsys):
    run("score", "meme.near", "--page-size", "20")
    capsys.readouterr()
    assert run("guard", "post-score", "meme.near", "--as-token", "rug.rugguard-demo.testnet",
               "--vault", "vault.rugguard-demo.testnet", "--oracle", "oracle.rugguard-demo.testnet",
               "--dry-run") == 0
    out = capsys.readouterr().out
    assert "Posting rug.rugguard-demo.testnet:" in out and "Dry run" in out
    assert "set_score" in out and "sign-as oracle.rugguard-demo.testnet" in out and "testnet" in out
    assert "stand-in for mainnet meme.near" in out


def test_guard_swap_dry_run_and_validation(run, capsys):
    assert run("guard", "swap", "tok.testnet", "--amount", "1.5", "--pool", "7", "--vault", "v.testnet",
               "--agent", "agent.testnet", "--dry-run") == 0
    out = capsys.readouterr().out
    assert "request_swap" in out and "1500000000000000000000000" in out and "sign-as agent.testnet" in out
    assert "sign-with-keychain" in out
    assert run("guard", "swap", "tok.testnet", "--pool", "7", "--vault", "v.testnet", "--agent", "agent.testnet",
               "--key-file", "/tmp/agent.json", "--dry-run") == 0
    assert "sign-with-access-key-file /tmp/agent.json" in capsys.readouterr().out
    assert run("guard", "swap", "BAD TOKEN", "--pool", "7", "--vault", "v.testnet", "--agent", "a.testnet",
               "--dry-run") == 2
    assert run("guard", "post-score", "unscored.near", "--vault", "v.testnet", "--oracle", "o.testnet",
               "--dry-run") == 2
    assert "run `rugguard score" in capsys.readouterr().out


def test_to_yocto():
    from rugguard.guard import to_yocto
    assert to_yocto("1") == 10**24 and to_yocto("0.000001") == 10**18 and to_yocto(".5") == 5 * 10**23
    for bad in ("", "-1", "1e3", "1.2.3", "abc"):
        with pytest.raises(ValueError):
            to_yocto(bad)


def test_describe_keys_for_an_agent_account():
    from rugguard.cli import describe_keys
    fc = {"public_key": "ed25519:HxSzPz6n2YFf25jsG9XYQyJGxZ9NzXbxkLgVdcMjYwtx", "permission": "FunctionCall",
          "is_full": False, "receiver_id": "vault.rugguard-jd.testnet", "method_names": ["request_swap"],
          "allowance": str(5 * 10**23)}
    verdict, lines = describe_keys("agent.rugguard-jd.testnet", [fc])
    assert verdict.startswith("No full-access key") and "vault.rugguard-jd.testnet" in verdict
    assert "only request_swap on vault.rugguard-jd.testnet" in lines[0] and "0.5 NEAR" in lines[0]
    full = {**fc, "permission": "FullAccess", "is_full": True, "receiver_id": None, "method_names": []}
    verdict, _ = describe_keys("agent.rugguard-jd.testnet", [fc, full])
    assert verdict.startswith("1 full-access key")


# ---- offline demo ----

def test_demo_pack_then_offline_demo_and_rescore(run, capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert run("outcome", "meme.near", "--page-size", "20") == 0
    assert run("score", "meme.near", "--page-size", "20") == 0
    stored = cli.Database(tmp_path / "cli.db").query(
        "SELECT score FROM risk_scores WHERE kind='total' ORDER BY score_id DESC LIMIT 1")[0]["score"]
    assert run("demo-pack") == 0
    assert "Packed" in capsys.readouterr().out and (tmp_path / "demo/rugguard-demo.db.gz").exists()

    (tmp_path / "seeds").mkdir()
    (tmp_path / "seeds/holdout.txt").write_text("meme.near\n")

    def no_network(*a, **k):
        raise AssertionError("the demo must not touch the network")
    monkeypatch.setattr(rugguard.http, "requests_transport", no_network)
    assert run("demo") == 0
    out = capsys.readouterr().out
    assert "offline demo" in out and "Holdout" in out and "testnet.nearblocks.io/txns/iPXzqG4s" in out
    assert (tmp_path / "runs/demo-holdout.md").exists() and (tmp_path / "runs/demo-holdout.csv").exists()

    # the cached responses reproduce the stored score with the network switched off
    monkeypatch.setattr(Settings, "from_env", classmethod(
        lambda cls: cls(db_path=tmp_path / "data/demo.db", max_retries=0, offline=True)))
    assert run("score", "meme.near", "--page-size", "20") == 0
    rescored = cli.Database(tmp_path / "data/demo.db").query(
        "SELECT score FROM risk_scores WHERE kind='total' ORDER BY score_id DESC LIMIT 1")[0]["score"]
    assert rescored == stored


def test_demo_without_a_pack_explains(run, capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert run("demo") == 1
    assert "No demo pack" in capsys.readouterr().out


def test_outcomes_reports_rugged_vs_not_agreement(tmp_path, monkeypatch, capsys):
    from rugguard.storage.db import Database
    db_path = tmp_path / "k.db"
    db = Database(db_path)
    db.init()
    for token, auto, human in (("a.near", "rugged", "rugged"), ("b.near", "rugged", "rugged"),
                               ("c.near", "alive", "faded"), ("d.near", "rugged", "faded"),
                               ("e.near", "alive", "needs_review")):
        db.put_outcome(token, auto, "outcome-rules-v2", [], {})
        db.put_review(token, human, "x", None)
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: cls(db_path=db_path)))
    assert cli.main(["outcomes"]) == 0
    out = capsys.readouterr().out
    assert "raw agreement 40%" in out                      # 2 of 5 identical labels
    assert "Rugged vs not rugged, 4 judged by both: agreement 75%" in out

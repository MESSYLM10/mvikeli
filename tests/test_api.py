import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rugguard import logs
from rugguard.api import create_app
from rugguard.api.jobs import JobQueue
from rugguard.config import Settings
from rugguard.validate import InvalidAccountId, account_id

from .test_cli import CliWorld

STATIC = Path(__file__).parents[1] / "rugguard" / "api" / "static"


def make(tmp_path, **kw):
    s = Settings(db_path=tmp_path / "api.db", max_retries=0)
    app = create_app(s, transport=CliWorld(), start_worker=False, **kw)
    return app, TestClient(app)


def drain(app):
    while app.state.jobs.run_one(timeout=0.01):
        pass


# ------------------------------------------------------------------ validation

@pytest.mark.parametrize("good", ["wrap.near", "token.v2.ref-finance.near", "a1", "x_y-z.near", "0" * 64])
def test_valid_account_ids(good):
    assert account_id(good) == good


@pytest.mark.parametrize("bad", ["", "a", "Wrap.near", "../etc", "a..near", "-a.near", "a.near.", "a b", "x" * 65,
                                 "a.near?x=1", "a/near", "<script>"])
def test_invalid_account_ids(bad):
    with pytest.raises(InvalidAccountId):
        account_id(bad)


def test_api_rejects_invalid_token_before_any_request(tmp_path):
    app, c = make(tmp_path)
    r = c.get("/api/tokens/Wrap.NEAR/surface")
    assert r.status_code == 422 and "not a valid NEAR account ID" in r.json()["detail"]
    assert c.post("/api/tokens/..%2F..%2Fetc/collect").status_code in (404, 422)


# ------------------------------------------------------------------ headers and UI

def test_security_headers_on_ui_and_api(tmp_path):
    app, c = make(tmp_path)
    for path in ("/", "/api/health"):
        r = c.get(path)
        assert r.status_code == 200
        csp = r.headers["content-security-policy"]
        assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp and "'unsafe-inline'" not in csp
        assert r.headers["x-content-type-options"] == "nosniff"
    assert c.get("/api/health").headers["cache-control"] == "no-store"
    docs = c.get("/docs").headers["content-security-policy"]
    assert "cdn.jsdelivr.net" in docs and "frame-ancestors 'none'" in docs
    assert "jsdelivr" not in c.get("/").headers["content-security-policy"]


def test_ui_never_injects_html():
    js = (STATIC / "app.js").read_text()
    for sink in (".innerHTML", ".outerHTML", "insertAdjacentHTML(", "document.write(", "eval(", "new Function("):
        assert sink not in js, sink
    # replaceChildren(null) renders the text "null": only the null-safe helper may call it
    assert js.count(".replaceChildren(") == 1 and "function setChildren" in js
    html = (STATIC / "index.html").read_text()
    assert "<script>" not in html and "style=" not in html and "https://" not in html


# ------------------------------------------------------------------ jobs

def test_outcome_job_round_trip(tmp_path):
    app, c = make(tmp_path)
    r = c.post("/api/tokens/meme.near/outcome")
    assert r.status_code == 202
    job_id = r.json()["job"]["id"]
    again = c.post("/api/tokens/meme.near/outcome")
    assert again.status_code == 200 and again.json()["job"]["id"] == job_id      # no duplicate
    drain(app)
    job = c.get(f"/api/jobs/{job_id}").json()
    assert job["status"] == "done", job
    view = c.get("/api/tokens/meme.near").json()
    assert view["outcome"]["label"] in ("rugged", "needs_review")
    assert view["outcome"]["evidence"]
    assert c.get("/api/outcomes").json()["rows"][0]["token_id"] == "meme.near"


def test_placeholder_token_job_is_rejected_not_stored(tmp_path):
    app, c = make(tmp_path)
    job_id = c.post("/api/tokens/some.meme.near/outcome").json()["job"]["id"]
    drain(app)
    assert "does not exist" in c.get(f"/api/jobs/{job_id}").json()["result"]["rejected"]
    assert c.get("/api/outcomes").json()["rows"] == []


def test_per_client_queue_limit(tmp_path):
    app, c = make(tmp_path)
    for t in ("a1.near", "a2.near", "a3.near"):
        assert c.post(f"/api/tokens/{t}/collect").status_code == 202
    r = c.post("/api/tokens/a4.near/collect")
    assert r.status_code == 429 and "limit is 3" in r.json()["detail"]


def test_failed_job_does_not_kill_worker():
    q = JobQueue(lambda job: 1 / 0 if job.target == "bad" else "ok")
    bad, _ = q.submit("x", "bad", "c")
    good, _ = q.submit("x", "good", "c")
    q.run_one(0.01)
    q.run_one(0.01)
    assert bad.status == "failed" and "ZeroDivisionError" in bad.error
    assert good.status == "done" and good.result == "ok"


def test_discover_job(tmp_path):
    app, c = make(tmp_path)
    job_id = c.post("/api/discover?recent=10").json()["job"]["id"]
    drain(app)
    tokens = c.get(f"/api/jobs/{job_id}").json()["result"]["tokens"]
    assert {t["token_id"] for t in tokens} == {"meme.near", "x.near"}


# ------------------------------------------------------------------ writes and logs

def test_review_needs_admin_key_when_configured(tmp_path):
    app, c = make(tmp_path, admin_token="s3cret")
    c.post("/api/tokens/meme.near/outcome")
    drain(app)
    body = {"label": "rugged", "reason": "dev dumped early"}
    assert c.post("/api/tokens/meme.near/review", json=body).status_code == 401
    assert c.post("/api/tokens/meme.near/review", json=body,
                  headers={"Authorization": "Bearer wrong"}).status_code == 401
    ok = c.post("/api/tokens/meme.near/review", json=body, headers={"Authorization": "Bearer s3cret"})
    assert ok.status_code == 200
    assert c.get("/api/logs").status_code == 401
    assert c.get("/api/health").json()["writes_need_key"] is True


def test_review_validation(tmp_path):
    app, c = make(tmp_path)
    assert c.post("/api/tokens/meme.near/review", json={"label": "rugged", "reason": "x" * 600}).status_code == 422
    assert c.post("/api/tokens/meme.near/review",
                  json={"label": "rugged", "reason": "no outcome yet"}).status_code == 409
    c.post("/api/tokens/meme.near/outcome")
    drain(app)
    assert c.post("/api/tokens/meme.near/review", json={"label": "nonsense", "reason": "abc"}).status_code == 422


def test_rate_limit(tmp_path):
    app, c = make(tmp_path, rate_per_minute=5)
    codes = [c.get("/api/health").status_code for _ in range(7)]
    assert codes[:5] == [200] * 5 and codes[5:] == [429, 429]


def test_forwarded_for_ignored_unless_trusted(tmp_path):
    app, c = make(tmp_path, rate_per_minute=2, trust_proxy=False)
    codes = [c.get("/api/health", headers={"X-Forwarded-For": f"10.0.0.{i}"}).status_code for i in range(3)]
    assert codes[-1] == 429      # spoofed addresses do not reset the limit


def test_logs_record_source_calls_without_secrets(tmp_path):
    app, c = make(tmp_path)
    c.get("/api/tokens/meme.near/surface")
    entries = c.get("/api/logs?n=200").json()["entries"]
    fetches = [e for e in entries if e["event"] == "fetch"]
    assert fetches and all("source" in e and "what" in e for e in fetches)
    assert all("Authorization" not in json.dumps(e) and "Bearer" not in json.dumps(e) for e in entries)


def test_results_and_offline_flag(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "seeds").mkdir()
    (tmp_path / "seeds/holdout.txt").write_text("meme.near\n")
    s = Settings(db_path=tmp_path / "r.db", max_retries=0, offline=True)
    app = create_app(s, transport=CliWorld(), start_worker=False)
    c = TestClient(app)
    assert c.get("/api/health").json()["offline"] is True
    r = c.get("/api/results").json()
    assert r["sets"] == []                                # nothing judged yet
    assert len(r["guard"]) == 3 and r["guard"][0]["url"].startswith("https://testnet.nearblocks.io/txns/")

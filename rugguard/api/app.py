"""Mvikeli HTTP API and web UI (FastAPI).

    rugguard serve                       # http://127.0.0.1:8000  (UI at /, API docs at /docs)

Security posture (see README "Running the service"):
  * Binds to 127.0.0.1 by default. Publish it through the Caddy proxy in docker-compose, not directly.
  * Every token ID is validated against NEAR account-ID rules before use (422 otherwise).
  * Work that spends external API quota runs through a bounded queue (jobs.py), never inline.
  * Per-client request rate limit. Client = socket address; X-Forwarded-For is only trusted when
    RUGGUARD_TRUST_PROXY=1 (set by docker-compose, where only the proxy can reach the API).
  * Writes (reviews, forget) and the activity log need RUGGUARD_ADMIN_TOKEN when it is set.
  * Strict security headers; the UI uses no inline script and never renders on-chain markup.
  * The service holds no NEAR keys.
"""

from __future__ import annotations

import hmac
import json
import os
import threading
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Callable

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .. import __version__, logs
from ..analysis import upgrade_surface
from ..analysis.outcome import LABELS, OutcomeRules, cohen_kappa
from ..collect import CollectOptions, Collector
from ..config import Settings
from ..http import Transport
from ..postmortem import discover_tokens, not_a_token, run_outcome
from ..scoring import evaluate, run_score
from ..analysis.score import ScoreRules
from ..storage.db import Database
from ..validate import InvalidAccountId, account_id
from .jobs import Job, JobQueue, QueueFull

STATIC = Path(__file__).with_name("static")

SECURITY_HEADERS = {
    # No inline script or style, no third-party origins, no framing, no images from anywhere but us.
    "Content-Security-Policy": ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; "
                                "connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; "
                                "frame-ancestors 'none'"),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Cross-Origin-Opener-Policy": "same-origin",
}


# FastAPI's interactive docs load Swagger UI from jsDelivr with an inline bootstrap script.
# Only these two pages get this looser policy; the UI and API keep the strict one.
DOCS_CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "style-src 'self' https://cdn.jsdelivr.net; img-src 'self' data: https://fastapi.tiangolo.com; "
            "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
DOCS_PATHS = ("/docs", "/redoc")


class ReviewIn(BaseModel):
    label: str = Field(..., description="one of: " + ", ".join(LABELS))
    reason: str = Field(..., min_length=3, max_length=500)
    reviewer: str | None = Field(None, max_length=64)


class RateLimiter:
    """Sliding one-minute window per client."""

    def __init__(self, per_minute: int):
        self.per_minute = per_minute
        self.hits: dict[str, deque] = defaultdict(deque)
        self.lock = threading.Lock()

    def allow(self, client: str) -> bool:
        now = time.monotonic()
        with self.lock:
            q = self.hits[client]
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= self.per_minute:
                return False
            q.append(now)
            return True


def create_app(settings: Settings | None = None, transport: Transport | None = None, *,
               admin_token: str | None = None, trust_proxy: bool | None = None,
               rate_per_minute: int | None = None, start_worker: bool = True,
               sleep: Callable[[float], None] | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    admin_token = admin_token if admin_token is not None else (os.getenv("RUGGUARD_ADMIN_TOKEN") or None)
    trust_proxy = trust_proxy if trust_proxy is not None else os.getenv("RUGGUARD_TRUST_PROXY") == "1"
    limiter = RateLimiter(rate_per_minute or int(os.getenv("RUGGUARD_RATE_PER_MIN", "120")))
    logs.setup()
    Database(settings.db_path).init()

    def open_db() -> Database:
        return Database(settings.db_path)

    def collector(db: Database) -> Collector:
        return Collector(settings, db, transport=transport, sleep=sleep)

    # ------------------------------------------------------------ job runner
    def run_job(job: Job) -> Any:
        db = open_db()
        try:
            c = collector(db)
            if job.kind == "discover":
                return {"tokens": discover_tokens(c, int(job.target))}
            problem = not_a_token(c, job.target)
            if problem:
                return {"rejected": problem}
            if job.kind == "collect":
                r = c.collect(job.target, CollectOptions())
                return {"snapshot_id": r.snapshot_id, "surface": r.surface, "concentration": r.concentration,
                        "errors": r.errors, "counts": r.counts}
            if job.kind == "outcome":
                result, errors = run_outcome(c, job.target, OutcomeRules())
                return {**result.as_dict(), "errors": errors}
            if job.kind == "score":
                result, errors = run_score(c, job.target)
                return {**result, "errors": errors}
            raise ValueError(f"unknown job kind {job.kind}")
        finally:
            db.close()

    jobs = JobQueue(run_job)

    app = FastAPI(title="Mvikeli", version=__version__,
                  description="Rug-pull risk signals and post-mortems for NEAR NEP-141 tokens. "
                              "Powered by Nearblocks.io APIs.")
    app.state.jobs = jobs
    if start_worker:
        jobs.start()

    # ------------------------------------------------------------ middleware
    def client_of(request: Request) -> str:
        if trust_proxy:
            fwd = request.headers.get("x-forwarded-for", "")
            if fwd:
                return fwd.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    @app.middleware("http")
    async def guard(request: Request, call_next):
        started = time.monotonic()
        client = client_of(request)
        if request.url.path.startswith("/api/") and not limiter.allow(client):
            response = JSONResponse({"detail": "Too many requests; the limit is per minute."}, status_code=429)
        else:
            response = await call_next(request)
        for k, v in SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        if request.url.path in DOCS_PATHS:
            response.headers["Content-Security-Policy"] = DOCS_CSP
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
            logs.event("http", method=request.method, path=request.url.path, status=response.status_code,
                       ms=round((time.monotonic() - started) * 1000))
        return response

    def require_admin(request: Request) -> None:
        if not admin_token:
            return
        given = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        if not hmac.compare_digest(given.encode(), admin_token.encode()):
            raise HTTPException(401, "This action needs the admin key (RUGGUARD_ADMIN_TOKEN).")

    def valid(token: str) -> str:
        try:
            return account_id(token)
        except InvalidAccountId as exc:
            raise HTTPException(422, str(exc))

    def submit(kind: str, target: str, request: Request) -> JSONResponse:
        try:
            job, created = jobs.submit(kind, target, client_of(request))
        except QueueFull as exc:
            raise HTTPException(429, str(exc))
        return JSONResponse({"job": job.public(), "created": created}, status_code=202 if created else 200)

    # ------------------------------------------------------------ endpoints
    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok", "version": __version__, "network": settings.network,
                "offline": settings.offline, "jobs_waiting": jobs.waiting(),
                "writes_need_key": bool(admin_token)}

    @app.get("/api/rules")
    def rules() -> dict:
        return {"score": ScoreRules().as_dict(), "outcome": OutcomeRules().as_dict(),
                "upgrade_surface": upgrade_surface.WEIGHTS_VERSION}

    @app.get("/api/tokens/{token}")
    def token_view(token: str) -> dict:
        token = valid(token)
        db = open_db()
        try:
            tok = db.query("SELECT * FROM tokens WHERE token_id = ?", (token,))
            snap = db.latest_snapshot(token)
            scores = db.latest_scores(token)
            outcome = db.query("SELECT * FROM outcomes WHERE token_id = ? ORDER BY outcome_id DESC LIMIT 1",
                               (token,))
            review = db.query("SELECT * FROM outcome_reviews WHERE token_id = ? ORDER BY review_id DESC LIMIT 1",
                              (token,))
            hist = db.history_run(token)
            return {
                "token_id": token,
                "metadata": {k: tok[0][k] for k in ("name", "symbol", "decimals", "total_supply")} if tok else None,
                "snapshot": {k: snap[k] for k in ("snapshot_id", "taken_at", "block_height")} | {
                    "errors": json.loads(snap["errors_json"])} if snap else None,
                "scores": [{"kind": s["kind"], "band": s["band"], "score": s["score"],
                            "reasons": json.loads(s["reasons_json"]), "facts": json.loads(s["facts_json"]),
                            "version": s["weights_version"], "taken_at": s["taken_at"]} for s in scores],
                "outcome": {"label": outcome[0]["label"], "rules_version": outcome[0]["rules_version"],
                            "evidence": json.loads(outcome[0]["evidence_json"]),
                            "facts": json.loads(outcome[0]["facts_json"]),
                            "computed_at": outcome[0]["computed_at"]} if outcome else None,
                "review": {k: review[0][k] for k in ("label", "reason", "reviewer", "created_at")} if review else None,
                "history": {k: hist[k] for k in ("rows", "complete", "pages", "fetched_at")} if hist else None,
            }
        finally:
            db.close()

    @app.get("/api/tokens/{token}/surface")
    def surface(token: str) -> dict:
        """Live upgrade-surface check: a few cached RPC calls, no NearBlocks quota."""
        token = valid(token)
        db = open_db()
        try:
            c = collector(db)
            errors: list = []
            account = c._try(errors, "view_account", lambda: c.rpc.view_account(token))
            keys = c._try(errors, "view_access_key_list", lambda: c.rpc.view_access_key_list(token))
            code = c.code_analysis(token, account, errors) if account else None
            return {"token_id": token, **upgrade_surface.assess(token, account, keys, code).as_dict(),
                    "errors": errors}
        finally:
            db.close()

    @app.post("/api/tokens/{token}/collect", status_code=202)
    def collect(token: str, request: Request):
        return submit("collect", valid(token), request)

    @app.post("/api/tokens/{token}/outcome", status_code=202)
    def outcome(token: str, request: Request):
        return submit("outcome", valid(token), request)

    @app.post("/api/tokens/{token}/score", status_code=202)
    def score_token(token: str, request: Request):
        return submit("score", valid(token), request)

    @app.get("/api/evaluation")
    def evaluation() -> dict:
        db = open_db()
        try:
            return evaluate(db)
        finally:
            db.close()

    @app.get("/api/results")
    def results() -> dict:
        """The evaluation per token set (seed files next to the service) and the testnet guard run."""
        from ..demo import GUARD_TXS
        sets = []
        db = open_db()
        try:
            for name, path, title in (("holdout", "seeds/holdout.txt", "Holdout: launches never used for tuning"),
                                      ("dev", "seeds/memecoins.txt", "Dev set: the rules were tuned here")):
                if not Path(path).exists():
                    continue
                only = {line.split()[0] for line in Path(path).read_text().splitlines()
                        if line.strip() and not line.strip().startswith("#")}
                rows = []
                for tag, use_human in (("Rule labels", False), ("After human review", True)):
                    ev = evaluate(db, only, use_human=use_human)
                    if ev["auc"] is None:
                        continue
                    if use_human and not any(r["label_source"] == "human" for r in ev["rows"]):
                        continue
                    rows.append({"labels": tag, "judged": ev["judged"], "rugged": ev["rugged"],
                                 "caught": ev["caught"], "false_alarms": ev["false_alarms"],
                                 "others": ev["judged"] - ev["rugged"], "auc": ev["auc"],
                                 "threshold": ev["threshold"]})
                if rows:
                    sets.append({"name": name, "title": title, "rows": rows})
        finally:
            db.close()
        guard = [{"what": what, "tx": tx, "url": f"https://testnet.nearblocks.io/txns/{tx}"} for what, tx in GUARD_TXS]
        return {"sets": sets, "guard": guard}

    @app.post("/api/discover", status_code=202)
    def discover(request: Request, recent: int = Query(300, ge=10, le=2000)):
        return submit("discover", str(recent), request)

    @app.get("/api/jobs")
    def list_jobs() -> dict:
        return {"jobs": [j.public() for j in jobs.recent()], "waiting": jobs.waiting()}

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: int) -> dict:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(404, f"No job {job_id}; finished jobs are kept for a while, then dropped.")
        return job.public()

    @app.get("/api/outcomes")
    def outcomes() -> dict:
        db = open_db()
        try:
            rows = [dict(r) for r in db.outcome_table()]
        finally:
            db.close()
        pairs = [(r["auto_label"], r["human_label"]) for r in rows if r["human_label"]]
        counts: dict[str, int] = {}
        for r in rows:
            counts[r["auto_label"]] = counts.get(r["auto_label"], 0) + 1
        return {"rows": rows, "counts": counts, "reviewed": len(pairs),
                "agreement": (sum(a == b for a, b in pairs) / len(pairs)) if pairs else None,
                "kappa": cohen_kappa(pairs)}

    @app.post("/api/tokens/{token}/review", dependencies=[Depends(require_admin)])
    def review(token: str, body: ReviewIn) -> dict:
        token = valid(token)
        if body.label not in LABELS:
            raise HTTPException(422, f"Label must be one of: {', '.join(LABELS)}")
        db = open_db()
        try:
            if not db.query("SELECT 1 FROM outcomes WHERE token_id = ?", (token,)):
                raise HTTPException(409, f"Run the post-mortem for {token} before reviewing it.")
            db.put_review(token, body.label, body.reason.strip(), body.reviewer)
        finally:
            db.close()
        logs.event("review", token=token, label=body.label)
        return {"recorded": True, "token_id": token, "label": body.label}

    @app.get("/api/logs", dependencies=[Depends(require_admin)])
    def activity(n: int = Query(100, ge=1, le=500)) -> dict:
        return {"entries": logs.recent(n)}

    # ------------------------------------------------------------ UI
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html")

    return app

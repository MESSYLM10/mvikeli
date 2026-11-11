"""A small, bounded job queue with one worker.

Anything that spends NearBlocks credits (collect, outcome, discover) goes through here:
  * one worker, so jobs run one at a time: free-tier pacing holds and SQLite has one writer;
  * a bounded queue (default 25) and a per-client cap on waiting jobs (default 3);
  * a request for a job that is already queued returns that job instead of a duplicate.
Jobs live in memory; their *results* (scores, outcomes) are stored in the database as usual.
"""

from __future__ import annotations

import itertools
import queue
import threading
import time
import traceback
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from .. import logs


class QueueFull(Exception):
    pass


@dataclass
class Job:
    id: int
    kind: str
    target: str
    client: str
    status: str = "queued"          # queued | running | done | failed
    created: float = field(default_factory=time.time)
    started: float | None = None
    finished: float | None = None
    result: Any = None
    error: str | None = None

    def public(self) -> dict:
        d = asdict(self)
        d.pop("client")
        return d


class JobQueue:
    def __init__(self, runner: Callable[[Job], Any], max_queued: int = 25, per_client: int = 3,
                 keep: int = 200):
        self.runner = runner
        self.max_queued = max_queued
        self.per_client = per_client
        self.keep = keep
        self.jobs: dict[int, Job] = {}
        self._q: queue.Queue[int] = queue.Queue()
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()

    # -------------------------------------------------------------- submit
    def submit(self, kind: str, target: str, client: str) -> tuple[Job, bool]:
        """Returns (job, created). Raises QueueFull with a reason the client can act on."""
        with self._lock:
            for j in self.jobs.values():
                if j.kind == kind and j.target == target and j.status in ("queued", "running"):
                    return j, False
            waiting = [j for j in self.jobs.values() if j.status == "queued"]
            if len(waiting) >= self.max_queued:
                raise QueueFull(f"The queue is full ({self.max_queued} jobs waiting). Try again in a few minutes.")
            mine = [j for j in waiting if j.client == client]
            if len(mine) >= self.per_client:
                raise QueueFull(f"You already have {len(mine)} jobs waiting; the limit is {self.per_client}.")
            job = Job(next(self._ids), kind, target, client)
            self.jobs[job.id] = job
            self._trim()
        self._q.put(job.id)
        logs.event("job.queued", job=job.id, kind=kind, target=target)
        return job, True

    def _trim(self) -> None:
        done = sorted((j for j in self.jobs.values() if j.status in ("done", "failed")), key=lambda j: j.id)
        for j in done[: max(0, len(self.jobs) - self.keep)]:
            self.jobs.pop(j.id, None)

    def get(self, job_id: int) -> Job | None:
        return self.jobs.get(job_id)

    def recent(self, n: int = 30) -> list[Job]:
        return sorted(self.jobs.values(), key=lambda j: j.id, reverse=True)[:n]

    def waiting(self) -> int:
        return sum(j.status == "queued" for j in self.jobs.values())

    # -------------------------------------------------------------- worker
    def start(self) -> None:
        if self._worker is None:
            self._worker = threading.Thread(target=self._loop, name="rugguard-worker", daemon=True)
            self._worker.start()

    def stop(self) -> None:
        self._stop.set()
        self._q.put(-1)

    def run_one(self, timeout: float | None = None) -> bool:
        """Process one job (used by the worker loop and by tests). Returns False if none."""
        try:
            job_id = self._q.get(timeout=timeout)
        except queue.Empty:
            return False
        job = self.jobs.get(job_id)
        if job is None:
            return job_id != -1
        job.status, job.started = "running", time.time()
        logs.event("job.started", job=job.id, kind=job.kind, target=job.target)
        try:
            job.result = self.runner(job)
            job.status = "done"
        except Exception as exc:  # a job failure must never kill the worker
            job.status, job.error = "failed", f"{type(exc).__name__}: {exc}"
            logs.event("job.failed", 40, job=job.id, error=job.error,
                       trace=traceback.format_exc(limit=3))
        job.finished = time.time()
        logs.event("job.finished", job=job.id, status=job.status,
                   seconds=round(job.finished - job.started, 1))
        return True

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.run_one(timeout=1.0)

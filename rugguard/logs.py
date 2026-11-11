"""Structured operational logs: one JSON object per line.

Decision logs (scores, outcomes, evidence) live in the database. These logs answer the
operational questions: which source was called, how long it took, cache hit or not, errors.
Headers are never logged, so API keys cannot leak; query strings are dropped from URLs.

    RUGGUARD_LOG_LEVEL=INFO       DEBUG also logs cache hits
    RUGGUARD_LOG_FILE=data/rugguard.log
The last 500 records are also kept in memory for the UI's activity panel.
"""

from __future__ import annotations

import collections
import json
import logging
import os
import threading
from datetime import datetime, timezone

RECENT: collections.deque = collections.deque(maxlen=500)
_lock = threading.Lock()
_configured = False


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "event": record.getMessage(),
            **getattr(record, "fields", {}),
        }
        return json.dumps(entry, default=str)


class RingHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            RECENT.append(json.loads(self.format(record)))
        except Exception:  # never let logging break the app
            pass


def setup(stderr: bool = False) -> None:
    """Idempotent. CLI keeps stderr quiet unless RUGGUARD_LOG_FILE or stderr=True."""
    global _configured
    with _lock:
        if _configured:
            return
        log = logging.getLogger("rugguard")
        log.setLevel(os.getenv("RUGGUARD_LOG_LEVEL", "INFO").upper())
        log.propagate = False
        fmt = JsonFormatter()
        ring = RingHandler()
        ring.setFormatter(fmt)
        log.addHandler(ring)
        path = os.getenv("RUGGUARD_LOG_FILE")
        if path:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            fh = logging.FileHandler(path)
            fh.setFormatter(fmt)
            log.addHandler(fh)
        if stderr:
            sh = logging.StreamHandler()
            sh.setFormatter(fmt)
            log.addHandler(sh)
        _configured = True


def event(name: str, level: int = logging.INFO, **fields) -> None:
    logging.getLogger("rugguard").log(level, name, extra={"fields": fields})


def recent(n: int = 100) -> list[dict]:
    return list(RECENT)[-n:]


# ---------------------------------------------------------------- progress
# Human progress lines for long CLI runs. Written to stderr, so `| tee file` keeps the
# results file clean while the terminal still shows that work is happening.
PROGRESS = False


def progress(message: str) -> None:
    if PROGRESS:
        import sys
        print(message, file=sys.stderr, flush=True)

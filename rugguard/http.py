"""JSON HTTP client with retries, polite backoff and a SQLite-backed response cache.

Every response is cached in `raw_responses`, keyed by a hash of the request, so that:
  * NearBlocks credits are not spent twice on the same page;
  * a demo can be replayed offline (RUGGUARD_OFFLINE=1) if an API is down.
"""

from __future__ import annotations

import hashlib
import logging
import json
import time
from typing import Any, Callable

import requests

from . import logs

from .config import Settings
from .storage.db import Database


class FetchError(RuntimeError):
    def __init__(self, source: str, message: str, status: int | None = None):
        super().__init__(f"[{source}] {message}")
        self.source = source
        self.status = status


# A transport takes (method, url, headers, json_body, timeout) and returns (status, parsed_json).
Transport = Callable[[str, str, dict, Any, float], tuple[int, Any]]


def requests_transport(method: str, url: str, headers: dict, body: Any, timeout: float) -> tuple[int, Any]:
    resp = requests.request(method, url, headers=headers, json=body, timeout=timeout)
    try:
        data = resp.json()
    except ValueError:
        data = {"_non_json": resp.text[:2000]}
    return resp.status_code, data


class JsonClient:
    RETRY_STATUSES = {429, 500, 502, 503, 504}

    def __init__(self, settings: Settings, db: Database, transport: Transport | None = None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic):
        self.settings = settings
        self.db = db
        self.transport = transport or requests_transport
        self.sleep = sleep
        self.clock = clock
        self._last_call: dict[str, float] = {}
        # Sources that kept answering 429: paused until this monotonic time (circuit breaker).
        self.paused_until: dict[str, float] = {}
        # Endpoints the provider has switched off (HTTP 403 "currently unavailable"):
        # remembered for the rest of the run so we stop spending rate limit on them.
        self.disabled: dict[str, str] = {}

    def _throttle(self, source: str) -> None:
        """Keep at least `min_interval` seconds between live calls to one source."""
        gap = self.settings.min_interval_s.get(source, 0.0)
        last = self._last_call.get(source)
        if gap and last is not None:
            wait = gap - (self.clock() - last)
            if wait > 0:
                self.sleep(wait)
        self._last_call[source] = self.clock()

    @staticmethod
    def _endpoint(url: str) -> str:
        return url.split("?")[0]

    def rate_limited(self, source: str) -> bool:
        until = self.paused_until.get(source)
        return until is not None and self.clock() < until

    @staticmethod
    def _describe(url: str, body: Any) -> str:
        """Log-safe description: URL path without query string, or the RPC request type + target."""
        if isinstance(body, dict) and isinstance(body.get("params"), dict):
            p = body["params"]
            target = p.get("account_id") or p.get("code_hash") or ""
            extra = f".{p['method_name']}" if p.get("method_name") else ""
            return f"{p.get('request_type')} {target}{extra}"
        return url.split("?")[0]

    @staticmethod
    def cache_key(source: str, request: dict) -> str:
        canonical = json.dumps({"source": source, **request}, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()

    def fetch(self, source: str, method: str, url: str, *, body: Any = None,
              headers: dict | None = None, ttl_s: int | None = None, use_cache: bool = True) -> Any:
        request = {"method": method, "url": url, "body": body}
        key = self.cache_key(source, request)
        ttl = self.settings.cache_ttl_s if ttl_s is None else ttl_s

        what = self._describe(url, body)
        if use_cache:
            cached = self.db.cache_get(key, None if self.settings.offline else ttl)
            if cached is not None:
                logs.event("fetch", logging.DEBUG, source=source, what=what, cache="hit")
                return cached["body"]
        if self.settings.offline:
            raise FetchError(source, f"offline mode and no cached response for {method} {url}")

        if self.rate_limited(source):
            mins = (self.paused_until[source] - self.clock()) / 60
            raise FetchError(source, f"rate limit: {source} refused repeatedly; paused for {mins:.0f} more "
                                     "minute(s). Cached data is still used.", 429)
        endpoint = self._endpoint(url)
        if endpoint in self.disabled:
            raise FetchError(source, f"skipped, endpoint unavailable earlier this run: {endpoint}", 403)

        hdrs = {"User-Agent": self.settings.user_agent, "Accept": "application/json", **(headers or {})}
        last_err: str = ""
        for attempt in range(self.settings.max_retries + 1):
            self._throttle(source)
            started = time.monotonic()
            try:
                status, data = self.transport(method, url, hdrs, body, self.settings.timeout_s)
            except requests.RequestException as exc:  # network-level failure
                status, data, last_err = None, None, str(exc)
            logs.event("fetch", logging.INFO if status is not None and status < 400 else logging.WARNING,
                       source=source, what=what, status=status, attempt=attempt + 1,
                       ms=round((time.monotonic() - started) * 1000), cache="miss")
            if status is not None and status < 400:
                self.db.cache_put(key, source, request, status, data)
                return data
            if status == 403 and "unavailable" in str(data).lower():
                self.disabled[endpoint] = str(data)[:200]
            if status is not None and status not in self.RETRY_STATUSES:
                raise FetchError(source, f"HTTP {status} for {url}: {str(data)[:300]}", status)
            last_err = f"HTTP {status}" if status is not None else last_err
            if attempt < self.settings.max_retries:
                # Rate limits (429) need a real pause; transient 5xx/network errors a short one.
                delay = min(15 * 2 ** attempt, 60) if status == 429 else min(2 ** attempt, 8)
                self.sleep(delay)
        if "429" in last_err:
            self.paused_until[source] = self.clock() + self.settings.rate_limit_pause_s
            logs.event("rate_limited", logging.WARNING, source=source,
                       pause_s=self.settings.rate_limit_pause_s)
            raise FetchError(source, f"rate limit: gave up after retries for {self._endpoint(url)}", 429)
        raise FetchError(source, f"gave up after retries for {url}: {last_err}")

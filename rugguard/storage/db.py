"""SQLite access layer. One connection per process; all writes go through here."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def dumps(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL") if str(self.path) != ":memory:" else None

    def init(self) -> None:
        self.conn.executescript(SCHEMA_PATH.read_text())
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ---------------------------------------------------------------- cache
    def cache_get(self, cache_key: str, max_age_s: int | None) -> dict | None:
        row = self.conn.execute(
            "SELECT status, body, fetched_at FROM raw_responses WHERE cache_key = ?", (cache_key,)
        ).fetchone()
        if row is None:
            return None
        if max_age_s is not None:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(row["fetched_at"])
            if age.total_seconds() > max_age_s:
                return None
        return {"status": row["status"], "body": json.loads(row["body"])}

    def cache_put(self, cache_key: str, source: str, request: dict, status: int, body: Any) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO raw_responses VALUES (?, ?, ?, ?, ?, ?)",
            (cache_key, source, dumps(request), status, dumps(body), utcnow()),
        )
        self.conn.commit()

    # --------------------------------------------------------------- tokens
    def upsert_token(self, token_id: str, meta: dict | None, total_supply: str | None) -> None:
        now = utcnow()
        meta = meta or {}
        self.conn.execute(
            """INSERT INTO tokens (token_id, name, symbol, decimals, total_supply, spec, first_seen_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(token_id) DO UPDATE SET
                 name = COALESCE(excluded.name, name),
                 symbol = COALESCE(excluded.symbol, symbol),
                 decimals = COALESCE(excluded.decimals, decimals),
                 total_supply = COALESCE(excluded.total_supply, total_supply),
                 spec = COALESCE(excluded.spec, spec),
                 updated_at = excluded.updated_at""",
            (token_id, meta.get("name"), meta.get("symbol"), meta.get("decimals"),
             total_supply, meta.get("spec"), now, now),
        )
        self.conn.commit()

    def new_snapshot(self, token_id: str, network: str, block_height: int | None) -> int:
        cur = self.conn.execute(
            "INSERT INTO snapshots (token_id, network, block_height, taken_at) VALUES (?, ?, ?, ?)",
            (token_id, network, block_height, utcnow()),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def set_snapshot_meta(self, snapshot_id: int, block_height: int | None, errors: list) -> None:
        self.conn.execute(
            "UPDATE snapshots SET block_height = COALESCE(?, block_height), errors_json = ? WHERE snapshot_id = ?",
            (block_height, dumps(errors), snapshot_id),
        )
        self.conn.commit()

    # -------------------------------------------------------- account/keys
    def put_account_state(self, snapshot_id: int, account_id: str, view: dict) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO account_state VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (snapshot_id, account_id, view.get("amount"), view.get("locked"), view.get("code_hash"),
             view.get("storage_usage"), view.get("global_contract_hash"),
             view.get("global_contract_account_id")),
        )
        self.conn.commit()

    def put_access_keys(self, snapshot_id: int, account_id: str, keys: Iterable[dict]) -> None:
        rows = [
            (snapshot_id, account_id, k["public_key"], k["permission"], int(k["is_full"]),
             k.get("receiver_id"), dumps(k.get("method_names") or []), k.get("allowance"), k.get("nonce"))
            for k in keys
        ]
        self.conn.executemany("INSERT OR REPLACE INTO access_keys VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        self.conn.commit()

    def put_contract_code(self, code_hash: str, analysis: dict) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO contract_code VALUES (?, ?, ?, ?, ?, ?, ?)",
            (code_hash, analysis["size_bytes"], dumps(analysis["imports"]), dumps(analysis["exports"]),
             dumps(analysis["flags"]), analysis.get("parse_error"), utcnow()),
        )
        self.conn.commit()

    def get_contract_code(self, code_hash: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM contract_code WHERE code_hash = ?", (code_hash,)).fetchone()
        if row is None:
            return None
        return {
            "size_bytes": row["size_bytes"],
            "imports": json.loads(row["imports_json"]),
            "exports": json.loads(row["exports_json"]),
            "flags": json.loads(row["flags_json"]),
            "parse_error": row["parse_error"],
        }

    # ------------------------------------------------------ holders/stats
    def put_holders(self, snapshot_id: int, source: str, holders: list[dict]) -> None:
        rows = [(snapshot_id, h["account_id"], str(h["balance"]), i + 1, source) for i, h in enumerate(holders)]
        self.conn.executemany("INSERT OR REPLACE INTO holders VALUES (?, ?, ?, ?, ?)", rows)
        self.conn.commit()

    def put_token_stats(self, snapshot_id: int, holder_count: int | None,
                        transfer_count: int | None, body: Any) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO token_stats VALUES (?, ?, ?, ?)",
            (snapshot_id, holder_count, transfer_count, dumps(body)),
        )
        self.conn.commit()

    def put_transfers(self, token_id: str, rows: list[dict]) -> int:
        data = [
            (token_id, r["event_key"], r.get("block_timestamp"), r.get("from_account"), r.get("to_account"),
             r.get("amount_raw"), r.get("cause"), r.get("tx_hash"), dumps(r["body"]))
            for r in rows
        ]
        cur = self.conn.executemany("INSERT OR IGNORE INTO ft_transfers VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", data)
        self.conn.commit()
        return cur.rowcount

    def put_deployments(self, token_id: str, rows: list[dict]) -> None:
        data = [(token_id, r["event_key"], r.get("tx_hash"), r.get("block_timestamp"), dumps(r["body"]))
                for r in rows]
        self.conn.executemany("INSERT OR IGNORE INTO deployments VALUES (?, ?, ?, ?, ?)", data)
        self.conn.commit()

    def put_pools(self, snapshot_id: int, dex: str, pools: list[dict]) -> None:
        data = [
            (snapshot_id, dex, p["pool_id"], p.get("pool_kind"), dumps(p.get("token_account_ids", [])),
             dumps(p.get("amounts", [])), p.get("shares_total_supply"), p.get("total_fee"))
            for p in pools
        ]
        self.conn.executemany("INSERT OR REPLACE INTO pools VALUES (?, ?, ?, ?, ?, ?, ?, ?)", data)
        self.conn.commit()

    # --------------------------------------------------------------- scores
    def put_score(self, snapshot_id: int, kind: str, score: float | None, band: str,
                  reasons: list, facts: dict, weights_version: str) -> None:
        self.conn.execute(
            """INSERT INTO risk_scores (snapshot_id, kind, score, band, reasons_json, facts_json,
                                        weights_version, computed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (snapshot_id, kind, score, band, dumps(reasons), dumps(facts), weights_version, utcnow()),
        )
        self.conn.commit()

    # ---------------------------------------------------------------- reads
    def latest_scores(self, token_id: str) -> list[sqlite3.Row]:
        """The most recent score of each kind for a token, across snapshots, with its snapshot time."""
        return self.conn.execute(
            """SELECT rs.*, sn.taken_at FROM risk_scores rs JOIN snapshots sn ON sn.snapshot_id = rs.snapshot_id
               WHERE sn.token_id = ? AND rs.score_id IN (
                   SELECT MAX(rs2.score_id) FROM risk_scores rs2
                   JOIN snapshots sn2 ON sn2.snapshot_id = rs2.snapshot_id
                   WHERE sn2.token_id = ? GROUP BY rs2.kind)
               ORDER BY rs.kind""", (token_id, token_id)).fetchall()

    def latest_snapshot(self, token_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM snapshots WHERE token_id = ? ORDER BY snapshot_id DESC LIMIT 1", (token_id,)
        ).fetchone()

    def scores_for(self, snapshot_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM risk_scores WHERE snapshot_id = ? ORDER BY score_id", (snapshot_id,)
        ).fetchall()

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()


def _attach_outcome_methods() -> None:
    """Post-mortem storage helpers (kept together for readability)."""

    def put_history_run(self, token_id: str, pages: int, page_size: int, rows: int, complete: bool,
                        oldest_ts: str | None, newest_ts: str | None) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO history_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (token_id, pages, page_size, rows, int(complete), oldest_ts, newest_ts, utcnow()),
        )
        self.conn.commit()

    def history_run(self, token_id: str):
        return self.conn.execute("SELECT * FROM history_runs WHERE token_id = ?", (token_id,)).fetchone()

    def transfers_for(self, token_id: str):
        return self.conn.execute("SELECT * FROM ft_transfers WHERE token_id = ?", (token_id,)).fetchall()

    def deployments_for(self, token_id: str):
        return self.conn.execute("SELECT * FROM deployments WHERE token_id = ?", (token_id,)).fetchall()

    def put_outcome(self, token_id: str, label: str, rules_version: str, evidence: list, facts: dict) -> None:
        self.conn.execute(
            "INSERT INTO outcomes (token_id, label, rules_version, evidence_json, facts_json, computed_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (token_id, label, rules_version, dumps(evidence), dumps(facts), utcnow()),
        )
        self.conn.commit()

    def put_review(self, token_id: str, label: str, reason: str, reviewer: str | None) -> None:
        self.conn.execute(
            "INSERT INTO outcome_reviews (token_id, label, reason, reviewer, created_at) VALUES (?, ?, ?, ?, ?)",
            (token_id, label, reason, reviewer, utcnow()),
        )
        self.conn.commit()

    def outcome_table(self):
        """Latest automatic label and latest human label per token."""
        return self.conn.execute(
            """SELECT o.token_id, o.label AS auto_label, o.rules_version, o.computed_at,
                      r.label AS human_label, r.reason
               FROM outcomes o
               JOIN (SELECT token_id, MAX(outcome_id) AS m FROM outcomes GROUP BY token_id) lo
                 ON lo.m = o.outcome_id
               LEFT JOIN (SELECT token_id, label, reason FROM outcome_reviews r1
                          WHERE review_id = (SELECT MAX(review_id) FROM outcome_reviews r2
                                             WHERE r2.token_id = r1.token_id)) r
                 ON r.token_id = o.token_id
               ORDER BY o.token_id""").fetchall()

    for fn in (put_history_run, history_run, transfers_for, deployments_for, put_outcome, put_review,
               outcome_table):
        setattr(Database, fn.__name__, fn)


_attach_outcome_methods()

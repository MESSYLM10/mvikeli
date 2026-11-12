-- Mvikeli storage schema (SQLite).
-- Token amounts are NEP-141 u128 values: always stored as TEXT, never INTEGER.

PRAGMA foreign_keys = ON;

-- Every external response, for rate-limit friendliness and reproducible demos.
CREATE TABLE IF NOT EXISTS raw_responses (
    cache_key   TEXT PRIMARY KEY,          -- sha256(source + canonical request)
    source      TEXT NOT NULL,             -- rpc | fastnear | nearblocks
    request     TEXT NOT NULL,             -- canonical JSON of the request
    status      INTEGER NOT NULL,
    body        TEXT NOT NULL,             -- response JSON
    fetched_at  TEXT NOT NULL              -- ISO-8601 UTC
);

CREATE TABLE IF NOT EXISTS tokens (
    token_id      TEXT PRIMARY KEY,        -- NEP-141 contract account, e.g. wrap.near
    name          TEXT,
    symbol        TEXT,
    decimals      INTEGER,
    total_supply  TEXT,                    -- raw u128
    spec          TEXT,
    first_seen_at TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    token_id      TEXT NOT NULL REFERENCES tokens(token_id),
    network       TEXT NOT NULL,
    block_height  INTEGER,
    taken_at      TEXT NOT NULL,
    errors_json   TEXT NOT NULL DEFAULT '[]'   -- sources that failed during this run
);
CREATE INDEX IF NOT EXISTS ix_snapshots_token ON snapshots(token_id, taken_at);

CREATE TABLE IF NOT EXISTS account_state (
    snapshot_id                INTEGER PRIMARY KEY REFERENCES snapshots(snapshot_id),
    account_id                 TEXT NOT NULL,
    amount                     TEXT,       -- yoctoNEAR
    locked                     TEXT,
    code_hash                  TEXT,
    storage_usage              INTEGER,
    global_contract_hash       TEXT,       -- code pinned by hash (immutable)
    global_contract_account_id TEXT        -- code follows another account (mutable)
);

CREATE TABLE IF NOT EXISTS access_keys (
    snapshot_id   INTEGER NOT NULL REFERENCES snapshots(snapshot_id),
    account_id    TEXT NOT NULL,
    public_key    TEXT NOT NULL,
    permission    TEXT NOT NULL,           -- FullAccess | FunctionCall | GasKeyFullAccess | GasKeyFunctionCall
    is_full       INTEGER NOT NULL,        -- 1 if the key can deploy code / manage keys
    receiver_id   TEXT,
    method_names  TEXT,                    -- JSON list ([] = all methods)
    allowance     TEXT,                    -- NULL = unlimited
    nonce         INTEGER,
    PRIMARY KEY (snapshot_id, public_key)
);

CREATE TABLE IF NOT EXISTS contract_code (
    code_hash     TEXT PRIMARY KEY,
    size_bytes    INTEGER NOT NULL,
    imports_json  TEXT NOT NULL,           -- ["env.promise_batch_action_deploy_contract", ...]
    exports_json  TEXT NOT NULL,           -- exported function names = contract methods
    flags_json    TEXT NOT NULL,           -- capability flags derived from imports/exports
    parse_error   TEXT,
    analysed_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS holders (
    snapshot_id   INTEGER NOT NULL REFERENCES snapshots(snapshot_id),
    account_id    TEXT NOT NULL,
    balance_raw   TEXT NOT NULL,
    rank          INTEGER NOT NULL,
    source        TEXT NOT NULL,           -- fastnear | nearblocks
    PRIMARY KEY (snapshot_id, account_id, source)
);

CREATE TABLE IF NOT EXISTS token_stats (
    snapshot_id   INTEGER PRIMARY KEY REFERENCES snapshots(snapshot_id),
    holder_count  INTEGER,
    transfer_count INTEGER,
    body_json     TEXT                     -- NearBlocks FT details as returned
);

CREATE TABLE IF NOT EXISTS ft_transfers (
    token_id        TEXT NOT NULL,
    event_key       TEXT NOT NULL,         -- receipt id + event index, or a hash of the row
    block_timestamp TEXT,                  -- nanoseconds, as string
    from_account    TEXT,
    to_account      TEXT,
    amount_raw      TEXT,
    cause           TEXT,
    tx_hash         TEXT,
    body_json       TEXT NOT NULL,
    PRIMARY KEY (token_id, event_key)
);
CREATE INDEX IF NOT EXISTS ix_transfers_time ON ft_transfers(token_id, block_timestamp);

CREATE TABLE IF NOT EXISTS deployments (
    token_id        TEXT NOT NULL,
    event_key       TEXT NOT NULL,
    tx_hash         TEXT,
    block_timestamp TEXT,
    body_json       TEXT NOT NULL,
    PRIMARY KEY (token_id, event_key)
);

CREATE TABLE IF NOT EXISTS pools (
    snapshot_id   INTEGER NOT NULL REFERENCES snapshots(snapshot_id),
    dex           TEXT NOT NULL,           -- contract, e.g. v2.ref-finance.near
    pool_id       INTEGER NOT NULL,
    pool_kind     TEXT,
    tokens_json   TEXT NOT NULL,
    amounts_json  TEXT NOT NULL,           -- raw reserves, same order as tokens
    shares_total  TEXT,
    total_fee     INTEGER,
    PRIMARY KEY (snapshot_id, dex, pool_id)
);

CREATE TABLE IF NOT EXISTS risk_scores (
    score_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id     INTEGER NOT NULL REFERENCES snapshots(snapshot_id),
    kind            TEXT NOT NULL,         -- upgrade_surface | concentration | total
    score           REAL,                  -- 0..1 sub-score or 0..100 total
    band            TEXT,                  -- low | medium | high | unknown
    reasons_json    TEXT NOT NULL,
    facts_json      TEXT NOT NULL,
    weights_version TEXT NOT NULL,
    computed_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_scores_snapshot ON risk_scores(snapshot_id, kind);

-- ---------------------------------------------------------------- post-mortem (outcomes)
-- How far back the stored transfer history reaches for each token.
CREATE TABLE IF NOT EXISTS history_runs (
    token_id     TEXT PRIMARY KEY,
    pages        INTEGER NOT NULL,
    page_size    INTEGER NOT NULL,
    rows         INTEGER NOT NULL,
    complete     INTEGER NOT NULL,        -- 1 = history reaches the first transfer
    oldest_ts    TEXT,
    newest_ts    TEXT,
    fetched_at   TEXT NOT NULL
);

-- Automatic outcome labels, one row per run (rules version recorded for audit).
CREATE TABLE IF NOT EXISTS outcomes (
    outcome_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    token_id       TEXT NOT NULL,
    label          TEXT NOT NULL,
    rules_version  TEXT NOT NULL,
    evidence_json  TEXT NOT NULL,
    facts_json     TEXT NOT NULL,
    computed_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_outcomes_token ON outcomes(token_id, outcome_id);

-- Human reviews. The latest row per token is the human label.
CREATE TABLE IF NOT EXISTS outcome_reviews (
    review_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    token_id    TEXT NOT NULL,
    label       TEXT NOT NULL,
    reason      TEXT NOT NULL,
    reviewer    TEXT,
    created_at  TEXT NOT NULL
);

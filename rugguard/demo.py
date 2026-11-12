"""Offline demo: a packed copy of a real Mvikeli database that runs with no network and no API keys.

`pack` copies the working database (scores, outcomes, evidence, transfers and, by default, the cached
API responses), drops nothing else, compacts it and gzips it into `demo/rugguard-demo.db.gz`.
`unpack` restores it to `data/demo.db`. With the response cache included, `RUGGUARD_OFFLINE=1` re-runs
`score` and `outcome` from the cache, so a reviewer can reproduce the stored results without the APIs.

Request headers (API keys) are never stored in the cache, so a pack carries no credentials.
"""

from __future__ import annotations

import gzip
import shutil
import sqlite3
import tempfile
from pathlib import Path

DEMO_PACK = Path("demo/rugguard-demo.db.gz")
DEMO_DB = Path("data/demo.db")

# The guard vault run on testnet (11 Nov 2026); see contracts/guard-vault/README.md.
GUARD_TXS = (
    ("Agent asks to buy a token scored 90: refused, HIGH_RISK", "iPXzqG4sbacHCnF1QkavWwLCkUVYbbq9HgEf9735D23"),
    ("Agent buys a token scored 20: approved, swapped on Rhea", "8Sr3Kk3gcW1Dbdo2yfNMBvURDLLf9267p7vKzYESMTgZ"),
    ("Agent asks for 2.5 wNEAR, limit 2: refused, OVER_SWAP_LIMIT", "GGhmZRpM38dRCjsUoivaQorTGwDYJz4CQgG9xwVSmAvi"),
)


def pack(src: Path, out: Path = DEMO_PACK, with_cache: bool = True) -> dict:
    if not Path(src).exists():
        raise FileNotFoundError(f"No database at {src}: run outcome and score first.")
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "demo.db"
        source = sqlite3.connect(str(src))
        dest = sqlite3.connect(str(copy))
        source.backup(dest)            # consistent copy even while WAL is in use
        source.close()
        if not with_cache:
            dest.execute("DELETE FROM raw_responses")
        dest.commit()
        dest.execute("PRAGMA journal_mode = DELETE")
        dest.execute("VACUUM")
        counts = {t: dest.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]          # names are constants
                  for t in ("tokens", "outcomes", "risk_scores", "raw_responses")}
        dest.close()
        raw = copy.stat().st_size
        with open(copy, "rb") as f_in, gzip.open(out, "wb", compresslevel=9) as f_out:
            shutil.copyfileobj(f_in, f_out)
    return {"path": str(out), "db_bytes": raw, "packed_bytes": out.stat().st_size, **counts}


def unpack(pack_path: Path = DEMO_PACK, dest: Path = DEMO_DB, reset: bool = False) -> bool:
    """Restore the demo database; returns True if it was (re)written."""
    if not Path(pack_path).exists():
        raise FileNotFoundError(f"No demo pack at {pack_path}.")
    if Path(dest).exists() and not reset:
        return False
    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        Path(f"{dest}{suffix}").unlink(missing_ok=True)
    with gzip.open(pack_path, "rb") as f_in, open(dest, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)
    return True

"""Runtime settings, read from environment variables (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv(path: Path = Path(".env")) -> None:
    """Minimal .env loader so judges need no extra package. Existing env wins."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _flag(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(int(default))).lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    network: str = "mainnet"
    rpc_url: str = "https://rpc.mainnet.near.org"
    fastnear_url: str = "https://api.fastnear.com"
    nearblocks_url: str = "https://api.nearblocks.io"
    nearblocks_api_key: str | None = None
    fastnear_transfers_url: str = "https://transfers.main.fastnear.com"
    fastnear_api_key: str | None = None
    # Where transfer history comes from: "fastnear" (account-centric, no key needed) or "nearblocks".
    history_source: str = "fastnear"
    rhea_contract: str = "v2.ref-finance.near"
    db_path: Path = Path("data/rugguard.db")
    timeout_s: float = 20.0
    max_retries: int = 3
    cache_ttl_s: int = 6 * 3600
    offline: bool = False
    user_agent: str = "rugguard/0.1 (+https://github.com/MESSYLM10)"
    # Minimum seconds between live calls per source. NearBlocks' free tier rate-limits hard,
    # so it is paced by default; an API key lets you lower this.
    min_interval_s: dict = field(default_factory=dict)
    # After a source keeps answering 429, stop calling it for this long (circuit breaker).
    rate_limit_pause_s: int = 15 * 60

    @property
    def nearblocks_page_size(self) -> int:
        """Rows per NearBlocks page. Each 25 rows cost one call on the free plan, so use 25 there."""
        return 100 if self.nearblocks_api_key else 25
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "Settings":
        _load_dotenv()
        network = os.getenv("RUGGUARD_NETWORK", "mainnet")
        testnet = network == "testnet"
        return cls(
            network=network,
            rpc_url=os.getenv(
                "RUGGUARD_RPC_URL",
                "https://rpc.testnet.near.org" if testnet else "https://rpc.mainnet.near.org",
            ),
            fastnear_url=os.getenv(
                "RUGGUARD_FASTNEAR_URL",
                "https://test.api.fastnear.com" if testnet else "https://api.fastnear.com",
            ),
            nearblocks_url=os.getenv(
                "RUGGUARD_NEARBLOCKS_URL",
                "https://api-testnet.nearblocks.io" if testnet else "https://api.nearblocks.io",
            ),
            nearblocks_api_key=os.getenv("NEARBLOCKS_API_KEY") or None,
            fastnear_transfers_url=os.getenv("RUGGUARD_FASTNEAR_TRANSFERS_URL", "https://transfers.main.fastnear.com"),
            fastnear_api_key=os.getenv("FASTNEAR_API_KEY") or None,
            history_source=os.getenv("RUGGUARD_HISTORY_SOURCE", "fastnear"),
            rhea_contract=os.getenv(
                "RUGGUARD_RHEA_CONTRACT",
                "ref-finance-101.testnet" if testnet else "v2.ref-finance.near",
            ),
            db_path=Path(os.getenv("RUGGUARD_DB", "data/rugguard.db")),
            timeout_s=float(os.getenv("RUGGUARD_TIMEOUT_S", "20")),
            max_retries=int(os.getenv("RUGGUARD_MAX_RETRIES", "3")),
            cache_ttl_s=int(os.getenv("RUGGUARD_CACHE_TTL_S", str(6 * 3600))),
            offline=_flag("RUGGUARD_OFFLINE"),
            min_interval_s={
                "nearblocks": float(os.getenv(
                    "RUGGUARD_NEARBLOCKS_MIN_INTERVAL_S",
                    # Free plan: 6 calls/min, 333/day, 10,000/month. 12 s keeps us at 5/min.
                    "1" if os.getenv("NEARBLOCKS_API_KEY") else "12",
                )),
                "rpc": float(os.getenv("RUGGUARD_RPC_MIN_INTERVAL_S", "0.1")),
                "fastnear_tx": float(os.getenv("RUGGUARD_FASTNEAR_MIN_INTERVAL_S", "0.3")),
            },
        )

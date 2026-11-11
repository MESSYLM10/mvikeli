"""Collection pipeline: one token in, one snapshot (plus upgrade-surface and concentration scores) out.

Each source is wrapped so that one failing API does not lose the whole snapshot;
failures are recorded in `snapshots.errors_json` and the affected score becomes "unknown".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .analysis import concentration, upgrade_surface
from .analysis.wasm import ANALYSER_VERSION, analyse_wasm
from .config import Settings
from .http import FetchError, JsonClient, Transport
from .sources import EMPTY_CODE_HASH, FastNear, NearBlocks, NearRpc, Rhea
from .storage.db import Database


@dataclass
class CollectOptions:
    transfers_pages: int = 2        # NearBlocks pages of 25 newest transfers
    with_pools: bool = False        # scan Rhea pools (many cached RPC calls on first run)
    with_nearblocks: bool = True
    # NearBlocks FT details and /count endpoints: off by default. On the free tier the count
    # endpoints return 403 "currently unavailable", and details add little for the score.
    nearblocks_stats: bool = False
    classify_holders: int = 15      # check this many top holders for contract code (RPC, cached)


@dataclass
class CollectResult:
    token_id: str
    snapshot_id: int
    surface: dict
    concentration: dict
    errors: list[dict] = field(default_factory=list)
    counts: dict = field(default_factory=dict)


class Collector:
    def __init__(self, settings: Settings, db: Database, transport: Transport | None = None,
                 sleep: Callable[[float], None] | None = None):
        self.settings = settings
        self.db = db
        client = JsonClient(settings, db, transport, **({"sleep": sleep} if sleep else {}))
        self.client = client
        self.rpc = NearRpc(client, settings.rpc_url)
        self.fastnear = FastNear(client, settings.fastnear_url, settings.fastnear_transfers_url,
                                 settings.fastnear_api_key)
        self.nearblocks = NearBlocks(client, settings.nearblocks_url, settings.nearblocks_api_key)
        self.rhea = Rhea(self.rpc, settings.rhea_contract)

    def _try(self, errors: list, step: str, fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except (FetchError, ValueError, KeyError, TypeError) as exc:
            errors.append({"step": step, "error": str(exc)[:500]})
            return None

    def code_analysis(self, token_id: str, account: dict, errors: list) -> dict | None:
        """Fetch and analyse the code the account actually runs (local or global), cached by hash."""
        if account.get("global_contract_account_id"):
            fetch = lambda: self.rpc.view_global_contract_code_by_account_id(account["global_contract_account_id"])
            known_hash = None
        elif account.get("global_contract_hash"):
            known_hash = account["global_contract_hash"]
            fetch = lambda: self.rpc.view_global_contract_code(known_hash)
        elif account.get("code_hash") not in (None, "", EMPTY_CODE_HASH):
            known_hash = account["code_hash"]
            fetch = lambda: self.rpc.view_code(token_id)
        else:
            return None

        if known_hash:
            cached = self.db.get_contract_code(known_hash)
            # Reuse a stored analysis only if the current analyser produced it; otherwise
            # re-analyse (the WASM itself comes back from the raw-response cache, no network).
            if cached is not None and cached["flags"].get("analyser_version") == ANALYSER_VERSION:
                return cached
        got = self._try(errors, "view_code", fetch)
        if got is None:
            return None
        code_hash, wasm = got
        analysis = analyse_wasm(wasm)
        self.db.put_contract_code(code_hash or known_hash or "unknown", analysis)
        return analysis

    def classify_holders(self, holders: list[dict], top_n: int, errors: list) -> set[str]:
        """Return the top holders that have contract code (pools, escrows, bridges, lockups).

        One cached view_account per holder. A holder whose lookup fails (e.g. an implicit
        account that does not exist yet) is treated as a wallet, the cautious choice.
        """
        contracts: set[str] = set()
        failures = 0
        for h in holders[:top_n]:
            if h["account_id"] in concentration.DEFAULT_EXCLUDED:
                continue   # known pool, escrow or burn address: no lookup needed
            try:
                view = self.rpc.view_account(h["account_id"], ttl_s=7 * 86400)
            except FetchError as exc:
                # An account that does not exist on-chain (e.g. an unfunded implicit account)
                # cannot hold code, so it is a wallet; that is an answer, not an error.
                if "UNKNOWN_ACCOUNT" not in str(exc):
                    failures += 1
                continue
            if (view.get("code_hash") not in (None, "", EMPTY_CODE_HASH)
                    or view.get("global_contract_hash") or view.get("global_contract_account_id")):
                contracts.add(h["account_id"])
        if failures:
            errors.append({"step": "classify_holders",
                           "error": f"{failures} holder lookup(s) failed; treated as wallets"})
        return contracts

    def collect(self, token_id: str, opts: CollectOptions | None = None) -> CollectResult:
        opts = opts or CollectOptions()
        errors: list[dict] = []
        counts: dict = {}

        meta = self._try(errors, "ft_metadata", lambda: self.rpc.ft_metadata(token_id))
        supply = self._try(errors, "ft_total_supply", lambda: self.rpc.ft_total_supply(token_id))
        self.db.upsert_token(token_id, meta, supply)

        account = self._try(errors, "view_account", lambda: self.rpc.view_account(token_id))
        snapshot_id = self.db.new_snapshot(token_id, self.settings.network,
                                           (account or {}).get("block_height"))

        if account:
            self.db.put_account_state(snapshot_id, token_id, account)
        keys = self._try(errors, "view_access_key_list", lambda: self.rpc.view_access_key_list(token_id))
        if keys is not None:
            self.db.put_access_keys(snapshot_id, token_id, keys)
            counts["keys"] = len(keys)

        code = self.code_analysis(token_id, account, errors) if account else None

        holders = self._try(errors, "fastnear_top_holders", lambda: self.fastnear.top_holders(token_id))
        if holders:
            self.db.put_holders(snapshot_id, "fastnear", holders)
            counts["holders_top"] = len(holders)

        contract_holders = self.classify_holders(holders or [], opts.classify_holders, errors)
        if contract_holders:
            counts["contract_holders"] = len(contract_holders)

        deployments = None
        if opts.with_nearblocks:
            # Deploy history first: it feeds the upgrade-surface score, so it gets the rate limit first.
            deployments = self._try(errors, "nearblocks_deployments",
                                    lambda: self.nearblocks.contract_deployments(token_id))
            if deployments is not None:
                self.db.put_deployments(token_id, deployments)
                counts["deployments"] = len(deployments)
            if opts.transfers_pages > 0:
                transfers = self._try(errors, "nearblocks_transfers",
                                      lambda: self.nearblocks.ft_transfers(token_id, 25, opts.transfers_pages))
                if transfers:
                    counts["transfers_new"] = self.db.put_transfers(token_id, transfers)
            if opts.nearblocks_stats:
                details = self._try(errors, "nearblocks_ft", lambda: self.nearblocks.ft_details(token_id))
                holder_count = self._try(errors, "nearblocks_holder_count",
                                         lambda: self.nearblocks.ft_holder_count(token_id))
                transfer_count = self._try(errors, "nearblocks_transfer_count",
                                           lambda: self.nearblocks.ft_transfer_count(token_id))
                self.db.put_token_stats(snapshot_id, holder_count, transfer_count, details)

        if opts.with_pools:
            pools = self._try(errors, "rhea_pools", lambda: self.rhea.pools_with_token(token_id))
            if pools:
                self.db.put_pools(snapshot_id, self.settings.rhea_contract, pools)
                counts["pools"] = len(pools)

        surface = upgrade_surface.assess(token_id, account, keys, code, deployments).as_dict()
        self.db.put_score(snapshot_id, "upgrade_surface", surface["score"], surface["band"],
                          surface["reasons"], surface["facts"], upgrade_surface.WEIGHTS_VERSION)
        conc = concentration.assess(holders or [], supply, contract_holders=contract_holders)
        self.db.put_score(snapshot_id, "concentration", conc["score"], conc["band"],
                          conc["reasons"], conc["facts"], concentration.WEIGHTS_VERSION)

        self.db.set_snapshot_meta(snapshot_id, (account or {}).get("block_height"), errors)
        return CollectResult(token_id, snapshot_id, surface, conc, errors, counts)

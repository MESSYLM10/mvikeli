"""Talk to the guard vault contract (contracts/guard-vault) through near-cli-rs.

Mvikeli is the vault's oracle: `post_score` reads the latest stored total score for a token and posts it
on-chain with `set_score`. The agent side (`request_swap`) is signed with the agent account's
function-call key, which the vault is the only contract it can call.

Commands are built as argument lists and run without a shell. `dry_run` prints them instead, so the exact
transaction can be checked before anything is signed.
"""

from __future__ import annotations

import json
import shlex
import subprocess
from dataclasses import dataclass, field
from typing import Callable

from .validate import account_id

NEAR = 10**24


def to_yocto(near: str | float) -> int:
    """'1.5' NEAR -> yoctoNEAR, without float rounding."""
    whole, _, frac = str(near).partition(".")
    if not (whole.isdigit() or whole == "") or (frac and not frac.isdigit()) or len(frac) > 24 \
            or not (whole or frac):
        raise ValueError(f"not a NEAR amount: {near!r}")
    return int(whole or 0) * NEAR + int((frac or "0").ljust(24, "0"))


@dataclass
class NearCli:
    network: str = "testnet"
    binary: str = "near"
    dry_run: bool = False
    runner: Callable[..., subprocess.CompletedProcess] = field(default=subprocess.run)
    printed: list[str] = field(default_factory=list)

    def _run(self, argv: list[str]) -> str:
        line = " ".join(shlex.quote(a) for a in argv)
        self.printed.append(line)
        if self.dry_run:
            return ""
        try:
            done = self.runner(argv, capture_output=True, text=True, timeout=120)
        except FileNotFoundError as exc:
            raise RuntimeError("near-cli-rs is not installed: cargo install --locked near-cli-rs") from exc
        out = (done.stdout or "") + (done.stderr or "")
        if done.returncode != 0:
            raise RuntimeError(f"near exited with {done.returncode}:\n{out.strip()[-2000:]}")
        return out

    def view(self, contract: str, method: str, args: dict) -> str:
        return self._run([self.binary, "contract", "call-function", "as-read-only", contract, method,
                          "json-args", json.dumps(args), "network-config", self.network, "now"])

    def call(self, contract: str, method: str, args: dict, signer: str, gas_tgas: int = 100,
             deposit: str = "0 NEAR", key_file: str | None = None) -> str:
        # The OS keychain by default; a key file (e.g. ~/.near-credentials/testnet/<acct>.json) when given.
        sign = ["sign-with-access-key-file", key_file] if key_file else ["sign-with-keychain"]
        return self._run([self.binary, "contract", "call-function", "as-transaction", contract, method,
                          "json-args", json.dumps(args), "prepaid-gas", f"{gas_tgas}.0 Tgas",
                          "attached-deposit", deposit, "sign-as", signer, "network-config", self.network,
                          *sign, "send"])


def latest_total(db, token: str) -> dict:
    rows = [r for r in db.latest_scores(token) if r["kind"] == "total"]
    if not rows or rows[0]["score"] is None:
        raise LookupError(f"No combined score stored for {token}: run `rugguard score {token}` first.")
    r = rows[0]
    return {"score": int(round(r["score"])), "rules_version": r["weights_version"], "band": r["band"],
            "computed_at": r["computed_at"]}


def post_score(cli: NearCli, db, token: str, vault: str, oracle: str, as_token: str | None = None) -> dict:
    """Post Mvikeli's latest score for `token` to the vault, optionally under a testnet stand-in ID."""
    s = latest_total(db, account_id(token))
    target = account_id(as_token or token)
    evidence = f"rugguard {s['rules_version']} score of {token} at {s['computed_at']}"
    if target != token:
        evidence = f"stand-in for mainnet {token}: " + evidence
    cli.call(account_id(vault), "set_score",
             {"token": target, "score": s["score"], "rules_version": s["rules_version"], "evidence": evidence[:256]},
             account_id(oracle))
    return {"token": target, **s, "evidence": evidence[:256]}


def request_swap(cli: NearCli, vault: str, agent: str, token: str, amount_near: str, pool_id: int,
                 min_out: str = "1", key_file: str | None = None) -> str:
    return cli.call(account_id(vault), "request_swap",
                    {"token_out": account_id(token), "amount_in": str(to_yocto(amount_near)),
                     "pool_id": int(pool_id), "min_amount_out": str(int(min_out))},
                    account_id(agent), gas_tgas=200, key_file=key_file)


def check(cli: NearCli, vault: str, token: str, amount_near: str) -> str:
    return cli.view(account_id(vault), "check", {"token_out": account_id(token),
                                                  "amount_in": str(to_yocto(amount_near))})


def config(cli: NearCli, vault: str) -> str:
    return cli.view(account_id(vault), "get_config", {})

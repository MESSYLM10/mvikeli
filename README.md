# RugGuard

**Rug-pull and dump-risk signals for NEAR NEP-141 tokens.** Built for the NEAR Hackathon (Nov 2026),
tracks: *AI Security & Defense* (primary) and *Memecoins With Mechanics*.

RugGuard checks a token before you (or your AI agent) buy it, and explains its verdict in plain words.
Its headline check is NEAR-specific: the **upgrade surface**, meaning whether anyone can still replace the
token's code or seize its account.

> Output is a set of *risk signals*, not an accusation. A high band means "these powers exist", not "this is a scam".

## Try it in one minute (offline, no API keys)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[api]"
python -m rugguard demo            # results for 30+ mainnet tokens, evaluation reports, testnet guard run
python -m rugguard demo --serve    # the same data in the web UI: http://127.0.0.1:8000
```

`demo` restores a packed copy of a real RugGuard database (`demo/rugguard-demo.db.gz`) to
`data/demo.db` and makes no network calls. It prints the holdout and dev-set evaluation, writes the
per-token CSV and Markdown reports to `runs/`, and lists the testnet transactions of the guard vault.
The pack includes the cached API responses, so stored scores can be re-computed offline:
`RUGGUARD_DB=data/demo.db RUGGUARD_OFFLINE=1 python -m rugguard score bome.narrowcap6721.near`.
`docker compose up --build` serves the same demo at http://127.0.0.1:8080.

## What it checks today

| Check | Source | High risk when |
| --- | --- | --- |
| Full-access keys on the token account | RPC `view_access_key_list` | Any full-access key exists: the holder can redeploy the contract |
| Global-contract binding | RPC `view_account` | Code follows a global contract **by account ID**, so its owner can change it |
| Self-upgrade / key backdoor in code | RPC `view_code` → WASM import + export parsing | Code exports an `upgrade`-style or `add_key` method *and* links the matching host function; or exports `mint`/`pause`/`blacklist`-style methods (medium) |
| Redeploy history | NearBlocks v3 deployments | More than one deploy (medium) |
| Holder concentration | FastNEAR top 100 ÷ `ft_total_supply`; top holders checked for contract code | Top 10 *wallets* hold ≥ 80%. Balances in contracts (pools, NEAR Intents, lockups) are reported separately, not counted as dump risk |

### Calibration from the first mainnet run

Bands are upgrade surface / holder concentration.

| Token | Run 1 | Run 2 | Run 3 (expected) | What changed |
| --- | --- | --- | --- | --- |
| wrap.near | medium / high | medium / low | low / low | Run 2: intents.near (48%) recognised as an escrow contract. Run 3: deploy history alone no longer raises the band once every upgrade path is closed |
| token.v2.ref-finance.near | medium / low | low / low | low / low | Imports alone are a note: near-sdk builds link deploy and add-key host functions in every contract |
| usdt.tether-token.near | medium / high | medium / high | medium / high | Correct: upgradable, mintable, pausable, blacklistable; the issuer treasury holds 93%. `upgrade_icon`/`upgrade_name_symbol` now reported as metadata changes, not code upgrades |
| meta-pool.near | medium / high | medium / low | medium / low | Exports `upgrade`, `set_owner_id`, `withdraw_all`; holders are mostly contracts |

Stored code analyses carry an analyser version and are recomputed when the logic changes.

Why three code checks and not just keys: NEAR docs note that a contract with no full-access keys can
still upgrade itself through its own update method, can add itself new keys, and, if it uses a global
contract by account ID, runs whatever that owner publishes.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e .                                         # or: pip install -r requirements.txt
cp .env.example .env                                     # optional: NearBlocks API key, endpoints

rugguard init-db
rugguard surface wrap.near                 # live upgrade-surface check, a few RPC calls
rugguard collect wrap.near --pools         # full snapshot into data/rugguard.db
rugguard collect-seeds seeds/tokens.txt    # every token in the seed list
rugguard report wrap.near                  # latest stored scores
rugguard export holders --out holders.csv  # any table to CSV
```

No install? `python -m rugguard <command>` works from the repo root after `pip install requests`.

**Offline replay:** every response is cached in SQLite. Set `RUGGUARD_OFFLINE=1` to re-run
collection and scoring from the cache only, e.g. for a demo when an API is down.

**Docker:**

```bash
docker build -t rugguard .
docker run --rm -v "$PWD/data:/app/data" rugguard collect wrap.near
```

## Example output

```
rug.meme.near  (snapshot 1, counts {"keys": 1, "holders_top": 3, "deployments": 3})
  Upgrade surface: HIGH
    - 1 full-access key on rug.meme.near: the holder can replace the token contract or move the account's funds at any time.
    - Contract code can deploy new code (promise_batch_action_deploy_contract); check who is allowed to call it.
    - Exports owner-power methods: mint.
    - Contract code has been deployed 3 times; check whether any redeploy happened after trading began.
  Holder concentration: HIGH
    - Top 10 wallets (excluding pools/burn) hold 75% of supply; the largest single wallet holds 70%.
    - dev.near alone could crash the price by selling.
```

(From the offline test fixtures; real tokens are collected with the commands above.)

## Running the service (web UI + API)

```bash
pip install -e ".[api]"
rugguard serve                       # http://127.0.0.1:8000 · API reference at /docs
```

The UI is a single report page per token: who can change the code (checked live), who holds the
supply (from the latest snapshot), and the post-mortem with its evidence. Below it are the post-mortem
table with reviews and agreement, a scan of recent launches, the job queue and an activity log.

**Contained deployment** (recommended whenever anyone else can reach the machine):

```bash
docker compose up --build            # http://127.0.0.1:8080
```

| Service | Exposure | Hardening |
| --- | --- | --- |
| `proxy` (Caddy) | The only published port, bound to `127.0.0.1` unless you change `RUGGUARD_BIND` | Request size and time limits, security headers, no admin API |
| `api` | Internal network only; never published | Unprivileged user, read-only root filesystem, no Linux capabilities, no privilege escalation, memory/CPU/process limits, only the data volume is writable |

**Why there is little to "partition" from meme.cooking or NEAR:** RugGuard only makes *outgoing*
read-only requests to NEAR RPC, FastNEAR and NearBlocks. Nothing on the chain or on meme.cooking can
connect to you. What needs guarding is the port you open, and the data the chain hands back:

| Threat | Control |
| --- | --- |
| Hostile token metadata (names, SVG icons) reaching the browser | The UI inserts all chain data as text, never HTML; icons are never rendered; strict Content-Security-Policy with no inline script; a test fails if HTML-injection sinks appear in the UI code |
| Crafted token IDs altering request URLs or queries | Every ID is checked against NEAR account-ID rules at every entry point (CLI and API) before use; SQL is parameterised |
| Someone burning your NearBlocks quota or CPU | Quota-spending work runs through a bounded queue (25 waiting, 3 per client, duplicates merged, one worker); per-client rate limit (`RUGGUARD_RATE_PER_MIN`, default 120); `X-Forwarded-For` trusted only behind the proxy |
| Malicious contract code | WASM is parsed with bounds checks and never executed |
| Unauthorised writes | Set `RUGGUARD_ADMIN_TOKEN` and reviews and the activity log require it (constant-time comparison) |
| Key theft | The service holds no NEAR keys. The guard-agent demo will run as a separate service with a testnet function-call key |

**Logs:** decisions (scores, outcomes, evidence, rules versions) are stored in the database.
Operational logs are JSON lines, one per source call, job and request, with source, target, status,
latency and cache hit. They go to `RUGGUARD_LOG_FILE` (in Docker: `/app/data/rugguard.log`) and to the
UI's activity panel. Headers and query strings are never logged, so API keys cannot leak into logs.

The `/docs` page loads Swagger UI from jsDelivr, so only that page gets a looser CSP. Disable it for a
public deployment if you prefer no third-party requests.

## Combined risk score (0-100)

```bash
rugguard score some.token.near            # or --seeds seeds/memecoins.txt
rugguard evaluate                          # scores vs post-mortem outcomes
```

The score uses **only data up to one hour after launch** (`--cut-hours` to change). Comparing it with
the post-mortem label is therefore a real prediction, and a test checks that a dump after the cut-off
cannot change the score.

| Signal | Weight | Sub-score 1.0 (worst) when |
| --- | --- | --- |
| Code can be changed | 25 | A full-access key or global binding (0.5 for an exported upgrade/add-key method) |
| A wallet controls the liquidity | 20 | Liquidity was added by a wallet (it holds the pool shares and can withdraw them), not a launchpad or locker contract |
| Supply in few wallets | 15 | Top 10 wallets hold ≥ 80% at the cut-off (contracts, pools, burn excluded) |
| Thin pool | 10 | The pool holds ≤ 2% of supply at the cut-off, or under 10 NEAR |
| Early buyers linked to deployer | 10 | ≥ 50% of first-hour buys went to wallets linked to the deployer by token transfers, or 3 buyers took most of it |
| Deployer still holds supply | 10 | The first wallet to hold supply, plus wallets it seeded before launch, hold ≥ 30% |
| Owner powers in the code | 10 | `mint`, `pause`, `blacklist`, ownership transfer and similar are exported |

**Red-flag floors:** a weighted average dilutes conditions that are enough on their own for a rug.
Someone able to replace the code sets a minimum of 60; one wallet group controlling the supply against a
thin pool sets a minimum of 70. Both the plain weighted score and any floor applied are stored and shown.
(Found on mainnet: `sinu-8.smithii.near`, with one wallet holding ~100% against a pool of a few NEAR,
averaged to 55 without the floor.)

A signal that cannot be computed is left out and the rest rescaled; the result reports its **coverage**
(share of weight used), so a score never claims more than the data supports. Bands: 0–29 low, 30–59
elevated, 60+ high.

`evaluate` joins each token's latest score with its outcome label (a human review overrides the automatic
label) and reports rugs caught at a score of 60+, false alarms, and AUC: the chance that a random rugged
token outscores a random non-rugged one. Pending, spoiled and needs-review tokens are excluded.

## Post-mortem: what actually happened to a token

RugGuard has two halves that never share inputs:

- **Pre-flight** (`collect`, `surface`): what was knowable about a token's risk.
- **Post-mortem** (`outcome`): what happened next. It labels tokens by explicit, versioned rules, so evaluation labels are reproducible instead of judged by eye.

```bash
rugguard discover --recent 300 --out seeds/memecoins.txt  # tokens from the newest Rhea pools
rugguard rules                                    # the rules and thresholds in force (JSON)
rugguard history some.meme.near --show-sample     # fetch full transfer history; check the parser
rugguard outcome some.meme.near other.near        # label + evidence
rugguard outcome --seeds seeds/memecoins.txt
rugguard review some.meme.near faded --reason "slow bleed, no insider sells"
rugguard outcomes                                 # auto vs human labels, agreement, Cohen's kappa
rugguard forget some.meme.near                    # remove a token's outcomes and reviews
```

Labels (outcome-rules-v2), checked in this order:

| Label | Rule (outcome-rules-v1) |
| --- | --- |
| `needs_review` | History incomplete, price can't be measured, a required source failed, or the token first traded more than 90 days ago (not a launch) |
| `spoiled` | Never reached a trading pool (e.g. a meme.cooking auction under its soft cap) |
| `rugged` | Pool keeps < 20% of the NEAR put in at launch (liquidity drained, even if the price holds); **or** price < 10% of launch **and** (insider exit **or** code redeployed after launch) |
| `pending` | Launched under 72 h ago (unless already rugged) |
| `faded` | Price < 10% of launch, no insider exit: died of lost interest |
| `alive` | Price ≥ 10% of launch |

**Insider exit:** among the 10 largest *wallets* whose balance peaked at ≥ 1% of supply (contracts such as
launchpads and escrows excluded), any that sent ≥ 50% of its peak to a DEX within 72 h of launch, with
exits totalling ≥ 2% of supply.

**Price is measured:** the pool's price now (Rhea reserves) against the price set at launch (the
launcher's token deposit and its wNEAR deposit, read from FastNEAR). Refunded deposits are skipped when
finding the launch. Without the wNEAR deposit, a constant-product estimate is used only when it is
plausible (under 20x); otherwise the token goes to review. **Why "drained" and not just "collapsed":**
removing liquidity takes both sides out in proportion, so the price can stay flat while holders lose
the ability to sell. Found on the smithii.near series: 372 NEAR in at launch, 20 NEAR left.

**Evaluation files:** every `evaluate` run saves `runs/eval-<set>-<UTC time>.csv` (one row per token: verdict, outcome, score, each signal's subscore, launch transaction, pool state, rule versions, snapshot and score IDs, evidence) and a matching `.md` report (recall, false alarms, AUC, a caught/missed table, the misses with their evidence, and the rules used). Files are never overwritten; `--out STEM` names them, `--no-files` skips them, `--seeds FILE` limits the run to one set, e.g. the holdout.

**Finding tokens:** Rhea assigns pool IDs in creation order, so `discover` lists the tokens paired with wNEAR in the newest pools, newest first, with each pool's wNEAR depth. Near-empty pools are candidates for rugs or fades, deep ones for survivors. `--suffix` keeps one launchpad's tokens. For a holdout set, `--exclude seeds/memecoins.txt --limit 20` skips the tokens you tuned on and keeps the newest 20; tokens launched more than 90 days ago come back `needs_review` (not judged), so keep `--recent` small. Names that are not real NEP-141 tokens are rejected before anything is stored.

Every label prints the evidence behind it (accounts, shares, hours, transaction hashes). Override any
threshold with `--rules my_rules.json`; unknown keys are rejected, and every stored label records the
rules version. Human reviews are stored separately, and `rugguard outcomes` reports agreement.

**Where history comes from.** By default, FastNEAR's Transfers API (no key needed when tested). It
answers "transfers of account X" only, so RugGuard rebuilds a token's history from the accounts that
matter: the Rhea pool, the launcher, the largest sellers and first-hour buyers, the current top holders
and the wallets the launcher seeded, typically 10-40 queries per token. Each of those accounts' balances
and peaks are then exact. NearBlocks (`--source nearblocks`) gives every transfer, but its free plan
allows 6 calls a minute and 333 a day, with each 25 rows costing a call; RugGuard paces it and stops a
batch cleanly when refused. `rugguard inspect <token>` summarises what is stored, without API calls.

**Cost (NearBlocks):** history walks back 25 transfers per call on the free plan, cached. Older pages are kept for a year,
and a later re-run stops as soon as it reaches stored transfers. A token with 2,000 transfers takes about
20 calls, roughly 3.5 minutes at the free-tier pacing.

## Repository layout

```
rugguard/
├── config.py            endpoints and settings from env / .env
├── http.py              retrying JSON client + SQLite response cache + offline mode
├── sources/
│   ├── rpc.py           NEAR JSON-RPC: view_account, view_access_key_list, view_code, NEP-141 views
│   ├── fastnear.py      FastNEAR /v1/ft/{token}/top holders
│   ├── nearblocks.py    NearBlocks v3: FT details, holder/transfer counts, transfers, deployments
│   └── rhea.py          Rhea (Ref) v2.ref-finance.near pools containing the token
├── analysis/
│   ├── wasm.py          WASM import/export reader + capability flags
│   ├── upgrade_surface.py
│   ├── concentration.py
│   ├── transfers.py     normalise NearBlocks FT events, replay balances
│   ├── outcome.py       post-mortem rules, labels, evidence, Cohen's kappa
│   └── score.py         combined 0-100 score from data up to the cut-off, AUC
├── postmortem.py        history + pools + deployments + holder checks → outcome
├── scoring.py          score pipeline and evaluation
├── api/                FastAPI app, job queue, static UI (no inline script)
├── validate.py         NEAR account-ID validation for every input
├── logs.py             JSON operational logs + in-memory ring for the UI
├── storage/schema.sql   14 tables; u128 amounts stored as TEXT
├── storage/db.py
├── collect.py           one token → one snapshot + scores; per-source error capture
└── cli.py
tests/                   offline tests: synthetic WASM binaries + fake NEAR/FastNEAR/NearBlocks APIs
seeds/tokens.txt
```

## Data model

`raw_responses` (cache) · `tokens` · `snapshots` · `account_state` · `access_keys` · `contract_code`
(deduplicated by code hash) · `holders` · `token_stats` · `ft_transfers` · `deployments` · `pools` ·
`risk_scores`. See `rugguard/storage/schema.sql`.

## Tests

```bash
pip install pytest && python -m pytest -q
```

The suite runs fully offline: it builds real WebAssembly binaries with chosen host-function imports and
serves NEAR-shaped RPC, FastNEAR and NearBlocks responses from a fake transport.

## Configuration

| Variable | Default |
| --- | --- |
| `RUGGUARD_NETWORK` | `mainnet` (`testnet` switches all endpoints) |
| `RUGGUARD_RPC_URL` | `https://rpc.mainnet.near.org` |
| `RUGGUARD_FASTNEAR_URL` | `https://api.fastnear.com` |
| `RUGGUARD_NEARBLOCKS_URL` | `https://api.nearblocks.io` |
| `NEARBLOCKS_API_KEY` | unset (free tier) |
| `RUGGUARD_DB` | `data/rugguard.db` |
| `RUGGUARD_OFFLINE` | `0` |
| `RUGGUARD_NEARBLOCKS_MIN_INTERVAL_S` | `10` without an API key, `1` with one |

**NearBlocks free tier:** calls are paced 10 s apart and a `429` backs off 15 → 30 → 60 s. The FT
details and `/count` endpoints returned `403 "currently unavailable"` in testing, so they are off by
default (`--stats` turns them on); an endpoint that returns that error is skipped for the rest of the run.
With pacing, each token costs about 30–40 s of NearBlocks time; `--no-nearblocks` skips it entirely.

## Known limits

- An exported upgrade method proves a *capability*, not who can trigger it; medium-band code findings need a human look at the method's access checks.
- Host-function imports do not discriminate on NEAR (near-sdk links them all), so the code check relies on exported method names, which can be renamed.
- Method-name hints can be evaded by renaming.
- NearBlocks v3 response envelopes are parsed defensively and raw bodies are always stored; field names are to be confirmed against live responses.
- Earliest-buyer clustering and liquidity scoring are on the roadmap (see below).

## Roadmap to submission (14 Nov 2026)

1. Funding-trail collection and early-buyer clustering
2. Weighted 0–100 score across six signals; small HTTP API
3. Guard agent holding only a function-call key, blocking swaps above a threshold
4. "Honest memecoin" NEP-141 contract: locked, vested team share, sell-rate limit

## Sources

- NEAR access keys: https://docs.near.org/protocol/access-keys
- Updating and locking contracts: https://docs.near.org/smart-contracts/release/upgrade
- RPC (accounts and contracts): https://docs.near.org/api/rpc/contracts
- FT standard (NEP-141): https://docs.near.org/primitives/ft/standard
- FastNEAR API: https://docs.near.org/tools/ecosystem-apis/fastnear-api
- DEX (Rhea / Ref Finance): https://docs.near.org/primitives/dex

Powered by Nearblocks.io APIs.

## Guard vault: an AI agent that cannot buy a rug

`contracts/guard-vault` is a NEAR contract for the AI Security track: the AI agent holds only a
function-call key that can call the vault's `request_swap`. Function-call keys cannot attach deposits, so
the agent cannot swap on its own; the vault swaps for it on Rhea, and first refuses when RugGuard's posted
score is 60 or more, when no fresh score exists (fail closed), or when the swap is over the per-swap or
daily limit. Refusals are recorded on-chain with the score and evidence. RugGuard posts scores as the
vault's oracle (`rugguard guard post-score`). Build, deploy and demo steps: `contracts/guard-vault/README.md`.

Verified on testnet: the agent's request to buy a token scored 90 was refused on-chain (`HIGH_RISK`), a token scored 20 was swapped on Rhea through the vault, and a 2.5 wNEAR request over the 2 wNEAR limit was refused; transaction links are in the contract README.

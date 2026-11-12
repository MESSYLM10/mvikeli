# Guard vault (testnet)

The AI agent gets money, not the keys to everything.

| Who | Holds | Can do |
|---|---|---|
| Owner (`rugguard-demo.testnet`) | full-access key | fund the vault, set limits, pause, change agent or oracle |
| RugGuard oracle (`oracle.rugguard-demo.testnet`) | its own key | `set_score(token, score, rules_version, evidence)` |
| AI agent (`agent.rugguard-demo.testnet`) | **one function-call key only**: receiver = vault, method = `request_swap` | ask the vault to swap |
| Vault (`vault.rugguard-demo.testnet`) | the wNEAR | checks every request, then swaps on Rhea itself |

A function-call key cannot attach a deposit, and Rhea swaps go through wNEAR `ft_transfer_call`, which needs
1 yoctoNEAR. So the agent cannot swap on its own: every swap has to pass the vault's checks.

**Checks, in order:** not paused → RugGuard has scored the token (otherwise refuse: the guard fails closed)
→ the score is under 24 h old → score below the threshold (60) → amount within the per-swap limit → within
the daily cap. A refusal returns normally, so the decision and its reason stay on-chain as a
`rugguard-guard` event (`swap_refused`, code `HIGH_RISK`, with the score and evidence).

## Test it (no network)

```
cargo test        # 12 tests: refusal with evidence, fail-closed, stale score, limits, pause, roles
```

## Deploy (testnet)

Needs Rust with `wasm32-unknown-unknown`, `cargo-near` and `near-cli-rs`. Replace `rugguard-demo` with your
account. near-cli-rs is interactive: if a command's wording differs on your version, run `near` alone and
follow the menus with the same choices.

```bash
# 1. Build
cd contracts/guard-vault
cargo near build non-reproducible-wasm          # -> target/near/guard_vault.wasm

# 2. Sub-accounts (paid from the owner account)
for a in vault:4 oracle:1 agent:1; do
  near account create-account fund-myself ${a%%:*}.rugguard-demo.testnet "${a##*:} NEAR" \
    autogenerate-new-keypair save-to-keychain sign-as rugguard-demo.testnet \
    network-config testnet sign-with-keychain send
done

# 3. Deploy and initialise: threshold 60, 2 wNEAR per swap, 3 wNEAR a day, scores valid 24 h
near contract deploy vault.rugguard-demo.testnet use-file target/near/guard_vault.wasm \
  with-init-call new json-args '{"owner":"rugguard-demo.testnet","oracle":"oracle.rugguard-demo.testnet",
  "agent":"agent.rugguard-demo.testnet","dex":"ref-finance-101.testnet","wnear":"wrap.testnet",
  "threshold":60,"max_per_swap":"2000000000000000000000000","daily_cap":"3000000000000000000000000",
  "max_score_age_hours":24}' prepaid-gas '100.0 Tgas' attached-deposit '0 NEAR' \
  network-config testnet sign-with-keychain send

# 4. Give the vault wNEAR (register, then wrap 3 NEAR)
near contract call-function as-transaction wrap.testnet storage_deposit json-args '{}' \
  prepaid-gas '30.0 Tgas' attached-deposit '0.00125 NEAR' sign-as vault.rugguard-demo.testnet \
  network-config testnet sign-with-keychain send
near contract call-function as-transaction wrap.testnet near_deposit json-args '{}' \
  prepaid-gas '30.0 Tgas' attached-deposit '3 NEAR' sign-as vault.rugguard-demo.testnet \
  network-config testnet sign-with-keychain send

# 5. The agent gets a function-call key for request_swap only, then loses its full-access key
near account add-key agent.rugguard-demo.testnet grant-function-call-access \
  --allowance '0.5 NEAR' --contract-account-id vault.rugguard-demo.testnet \
  --function-names request_swap autogenerate-new-keypair save-to-keychain \
  network-config testnet sign-with-keychain send
near account list-keys agent.rugguard-demo.testnet network-config testnet now
near account delete-keys agent.rugguard-demo.testnet public-keys ed25519:<THE_FULL_ACCESS_KEY> \
  network-config testnet sign-with-keychain send

# RugGuard's own key check proves it: the agent has a FunctionCall key and nothing else
RUGGUARD_NETWORK=testnet python3 -m rugguard keys agent.rugguard-demo.testnet
```

## Demo

Testnet has no history of real rugs, so the oracle posts the scores RugGuard computed for two **mainnet**
tokens under two testnet token IDs. The evidence string on-chain says so ("stand-in for mainnet …").

```bash
# Find a wNEAR pool on testnet Rhea for the clean stand-in
RUGGUARD_NETWORK=testnet python3 -m rugguard discover --recent 50

# The vault must be registered with the output token to receive it
near contract call-function as-transaction <CLEAN_TESTNET_TOKEN> storage_deposit json-args '{}' \
  prepaid-gas '30.0 Tgas' attached-deposit '0.00125 NEAR' sign-as vault.rugguard-demo.testnet \
  network-config testnet sign-with-keychain send

# .env: RUGGUARD_VAULT=vault.rugguard-demo.testnet  RUGGUARD_ORACLE=oracle.rugguard-demo.testnet
#       RUGGUARD_AGENT=agent.rugguard-demo.testnet
python3 -m rugguard guard post-score bome.narrowcap6721.near --as-token <RUG_TESTNET_TOKEN>      # 90/100
python3 -m rugguard guard post-score presale.neardrgns.near --as-token <CLEAN_TESTNET_TOKEN>      # 20/100

python3 -m rugguard guard check <RUG_TESTNET_TOKEN> --amount 1          # HIGH_RISK, no transaction
python3 -m rugguard guard swap  <RUG_TESTNET_TOKEN> --amount 1 --pool <POOL>   # refused on-chain
python3 -m rugguard guard swap  <CLEAN_TESTNET_TOKEN> --amount 1 --pool <POOL> # approved, swapped on Rhea
python3 -m rugguard guard swap  <CLEAN_TESTNET_TOKEN> --amount 2.5 --pool <POOL> # OVER_SWAP_LIMIT
python3 -m rugguard guard config
```

Add `--dry-run` to any `guard` command to print the exact `near` command without signing anything.

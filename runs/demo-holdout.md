# RugGuard evaluation: holdout

- Run: `20261112T081004Z` (UTC), code commit `7de867d`
- Command: `rugguard demo`
- Outcome rules: outcome-rules-v2; score rules: score-v2
- Per-token data: `demo-holdout.csv` (one row per token, with rule versions, snapshot IDs and evidence)

## Result

| Measure | Value |
|---|---|
| Tokens judged | 18 (2 excluded) |
| Rugs caught at score ≥ 60 | 11 of 13 (recall 85%) |
| False alarms | 0 of 5 non-rugged |
| Precision | 100% |
| AUC | 0.95 (0.5 = coin flip, 1.0 = perfect ranking) |
| Mean score | rugged 66.2, alive 28.5 |

| | Scored ≥ 60 | Scored < 60 |
|---|---|---|
| Rugged | 11 caught | 2 missed |
| Not rugged | 0 false alarm(s) | 5 correctly clear |

## Compared with simpler rules

Each simpler rule runs on the same judged tokens. A single signal flags a token when its subscore is at least 0.5.

| Rule | Rugs caught | False alarms | AUC |
|---|---|---|---|
| **RugGuard score ≥ 60** | **11 of 13** | **0 of 5** | **0.95** |
| Flag every token | 13 of 13 | 5 of 5 | 0.50 |
| Only: code can be replaced | 1 of 13 | 0 of 5 | 0.54 |
| Only: a wallet added the liquidity | 12 of 13 | 2 of 5 | 0.76 |
| Only: top wallets hold most of the supply | 11 of 13 | 2 of 5 | 0.87 |
| Only: thin pool at launch | 12 of 13 | 3 of 5 | 0.68 |
| Only: first-hour buying is concentrated or linked | 11 of 13 | 2 of 5 | 0.90 |
| Only: deployer wallet holds most of the supply | 11 of 13 | 4 of 5 | 0.70 |
| Only: owner powers (mint, pause, blacklist) | 1 of 13 | 0 of 5 | 0.54 |

## Read with care

- Small sample (18 judged tokens): treat these rates as indicative, not established.
- 10 of 18 judged tokens share the score 70.0: the red-flag floor decides most cases, so the score acts close to a yes/no rule.

## Tokens

| Token | Outcome | Score | Band | Verdict |
|---|---|---|---|---|
| `bome.narrowcap6721.near` | rugged | 90 | high | caught |
| `6ca890f766fdaeccd6c934da325f5f06ebc63ca9be4f5f1b44d0686af93c64a8` | rugged | 70 | high | caught |
| `elon.near-elon.near` | rugged | 70 | high | caught |
| `id.nsaw.near` | rugged | 70 | high | caught |
| `kimshi.kimshi.near` | rugged | 70 | high | caught |
| `meme.near_moki.near` | rugged | 70 | high | caught |
| `moustache.moustachebaby.near` | rugged | 70 | high | caught |
| `near.shadowprivacy.near` | rugged | 70 | high | caught |
| `ngas.near-gas.near` | rugged | 70 | high | caught |
| `token.nspear.near` | rugged | 70 | high | caught |
| `token.taler-finance.near` | rugged | 70 | high | caught |
| `usdt.tether-token.near` | needs_review | 64 | high | excluded (outcome needs review) |
| `core.pensoro.near` | alive | 45 | elevated | correct clear |
| `kinu-3.nearlytrade.near` | rugged (human) | 40 | elevated | missed |
| `nhum.nearlytrade.near` | alive | 33 | elevated | correct clear |
| `broomy.broomcat.near` | rugged | 31 | elevated | missed |
| `seat.tokens.sickyak1747.near` | alive | 24 | low | correct clear |
| `presale.neardrgns.near` | alive | 20 | low | correct clear |
| `aurora.token-factory.sickyak1747.near` | alive | 20 | low | correct clear |
| `wnear-78a14e.nearafun.near` | needs_review | 0 | low | excluded (outcome needs review) |

## Misses and false alarms

**`kinu-3.nearlytrade.near`**: missed, outcome rugged, score 40

- Outcome: Launched 26.1 days ago: first transfer into v2.ref-finance.near (tx HEqt6BthFwH3fz6e67V2jSdcXqzmSGm8dFafSfoAojmT).
- Outcome: Liquidity drained: pool 8961 now holds 0.25 NEAR, 0.9% of the 27.34 NEAR put in at launch (rule: below 20%).
- Outcome: Price now ≈ 139% of launch price (pool now 0.25 NEAR for 84524429899602228535603 tokens; at launch kulesh_off.user.intear.near put in 27.34 NEAR for 12891700935682098641149450 tokens); collapse line is 10%.
- Outcome: Insider exit: credss.near peaked at 12% of supply and sold 139% of it to the DEX in 8 transfer(s) within 31.3 h of launch (first tx F13YZmkjU7bJ87rNW8fQ1zw32Gu1MZu1Fe1sQjzdzGgR).
- Score: Liquidity was added by the wallet kulesh_off.user.intear.near, which holds the pool shares and can withdraw them at any time.
- Score: At the cut-off the pool held 3.7% of supply, seeded with 27.34 NEAR at launch.
- Score: 6 wallet(s) bought before the cut-off; 0% of that volume went to wallets linked to the deployer, and the top 3 buyers took 82%.

**`broomy.broomcat.near`**: missed, outcome rugged, score 31

- Outcome: Launched 22.9 days ago: first transfer into v2.ref-finance.near (tx 6nDEzSGVi41GE43jFdXT1BYL7mVygKLyd9btyKs6mWmb).
- Outcome: Liquidity drained: pool 8974 now holds 0.00 NEAR, 0.0% of the 109.00 NEAR put in at launch (rule: below 20%).
- Outcome: Price now ≈ 0.0% of launch price (pool now 0.00 NEAR for 10002744918627354586915406029898 tokens; at launch broomcat.near put in 109.00 NEAR for 1000000000000000000000000000000000 tokens); collapse line is 10%.
- Outcome: Insider exit: broomcat.near peaked at 100% of supply and sold 101% of it to the DEX in 2 transfer(s) within 24.6 h of launch (first tx 6nDEzSGVi41GE43jFdXT1BYL7mVygKLyd9btyKs6mWmb).
- Score: Liquidity was added by the wallet broomcat.near, which holds the pool shares and can withdraw them at any time.
- Score: 1 wallet(s) bought before the cut-off; 100% of that volume went to wallets linked to the deployer, and the top 3 buyers took 100%.
- Score: The deployer broomcat.near and 0 wallet(s) it seeded before launch held 8.2% at the cut-off.

## Rules used

```json
{
  "score": {
    "version": "score-v2",
    "cut_hours": 1.0,
    "weights": {
      "upgrade": 25,
      "lp_control": 20,
      "concentration": 15,
      "liquidity": 10,
      "early_buyers": 10,
      "deployer": 10,
      "owner_powers": 10
    },
    "floor_upgrade": 60,
    "floor_drain": 70,
    "band_high": 60,
    "band_elevated": 30,
    "pool_share_safe": 0.3,
    "pool_share_worst": 0.02,
    "thin_pool_near": 10.0,
    "deployer_share_safe": 0.05,
    "deployer_share_worst": 0.3,
    "linked_safe": 0.1,
    "linked_worst": 0.5,
    "dex_accounts": [
      "v2.ref-finance.near",
      "dclv2.ref-labs.near",
      "aggregatedex.near"
    ],
    "launch_pool_account": "v2.ref-finance.near"
  },
  "outcome": {
    "version": "outcome-rules-v2",
    "collapse_ratio": 0.1,
    "min_age_hours": 72,
    "exit_window_hours": 72,
    "insider_min_share": 0.01,
    "insider_top_n": 10,
    "exit_sell_fraction": 0.5,
    "min_exit_supply_share": 0.02,
    "liquidity_removed_near": 1.0,
    "liquidity_drained_fraction": 0.2,
    "max_launch_age_days": 90,
    "max_estimate_ratio": 20.0,
    "dex_accounts": [
      "v2.ref-finance.near",
      "dclv2.ref-labs.near",
      "aggregatedex.near"
    ],
    "launch_pool_account": "v2.ref-finance.near",
    "wnear_id": "wrap.near",
    "wnear_decimals": 24
  }
}
```

The rule versions above identify the thresholds; the JSON shows the defaults for this code commit.

Data: NEAR RPC, FastNEAR, Rhea (Ref Finance) pools. Powered by Nearblocks.io APIs.

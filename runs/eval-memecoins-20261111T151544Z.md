# RugGuard evaluation: memecoins

- Run: `20261111T151544Z` (UTC), code commit `unknown`
- Command: `rugguard evaluate --seeds seeds/memecoins.txt`
- Outcome rules: outcome-rules-v2; score rules: score-v2
- Per-token data: `eval-memecoins-20261111T151544Z.csv` (one row per token, with rule versions, snapshot IDs and evidence)

## Result

| Measure | Value |
|---|---|
| Tokens judged | 12 (2 excluded) |
| Rugs caught at score ≥ 60 | 5 of 7 (recall 71%) |
| False alarms | 3 of 5 non-rugged |
| Precision | 62% |
| AUC | 0.50 (0.5 = coin flip, 1.0 = perfect ranking) |
| Mean score | rugged 58.0, alive 59.2 |

| | Scored ≥ 60 | Scored < 60 |
|---|---|---|
| Rugged | 5 caught | 2 missed |
| Not rugged | 3 false alarm(s) | 2 correctly clear |

## Read with care

- Small sample (12 judged tokens): treat these rates as indicative, not established.
- 8 of 12 judged tokens share the score 70.0: the red-flag floor decides most cases, so the score acts close to a yes/no rule.

## Tokens

| Token | Outcome | Score | Band | Verdict |
|---|---|---|---|---|
| `bmw.neardrgns.near` | alive | 70 | high | false alarm |
| `dabi-7.smithii.near` | rugged | 70 | high | caught |
| `lumos.lumoscore.near` | alive | 70 | high | false alarm |
| `meili-5.smithii.near` | rugged | 70 | high | caught |
| `mmli-6.smithii.near` | rugged | 70 | high | caught |
| `nbot.near_bot.near` | rugged | 70 | high | caught |
| `olccl-4.smithii.near` | alive | 70 | high | false alarm |
| `sinu-8.smithii.near` | rugged | 70 | high | caught |
| `fuk-3.smithii.near` | alive | 50 | elevated | correct clear |
| `unog.near` | alive | 36 | elevated | correct clear |
| `pmoney-4.nearlytrade.near` | rugged | 33 | elevated | missed |
| `nexus.nexusdev.near` | needs_review | 31 | elevated | excluded (outcome needs review) |
| `nico131.tkn.near` | needs_review | 30 | elevated | excluded (outcome needs review) |
| `amd.amemedog.near` | rugged | 23 | low | missed |

## Misses and false alarms

**`bmw.neardrgns.near`**: false alarm, outcome alive, score 70

- Outcome: Launched 17.6 days ago: first transfer into v2.ref-finance.near (tx 6jAcsqXQRvjBze8RXtrK3qiQbWXKzDw5ZTWkYpJV7gmY).
- Outcome: Price now ≈ 107% of launch price (pool now 7.86 NEAR for 4177012301315188724128123 tokens; at launch launchpad.sickyak1747.near put in 7.61 NEAR for 4318181818181818181818181 tokens); collapse line is 10%.
- Outcome: Checked 2 large wallet(s); none sold 50% of its peak within 72 h.
- Score: Red flag: one wallet group controls the supply and the pool is thin, so it can drain the pool by selling.
- Score: The deployer 92154721589a3dfd41153d1e3e6fb29c5da36d38d83c2db100e8aff4bd28bdd7 and 0 wallet(s) it seeded before launch held 57% at the cut-off.
- Score: 1 wallet(s) bought before the cut-off; 100% of that volume went to wallets linked to the deployer, and the top 3 buyers took 100%.

**`lumos.lumoscore.near`**: false alarm, outcome alive, score 70

- Outcome: Launched 7.4 days ago: first transfer into v2.ref-finance.near (tx FyuHZ2CkaKUQWeRqgZGzvL9WypX9s1DeyHWytEK4RAKR).
- Outcome: Price now ≈ 98% of launch price (pool now 0.30 NEAR for 2288431525060000000000 tokens; at launch treasury.lumoscore.near put in 0.30 NEAR for 2268000000000000000000 tokens); collapse line is 10%.
- Outcome: Checked 1 large wallet(s); none sold 50% of its peak within 72 h.
- Score: Red flag: one wallet group controls the supply and the pool is thin, so it can drain the pool by selling.
- Score: Liquidity was added by the wallet treasury.lumoscore.near, which holds the pool shares and can withdraw them at any time.
- Score: At the cut-off the top 10 wallets held 100% of supply.

**`olccl-4.smithii.near`**: false alarm, outcome alive, score 70

- Outcome: Launched 5.2 days ago: first transfer into v2.ref-finance.near (tx bQoBdJQLooqkVoJDPgnS81UKNB4pAM4UwBAyWrkB1Sy).
- Outcome: Price now ≈ 100% of launch price (pool now 17.13 NEAR for 23127989783252779789047 tokens; at launch toughuni8251.near put in 74.00 NEAR for 100000000000000000000000 tokens); collapse line is 10%.
- Outcome: Checked 3 large wallet(s); none sold 50% of its peak within 72 h.
- Score: Red flag: one wallet group controls the supply and the pool is thin, so it can drain the pool by selling.
- Score: Liquidity was added by the wallet toughuni8251.near, which holds the pool shares and can withdraw them at any time.
- Score: At the cut-off the top 10 wallets held 100% of supply.

**`pmoney-4.nearlytrade.near`**: missed, outcome rugged, score 33

- Outcome: Launched 42.1 days ago: first transfer into v2.ref-finance.near (tx HFEpxRxzcHJqZEBT7Zeoc6UQAjTsCji36WJKXHmVhZDE).
- Outcome: Liquidity drained: pool 9004 now holds 0.00 NEAR, 0.0% of the 406.60 NEAR put in at launch (rule: below 20%).
- Outcome: Price now ≈ 0.0% of launch price (pool now 0.00 NEAR for 98550461545035245413 tokens; at launch tuffguy.user.intear.near put in 406.60 NEAR for 828941660632 tokens); collapse line is 10%.
- Outcome: Insider exit: cook290.near peaked at 6.2% of supply and sold 99% of it to the DEX in 2 transfer(s) within -54.6 h of launch (first tx FR6JDmGkHsrcLY9QXcMdDsnM8uLT897S52XXLnMZKH8H).
- Score: Liquidity was added by the wallet tuffguy.user.intear.near, which holds the pool shares and can withdraw them at any time.
- Score: At the cut-off the pool held 0.0% of supply, seeded with 406.60 NEAR at launch.

**`amd.amemedog.near`**: missed, outcome rugged, score 23

- Outcome: Launched 6.7 days ago: first transfer into v2.ref-finance.near (tx 8vTbLvbhaRfsCoiu1kFc9ALUyQJGpLyyo7ci5k58tQwo).
- Outcome: Liquidity drained: pool 9002 now holds 0.00 NEAR, 0.0% of the 77.00 NEAR put in at launch (rule: below 20%).
- Outcome: Price now ≈ 67% of launch price (pool now 0.00 NEAR for 11394296393753976564876299867 tokens; at launch amemedog.near put in 77.00 NEAR for 900000000000000000000000000000000 tokens); collapse line is 10%.
- Outcome: Insider exit: amemedog.near peaked at 100% of supply and sold 100% of it to the DEX in 2 transfer(s) within 9.8 h of launch (first tx 8vTbLvbhaRfsCoiu1kFc9ALUyQJGpLyyo7ci5k58tQwo).
- Score: Liquidity was added by the wallet amemedog.near, which holds the pool shares and can withdraw them at any time.
- Score: The deployer amemedog.near and 0 wallet(s) it seeded before launch held 12% at the cut-off.

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

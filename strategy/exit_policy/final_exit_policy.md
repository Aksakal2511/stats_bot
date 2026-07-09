# Final Exit Policy

Research-only exit policy notes for the policy-cube BTC 5-minute strategy.

The active bot default is now `hold_to_resolution`. Positions are held to market resolution unless the separate operational emergency guard fires.

The previous tactical exit strategy, `btc_state_policy_plus_bps`, remains available with:

```bash
python3 run_bot.py --paper --exit-policy-mode bps_loss_only
```

When that mode is enabled, the table below supplies the side-symmetric loss-only threshold. UP and DOWN trades use the same threshold.

## Threshold Table

Session order is chronological by London hour bucket: `00-02`, `03-06`, `07-11`, `12-13`, `14-16`, `17-20`, `21-23`.

| Day Type | Session Bucket | Exit BPS Threshold |
|---|---|---:|
| weekday | ASIA_LOW_LIQUIDITY | 2.0 |
| weekday | ASIA_ACTIVE | 2.0 |
| weekday | EUROPE_OPEN | 2.0 |
| weekday | US_PREOPEN | 2.0 |
| weekday | US_OPEN_EXPANSION | 2.0 |
| weekday | US_LATE_SESSION | 2.0 |
| weekday | US_AFTERHOURS | 2.0 |
| weekend | ASIA_LOW_LIQUIDITY | 2.0 |
| weekend | ASIA_ACTIVE | 2.0 |
| weekend | EUROPE_OPEN | 2.0 |
| weekend | US_PREOPEN | 2.0 |
| weekend | US_OPEN_EXPANSION | 2.0 |
| weekend | US_LATE_SESSION | 2.0 |
| weekend | US_AFTERHOURS | 2.0 |

## Simulation Assumptions

- Entry source: `policy_cube_dual_side_trade_values_offset300.jsonl`
- Exit family: `btc_state`
- Active exit strategy: `hold_to_resolution`
- Optional exit strategy: `btc_state_policy_plus_bps`
- Optional threshold mode: uniform `2.0` BPS loss-only paper test.
- Entry mode: first qualifying policy episode second.
- Window start block: do not enter during the first 20 seconds of a 5-minute window.
- Flat rule: trade only when flat.
- Re-entry cooldown: 10 seconds after strategy-specific exit.
- Side behavior: symmetric, no UP/DOWN-specific threshold adaptation.
- Profit exits are disabled.
- Tactical BPS exits are disabled in the default profile.
- In `bps_loss_only` mode, the bot exits early only on the loss-side BPS trigger plus loss of policy support.

The machine-readable version of this policy is `final_bps_thresholds.csv`.

## Exact Exit Trigger

For each open trade, the threshold is fixed from the entry trade's `entry_day_type + entry_session_bucket`.

In optional `bps_loss_only` mode, the strategy exits only when both conditions are true:

```text
policy_no_longer_supportive
AND
signed_bps_materially_against
```

`policy_no_longer_supportive` means the current live policy-cube lookup no longer gives the held side executable side-specific value at the entry edge threshold fixed when the trade was opened. A missing current policy match also counts as not supportive.

For an UP trade:

```text
signed_bps_materially_against =
    current cb_signed_bps_raw <= entry cb_signed_bps_raw - threshold
```

For a DOWN trade:

```text
signed_bps_materially_against =
    current cb_signed_bps_raw >= entry cb_signed_bps_raw + threshold
```

When both conditions are true, exit immediately at the held side's current bid. If the trigger never occurs before market end, hold to settlement.

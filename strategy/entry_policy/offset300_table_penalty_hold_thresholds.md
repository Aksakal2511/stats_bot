# Previous Offset300 Table-Penalty Hold Entry Thresholds

This is the previous offset300 table-penalty profile retained for comparison.

The active default profile is now:

```text
strategy/entry_policy/offset300_hold_value_session_thresholds.csv
```

It pairs with:

- `strategy/policy_cube/policy_cube_dual_side_trade_values_offset300.jsonl`
- `strategy/bucket_config/volatility_bucket_edges_offset300.json`

The profile was selected from:

```text
/Volumes/Kate_DATA/policy_finder/data/artifacts/policy_cube_offset_bundle_table_penalty_diagnostics
```

Runtime behavior:

- Entry uses the offset300 dual-side policy cube.
- UP and DOWN edges are still calculated side-by-side as `trade_value - live ask`.
- The selected side must clear the enabled day/session threshold.
- Disabled sessions are explicit blockers and will show as `ENTRY_SESSION_DISABLED`.
- Tactical BPS exits are disabled by default; positions hold to resolution unless the operational emergency guard fires.

## Threshold Table

| Day Type | Session Bucket | Enabled | Entry Edge Threshold | Source Penalty | Source Current Threshold | Notes |
|---|---|---:|---:|---:|---:|---|
| weekday | ASIA_LOW_LIQUIDITY | yes | 0.260 | +0.100 | 0.160 | selected |
| weekday | ASIA_ACTIVE | no | 0.375 | +0.175 | 0.200 | disabled, failed guardrails |
| weekday | EUROPE_OPEN | no | 0.370 | +0.150 | 0.220 | disabled, failed guardrails |
| weekday | US_PREOPEN | yes | 0.200 | +0.000 | 0.200 | selected |
| weekday | US_OPEN_EXPANSION | yes | 0.160 | -0.050 | 0.210 | selected |
| weekday | US_LATE_SESSION | yes | 0.300 | +0.050 | 0.250 | selected |
| weekday | US_AFTERHOURS | no | 0.375 | +0.125 | 0.250 | disabled, failed guardrails |
| weekend | ASIA_LOW_LIQUIDITY | yes | 0.205 | +0.075 | 0.130 | selected |
| weekend | ASIA_ACTIVE | yes | 0.195 | +0.075 | 0.120 | selected by guardrail |
| weekend | EUROPE_OPEN | no | 0.280 | +0.150 | 0.130 | disabled, negative best raw |
| weekend | US_PREOPEN | yes | 0.170 | +0.050 | 0.120 | selected |
| weekend | US_OPEN_EXPANSION | yes | 0.320 | +0.200 | 0.120 | selected |
| weekend | US_LATE_SESSION | yes | 0.320 | +0.200 | 0.120 | selected |
| weekend | US_AFTERHOURS | no | 0.120 | +0.000 | 0.120 | disabled, failed guardrails |

## Research Readout

Reconstructed strategy summary from the offset bundle diagnostics:

| Bundle | Trades | Trades/Day | Total Net | Positive Day Rate | Worst Day | p05 Day |
|---|---:|---:|---:|---:|---:|---:|
| offset200 | 12,037 | 102.9 | +$2,053.56 | 63.2% | -$123.69 | -$64.01 |
| offset300 | 9,422 | 80.5 | +$2,365.57 | 66.7% | -$132.41 | -$63.99 |

The default uses offset300 because it was the stronger survivor under maker-entry plus taker-exit fees and gave the cleaner net result after the 1-second as-of timing audit.

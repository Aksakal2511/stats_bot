# Offset300 Hold-Value Session Thresholds

This is the active default entry profile for `stats_bot`.

It pairs with:

- `strategy/policy_cube/policy_cube_dual_side_hold_values_offset300.jsonl`
- `strategy/bucket_config/volatility_bucket_edges_offset300.json`

The profile was selected from:

```text
/Volumes/Kate_DATA/policy_finder/data/artifacts/policy_cube_offset_bundle_hold_value_uniform_threshold_refinement
```

Runtime behavior:

- Entry value is hold-to-resolution value: `avg_side_entry_ask + side_settlement_edge`.
- Live edge is `hold_value - live side ask`.
- UP and DOWN are evaluated independently; the larger executable edge wins.
- A side is executable only when its settlement-supported value flag is true and live edge clears the day/session threshold.
- Entry is maker/post-only at `best_ask - 1 tick`.
- The active entry window is second `45` through second `240` of the 5-minute market.
- Normal exit mode is `hold_to_resolution`; BPS tactical exits are disabled unless explicitly requested.

## Threshold Table

| Day Type | Session Bucket | Enabled | Entry Edge Threshold |
|---|---|---:|---:|
| weekday | ASIA_LOW_LIQUIDITY | yes | 0.09 |
| weekday | ASIA_ACTIVE | yes | 0.12 |
| weekday | EUROPE_OPEN | yes | 0.11 |
| weekday | US_PREOPEN | yes | 0.12 |
| weekday | US_OPEN_EXPANSION | yes | 0.07 |
| weekday | US_LATE_SESSION | yes | 0.09 |
| weekday | US_AFTERHOURS | yes | 0.07 |
| weekend | ASIA_LOW_LIQUIDITY | yes | 0.14 |
| weekend | ASIA_ACTIVE | yes | 0.08 |
| weekend | EUROPE_OPEN | yes | 0.08 |
| weekend | US_PREOPEN | yes | 0.09 |
| weekend | US_OPEN_EXPANSION | yes | 0.15 |
| weekend | US_LATE_SESSION | yes | 0.06 |
| weekend | US_AFTERHOURS | yes | 0.16 |

## Research Readout

The final table was reconstructed from the 11-spec uniform threshold sweep by selecting the best threshold per day/session bucket using combined maker-entry PnL across offset200 and offset300, then evaluating the assembled offset300 strategy.

| Execution | Trades | Trades/Day | Total Net | Mean/Trade | Positive Day Rate | Worst Day | p05 Day | Max Drawdown |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| maker entry + settlement | 22,748 | 194.4 | +$11,418.73 | +$0.50 | 87.2% | -$198.42 | -$85.40 | -$198.42 |
| taker entry + settlement | 22,748 | 194.4 | +$3,973.19 | +$0.17 | 65.0% | -$250.86 | -$146.67 | -$346.53 |

June 1-10 survived under maker entry but was weaker than the pre-June period. DOWN trades carried June; UP trades were near flat before fees and strongly negative after taker fees. Monitor side-level realised PnL before considering live deployment.

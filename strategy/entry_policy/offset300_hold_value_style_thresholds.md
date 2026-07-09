# Offset300 Hold-Value Style Thresholds

This is the active default entry profile for `stats_bot`.

It uses:

- `strategy/policy_cube/policy_cube_dual_side_hold_values_offset300.jsonl`
- `strategy/bucket_config/volatility_bucket_edges_offset300.json`
- entry window second `45` through `240`
- hold-to-resolution exits

Entry edge is:

```text
hold_value - live_side_ask
```

Threshold lookup is:

```text
day_type + session_bucket + selected_trade_style
```

If a style row is missing, the bot falls back to the blank `trade_style` row for that day/session. Neutral near-zero rows are explicitly disabled.

## Active Threshold Matrix

| Day Type | Session Bucket | Continuation | Mean Reversion |
|---|---|---:|---:|
| weekday | ASIA_LOW_LIQUIDITY | 0.15 | 0.15 |
| weekday | ASIA_ACTIVE | 0.14 | 0.14 |
| weekday | EUROPE_OPEN | 0.09 | 0.10 |
| weekday | US_PREOPEN | 0.07 | 0.12 |
| weekday | US_OPEN_EXPANSION | 0.08 | 0.11 |
| weekday | US_LATE_SESSION | 0.08 | 0.09 |
| weekday | US_AFTERHOURS | 0.10 | 0.10 |
| weekend | ASIA_LOW_LIQUIDITY | 0.07 | 0.13 |
| weekend | ASIA_ACTIVE | 0.08 | 0.09 |
| weekend | EUROPE_OPEN | 0.06 | 0.13 |
| weekend | US_PREOPEN | 0.08 | 0.09 |
| weekend | US_OPEN_EXPANSION | 0.15 | 0.15 |
| weekend | US_LATE_SESSION | 0.08 | 0.14 |
| weekend | US_AFTERHOURS | 0.09 | 0.16 |

## Notes

- The table enforces `mean_reversion >= continuation`.
- The previous day/session-only table remains available at `offset300_hold_value_session_thresholds.csv`.
- Blank `trade_style` rows in the CSV are fallback values only.
- `neutral` rows are disabled to avoid near-zero BPS trades being treated as continuation or mean reversion.

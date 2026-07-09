# Offset250 10-Bucket Hold-Value Style Thresholds

Active `stats_bot` entry edge profile for the offset250 10-bucket hold-to-settlement strategy.

Active artifacts:

- `strategy/policy_cube/policy_cube_dual_side_hold_values_offset250_10bucket.jsonl`
- `strategy/bucket_config/volatility_bucket_edges_offset250_10bucket.json`
- `strategy/entry_policy/offset250_10bucket_hold_value_style_thresholds.csv`
- `strategy/exit_policy/offset250_10bucket_hold_exit_thresholds.csv`

Entry edge is `hold_value - live_side_ask`. Threshold lookup is `day_type + session_bucket + selected_trade_style`.
Neutral rows are disabled.

Backtest overlay below uses live minimum-size staking: 5 contracts per filled maker entry.

| Day Type | Session Bucket | Continuation | Mean Reversion | Trades | Maker Net | Taker Net | PDR | Worst Day | p05 Day |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| weekday | ASIA_CORE_HANDOVER | 0.10 | 0.10 | 2,769 | $449.65 | $103.44 | 67.1% | $-34.30 | $-15.90 |
| weekday | ASIA_DEEP_LOW | 0.09 | 0.12 | 1,446 | $429.20 | $248.98 | 70.2% | $-17.80 | $-9.69 |
| weekday | ASIA_PRE_ACTIVE | 0.10 | 0.12 | 1,331 | $333.30 | $168.23 | 61.4% | $-17.55 | $-10.86 |
| weekday | EUROPE_MIDDAY | 0.10 | 0.14 | 1,343 | $399.50 | $228.76 | 69.9% | $-26.95 | $-9.26 |
| weekday | EUROPE_OPEN | 0.12 | 0.12 | 1,285 | $358.30 | $199.59 | 68.7% | $-15.85 | $-11.45 |
| weekday | US_AFTERHOURS | 0.07 | 0.12 | 2,807 | $522.85 | $176.76 | 70.7% | $-26.75 | $-13.14 |
| weekday | US_LATE_CORE | 0.12 | 0.10 | 1,304 | $329.30 | $168.25 | 78.3% | $-12.50 | $-10.39 |
| weekday | US_OPEN_EARLY | 0.09 | 0.08 | 1,724 | $589.75 | $372.95 | 80.7% | $-12.10 | $-4.60 |
| weekday | US_OPEN_LATE | 0.16 | 0.12 | 1,023 | $372.30 | $243.84 | 74.7% | $-11.85 | $-6.76 |
| weekday | US_PREOPEN | 0.16 | 0.14 | 788 | $328.15 | $231.05 | 68.7% | $-13.80 | $-8.69 |
| weekend | ASIA_CORE_HANDOVER | 0.09 | 0.10 | 1,336 | $296.40 | $124.99 | 67.6% | $-30.00 | $-12.58 |
| weekend | ASIA_DEEP_LOW | 0.16 | 0.08 | 530 | $180.45 | $112.84 | 69.8% | $-11.70 | $-6.14 |
| weekend | ASIA_PRE_ACTIVE | 0.10 | 0.14 | 512 | $211.80 | $146.86 | 79.4% | $-15.15 | $-12.14 |
| weekend | EUROPE_MIDDAY | 0.14 | 0.07 | 492 | $213.25 | $150.03 | 88.2% | $-7.60 | $-3.59 |
| weekend | EUROPE_OPEN | 0.06 | 0.06 | 664 | $110.45 | $25.15 | 64.7% | $-17.70 | $-14.29 |
| weekend | US_AFTERHOURS | 0.10 | 0.16 | 1,080 | $373.60 | $237.51 | 88.2% | $-18.30 | $-10.40 |
| weekend | US_LATE_CORE | 0.14 | 0.14 | 455 | $147.25 | $89.75 | 64.7% | $-11.05 | $-8.15 |
| weekend | US_OPEN_EARLY | 0.14 | 0.10 | 501 | $176.30 | $114.01 | 67.6% | $-12.15 | $-9.69 |
| weekend | US_OPEN_LATE | 0.12 | 0.08 | 610 | $174.75 | $97.21 | 76.5% | $-19.00 | $-13.71 |
| weekend | US_PREOPEN | 0.07 | 0.08 | 527 | $174.95 | $108.13 | 67.6% | $-15.50 | $-8.01 |

Research source:

`/Volumes/Kate_DATA/policy_finder/data/artifacts/policy_cube_statsbot_10_bucket_hold_value_style_pair_refinement`

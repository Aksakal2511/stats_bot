# Offset250 10-Bucket Coinbase-Source Entry Thresholds

Active live threshold table generated from the Coinbase-source offset250 10-bucket hold-value style-pair simulation.

- Source policy cube: `offset250_10bucket_cb_sources`
- BTC signed BPS source in cube build: `cb`
- Volatility source in cube build and bucket edges: `cb`
- Value mode: `hold_value_settlement_supported`
- Entry window: `45-240`
- Neutral trade style: disabled

| Day Type   | Session Bucket     |   Continuation |   Mean Reversion |
|:-----------|:-------------------|---------------:|-----------------:|
| weekday    | ASIA_CORE_HANDOVER |           0.1  |             0.09 |
| weekday    | ASIA_DEEP_LOW      |           0.12 |             0.12 |
| weekday    | ASIA_PRE_ACTIVE    |           0.07 |             0.09 |
| weekday    | EUROPE_MIDDAY      |           0.08 |             0.1  |
| weekday    | EUROPE_OPEN        |           0.09 |             0.12 |
| weekday    | US_AFTERHOURS      |           0.12 |             0.08 |
| weekday    | US_LATE_CORE       |           0.09 |             0.1  |
| weekday    | US_OPEN_EARLY      |           0.07 |             0.1  |
| weekday    | US_OPEN_LATE       |           0.07 |             0.08 |
| weekday    | US_PREOPEN         |           0.12 |             0.14 |
| weekend    | ASIA_CORE_HANDOVER |           0.12 |             0.12 |
| weekend    | ASIA_DEEP_LOW      |           0.12 |             0.12 |
| weekend    | ASIA_PRE_ACTIVE    |           0.07 |             0.1  |
| weekend    | EUROPE_MIDDAY      |           0.16 |             0.08 |
| weekend    | EUROPE_OPEN        |           0.07 |             0.12 |
| weekend    | US_AFTERHOURS      |           0.12 |             0.1  |
| weekend    | US_LATE_CORE       |           0.08 |             0.08 |
| weekend    | US_OPEN_EARLY      |           0.12 |             0.1  |
| weekend    | US_OPEN_LATE       |           0.1  |             0.1  |
| weekend    | US_PREOPEN         |           0.09 |             0.16 |

Research artifact:

```text
/Volumes/Kate_DATA/policy_finder/data/artifacts/policy_cube_statsbot_10_bucket_hold_value_style_pair_refinement_offset250_cb_sources
```

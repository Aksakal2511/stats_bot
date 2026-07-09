# z6_local_v1

`policy_bundle.json` is the active production artifact trained on corrected offset250 observations from `2026-02-14` through `2026-06-27`. Coinbase supplies the causal BTC signal and completed-minute 60-minute realised volatility; resolved Polymarket/Gamma outcomes supply settlement labels.

The final regularised policy level uses weekday/weekend, one of four six-hour London segments, TTE, move-z bucket and selected-side price bucket. Production edge thresholds are `0.05` for continuation and mean reversion.

```text
00:00-05:59  ASIA
06:00-11:59  EUROPE
12:00-17:59  US_OPEN
18:00-23:59  US_LATE
```

Source research artifact:

```text
/Volumes/Kate_DATA/policy_finder/data/artifacts/stats_bot_z6_local_v1_polymarket_labeled
```

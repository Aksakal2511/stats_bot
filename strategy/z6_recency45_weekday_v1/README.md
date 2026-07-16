# z6_recency45_weekday_v1

This is a legacy schema-v2 research artifact. The active production strategy is
`strategy/z6_recency120_weekday_v1`.

Weekdays use recursively weighted policy values with a 45-day half-life and parent strength 10 at base, day, TTE and final levels. Weekends retain the previous `z6_local_v1` feature configuration and lifetime tables unchanged. Continuation and mean-reversion trades use the same branch selection.

Coinbase supplies the causal BTC signal and completed-minute 60-minute realised volatility. Resolved Polymarket/Gamma outcomes supply settlement labels. Prices remain `0.20-0.80`. The legacy profile used continuation `0.04` edge with `60-239` seconds TTE and mean reversion `0.04` edge with `60-255` seconds TTE.

Legacy profile:

| Day | Session | Style | Edge | TTE |
|---|---|---|---:|---:|
| All | All | Continuation | 0.04 | 60-239 |
| All | All | Mean reversion | 0.04 | 60-255 |

Source artifact:

```text
/Volumes/Kate_DATA/policy_finder/data/artifacts/stats_bot_z6_recency45_weekday_v1_clean_ffm_to_20260713
```

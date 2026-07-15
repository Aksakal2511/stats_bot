# z6_recency45_weekday_v1

`policy_bundle.json` is the active schema-v2 production artifact trained on clean/FFM-omitted offset250 observations from `2026-02-14` through `2026-07-13`.

Weekdays use recursively weighted policy values with a 45-day half-life and parent strength 10 at base, day, TTE and final levels. Weekends retain the previous `z6_local_v1` feature configuration and lifetime tables unchanged. Continuation and mean-reversion trades use the same branch selection.

Coinbase supplies the causal BTC signal and completed-minute 60-minute realised volatility. Resolved Polymarket/Gamma outcomes supply settlement labels. Prices remain `0.20-0.80`. The shipped profile is continuation `0.04` edge with `60-239` seconds TTE and mean reversion `0.04` edge with `60-255` seconds TTE.

The active profile is:

| Day | Session | Style | Edge | TTE |
|---|---|---|---:|---:|
| All | All | Continuation | 0.04 | 60-239 |
| All | All | Mean reversion | 0.04 | 60-255 |

Source artifact:

```text
/Volumes/Kate_DATA/policy_finder/data/artifacts/stats_bot_z6_recency45_weekday_v1_clean_ffm_to_20260713
```

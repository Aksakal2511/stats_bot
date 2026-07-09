# z6_recency45_weekday_v1

`policy_bundle.json` is the active schema-v2 production artifact trained on corrected offset250 observations from `2026-02-14` through `2026-06-27`.

Weekdays use recursively weighted policy values with a 45-day half-life and parent strength 10 at base, day, TTE and final levels. Weekends retain the previous `z6_local_v1` feature configuration and lifetime tables unchanged. Continuation and mean-reversion trades use the same branch selection.

Coinbase supplies the causal BTC signal and completed-minute 60-minute realised volatility. Resolved Polymarket/Gamma outcomes supply settlement labels. Prices remain `0.20-0.80`. The default entry profile remains `0.05` edge and `60-255` seconds TTE, with London-aligned day/session/style overrides in `entry_profiles.json` derived from the three-fold execution-aware segmented replay.

The active overrides are:

| Day | Session | Style | Edge | TTE |
|---|---|---|---:|---:|
| Weekday | EUROPE | Mean reversion | 0.05 | 120-179 |
| Weekday | US_OPEN | Continuation | 0.04 | 90-239 |
| Weekend | EUROPE | Continuation | 0.04 | 60-255 |
| Weekend | EUROPE | Mean reversion | 0.03 | 60-255 |
| Weekend | US_OPEN | Mean reversion | 0.07 | 60-255 |

Source artifact:

```text
/Volumes/Kate_DATA/policy_finder/data/artifacts/stats_bot_z6_recency45_weekday_v1
```

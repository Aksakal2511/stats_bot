# z6_recency120_weekday_v1

`policy_bundle.json` is the active schema-v2 production artifact trained on clean/FFM-omitted offset250 observations from `2026-02-14` through `2026-07-13`.

Weekdays use recursively weighted policy values with a 120-day half-life and parent strength 10 at base, day, TTE and final levels. Weekends use the clean/FFM-omitted lifetime hierarchy. Coinbase supplies the causal BTC signal and completed-minute 60-minute realised volatility. Resolved Polymarket/Gamma outcomes supply settlement labels.

The active profile is:

| Day | Session | Style | Edge | TTE |
|---|---|---|---:|---:|
| All | All | Continuation | 0.05 | 60-255 |
| All | All | Mean reversion | 0.05 | 60-255 |

Source artifact:

```text
/Volumes/Kate_DATA/policy_finder/data/artifacts/stats_bot_z6_hl120_weekday_v1_clean_ffm_to_20260713
```

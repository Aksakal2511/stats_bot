# z6_fullraw_hl90_weekday_v1

`policy_bundle.json` is the active schema-v2 production artifact trained on full/raw offset250 Coinbase observations from `2026-02-14` through `2026-07-13`.

Weekdays use recursively weighted policy values with a 90-day half-life and parent strength 10 at base, day, TTE and final levels. Weekends use the lifetime hierarchy. Coinbase supplies the causal BTC signal and completed-minute 60-minute realised volatility. Resolved Polymarket/Gamma outcomes supply settlement labels.

The active profile is:

| Day | Session | Style | Edge | TTE |
|---|---|---|---:|---:|
| All | All | Continuation | 0.06 | 60-255 |
| All | All | Mean reversion | 0.10 | 60-255 |

Source artifacts:

```text
/Volumes/Kate_DATA/policy_finder/data/artifacts/policy_cube_regime_regularized_offset250_cb_full_raw_20260713/cache
/Volumes/Kate_DATA/policy_finder/data/artifacts/stats_bot_z6_fullraw_hl90_weekday_v1
```

The session/style drawdown guard is disabled by default for this runtime. The run-level peak-realised drawdown kill remains separate and can still be configured with `STATS_PEAK_DRAWDOWN_KILL_USD`.

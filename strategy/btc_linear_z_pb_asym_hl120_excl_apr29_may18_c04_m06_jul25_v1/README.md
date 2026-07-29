# btc_linear_z_pb_asym_hl120_excl_apr29_may18_dynamic_edges_jul25_v1

`strategy_manifest.json` points Stats Bot at a continuous move_dz x price
surface. This is not a policy cube; the live value is calculated from the fitted
model artifact at runtime.

| Feature | Value |
|---|---|
| Training | 2026-02-14 through 2026-07-25 |
| Excluded training ranges | 2026-04-29 through 2026-05-18 |
| Signal | side-aligned distance-to-beat z (`move_dz`) |
| Momentum | none |
| Price buckets | asymmetric |
| Model | btc_linear_z_price_bucket, C=0.05, asymmetric price buckets |
| Recency | HL120 |
| Day type | excluded |
| Side | pooled |
| Entry | conservative day-type/time dynamic edge table; C04/M06 fallback |
| TTE | 45-265s |
| Price range | 0.20-0.80 |
| Training rows | 11,388,613 |
| Unique markets | 39,987 |

Runtime keeps the model no-session; the London time band is used only to choose
the entry edge thresholds and to label the dashboard profile.

| Day type | London time | Continuation | Mean reversion |
|---|---:|---:|---:|
| weekday | 00:00-08:00 | 7c | 4c |
| weekday | 08:00-14:30 | 4c | 6c |
| weekday | 14:30-17:00 | 4c | 5c |
| weekday | 17:00-22:00 | 5c | 6c |
| weekday | 22:00-24:00 | 5c | 5c |
| weekend | 00:00-08:00 | 3c | 4c |
| weekend | 08:00-14:30 | 3c | 8c |
| weekend | 14:30-17:00 | 3c | 8c |
| weekend | 17:00-22:00 | 10c | 6c |
| weekend | 22:00-24:00 | 7c | 6c |

# btc_linear_z_pb_asym_hl120_excl_apr29_may18_c04_m06_jul25_v1

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
| Entry | continuation 4%, mean reversion 6% |
| TTE | 45-265s |
| Price range | 0.20-0.80 |
| Training rows | 11,388,613 |
| Unique markets | 39,987 |

Runtime keeps the macro session only as dashboard context.

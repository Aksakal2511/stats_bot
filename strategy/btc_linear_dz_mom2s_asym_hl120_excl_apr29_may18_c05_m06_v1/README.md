# btc_linear_dz_mom2s_asym_hl120_excl_apr29_may18_c05_m06_v1

`strategy_manifest.json` points Stats Bot at a continuous move_dz x price
surface. This is not a policy cube; the live value is calculated from the fitted
model artifact at runtime.

| Feature | Value |
|---|---|
| Training | 2026-02-14 through 2026-06-12 |
| Excluded training ranges | 2026-04-29 through 2026-05-18 |
| Signal | side-aligned distance-to-beat z (`move_dz`) |
| Momentum | side_dz_delta_2s |
| Price buckets | asymmetric |
| Model | btc_linear_z_momentum_price_bucket, C=0.03, asymmetric price buckets, side_dz_delta_2s |
| Recency | HL120 |
| Day type | excluded |
| Side | pooled |
| Entry | continuation 5%, mean reversion 6% |
| TTE | 45-265s |
| Price range | 0.20-0.80 |
| Training rows | 7,115,882 |
| Unique markets | 27,556 |

Runtime keeps the macro session only as dashboard context.

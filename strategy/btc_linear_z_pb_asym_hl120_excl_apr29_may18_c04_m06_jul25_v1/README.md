# btc_linear_z_pb_asym_hl120_excl_apr29_may18_direct_child118_support50_c04_m06_floor_dynamic_edges_jul25_v1

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
| Entry | direct child118 support>=50 dynamic edge selector with C04/M06 floor and contradiction/day-time fallbacks |
| TTE | 45-265s |
| Price range | 0.20-0.80 |
| Training rows | 11,388,613 |
| Unique markets | 39,987 |

Runtime keeps the value model no-session. Required entry edges come from
`edge_selector_child118_support50.json` was regenerated from the large fixed-model
replay using `direct_lookup`, `C>=4c`, and `MR>=6c`:

1. `day_group_time_contradiction_vol_q4` when selected-edge support is at least 50.
2. `day_group_time_contradiction` fallback.
3. `day_group_time` fallback.

The older price-bucket and timed profiles remain configured only as rollback
fallbacks if the dynamic selector cannot resolve a segment.

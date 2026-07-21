# surface_spline_b5x5_p1_hl120_no_session_c01_m01_v1

`strategy_manifest.json` points Stats Bot at a continuous move_dz x price spline
surface. This is not a policy cube; the live value is calculated from the fitted
model artifact at runtime.

| Feature | Value |
|---|---|
| Training | 2026-02-14 through 2026-07-13 |
| Signal | side-aligned distance-to-beat z (`move_dz`) |
| Model | surface_spline b5x5, penalty 1 |
| Recency | HL120 |
| Day type | excluded |
| Side | pooled |
| Entry | continuation 1%, mean reversion 1% |
| TTE | 45-265s |
| Price range | 0.20-0.80 |
| Training rows | 10,809,491 |
| Unique markets | 41,913 |

Runtime keeps the macro session only as dashboard context.

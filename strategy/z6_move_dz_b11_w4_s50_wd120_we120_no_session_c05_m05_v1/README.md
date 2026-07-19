# z6_move_dz_b11_w4_s50_wd120_we120_no_session_c05_m05_v1

`policy_bundle.json` is the active move-dz/no-TTE candidate trained on full/raw
offset250 observations from `2026-02-14` through `2026-07-13`.

Profile:

| Feature | Value |
|---|---|
| Signal | side-aligned distance-to-beat z (`move_dz`) |
| Calibration | 4h London buckets, b11 |
| Hierarchy | no-session: base -> day_type |
| Recency | weekday HL120, weekend HL120 |
| Strength | S50 |
| Entry | continuation 5% edge, mean reversion 5% edge |
| TTE | 45-265s |

The runtime still stores the policy bucket key as `move_z_bucket` for schema
compatibility, but the value inside that bucket is `move_dz`.

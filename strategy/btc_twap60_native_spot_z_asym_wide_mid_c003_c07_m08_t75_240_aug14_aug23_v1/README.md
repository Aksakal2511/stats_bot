# btc_twap60_native_spot_z_asym_wide_mid_c003_c07_m08_t75_240_aug14_aug23_v1

TWAP-native BTC 5-minute value bundle.

- Model: `twap_native_spot_z_price_bucket`
- Feature variant: `spot_z`
- Feature columns: `selected_raw_move_dz`
- Price buckets: `asymmetric_wide_mid`
- Training data: TWAP-native Telonex/Polymarket + Coinbase event dataset, 2026-08-14..2026-08-23
- Exclusions: `pm_dead_zone_window = true`, outside TTE 75-240s, outside price 0.20-0.80
- Edges: base C 7%, MR 8%; high-vol continuation C 6% when `local_vol_1h >= 61.85814361523389` bps
- Training rows: 3,478,735
- Unique markets: 2,652
- Bucket rows: {'p20_30': 574557, 'p30_45': 856856, 'p45_55': 547467, 'p55_70': 860259, 'p70_80': 639596}

Internal trade-style names remain `continuation` and `mean_reversion` for stats_bot compatibility.
The dashboard maps them to `C` and `MR`.

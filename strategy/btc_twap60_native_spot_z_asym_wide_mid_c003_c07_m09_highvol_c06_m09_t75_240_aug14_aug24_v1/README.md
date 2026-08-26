# btc_twap60_native_spot_z_asym_wide_mid_c003_c07_m09_highvol_c06_m09_t75_240_aug14_aug24_v1

TWAP-native BTC 5-minute value bundle.

- Model: `twap_native_spot_z_price_bucket`
- Feature variant: `spot_z`
- Feature columns: `selected_raw_move_dz`
- Price buckets: `asymmetric_wide_mid`
- Training data: TWAP-native Telonex/Polymarket + Coinbase event dataset, 2026-08-14..2026-08-24
- Exclusions: `pm_dead_zone_window = true`, outside TTE 75-240s, outside price 0.20-0.80
- Edges: base C 7%, MR 9%; high-vol continuation C 6% when local_vol_1h >= 63.226 bps
- Training rows: 4,043,021
- Unique markets: 2,913
- Bucket rows: {'p20_30': 667115, 'p30_45': 997798, 'p45_55': 634676, 'p55_70': 999603, 'p70_80': 743829}

Internal trade-style names remain `continuation` and `mean_reversion` for stats_bot compatibility.
The dashboard maps them to `C` and `MR`.

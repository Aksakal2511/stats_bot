# btc_twap60_native_spot_z_asym_wide_mid_c003_c07_m09_threezone_c09_c07_c05_m09_t75_240_aug14_aug26_v1

TWAP-native BTC 5-minute value bundle.

- Model: `twap_native_spot_z_price_bucket`
- Feature variant: `spot_z`
- Feature columns: `selected_raw_move_dz`
- Price buckets: `asymmetric_wide_mid`
- Training data: TWAP-native Telonex/Polymarket + Coinbase event dataset, 2026-08-14..2026-08-26
- Exclusions: `pm_dead_zone_window = true`, outside TTE 75-240s, outside price 0.20-0.80
- Edges: local-vol three-zone selector
  - `local_vol_1h <= 28.2723 bps`: C09/M09
  - `28.2723 < local_vol_1h < 62.5378 bps`: C07/M09
  - `local_vol_1h >= 62.5378 bps`: C05/M09
- Training rows: 4,887,816
- Unique markets: 3,392
- Bucket rows: {'p20_30': 796083, 'p30_45': 1210243, 'p45_55': 777392, 'p55_70': 1215420, 'p70_80': 888678}

Internal trade-style names remain `continuation` and `mean_reversion` for stats_bot compatibility.
The dashboard maps them to `C` and `MR`.

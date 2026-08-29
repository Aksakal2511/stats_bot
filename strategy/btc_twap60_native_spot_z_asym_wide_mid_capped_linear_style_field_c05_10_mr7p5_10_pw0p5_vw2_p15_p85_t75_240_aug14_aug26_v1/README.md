# btc_twap60_native_spot_z_asym_wide_mid_capped_linear_style_field_c05_10_mr7p5_10_pw0p5_vw2_p15_p85_t75_240_aug14_aug26_v1

TWAP-native BTC 5-minute value bundle.

- Model: `twap_native_spot_z_price_bucket`
- Feature variant: `spot_z`
- Feature columns: `selected_raw_move_dz`
- Price buckets: `asymmetric_wide_mid`
- Training data: TWAP-native Telonex/Polymarket + Coinbase event dataset, 2026-08-14..2026-08-26
- Exclusions: `pm_dead_zone_window = true`, outside TTE 75-240s, outside price 0.20-0.80
- Model fit type: `capped_linear`
- Edges: style-aware field
- Training rows: 4,887,816
- Unique markets: 3,392
- Bucket rows: {'p20_30': 796083, 'p30_45': 1210243, 'p45_55': 777392, 'p55_70': 1215420, 'p70_80': 888678}

Internal trade-style names remain `continuation` and `mean_reversion` for stats_bot compatibility.
The dashboard maps them to `C` and `MR`.

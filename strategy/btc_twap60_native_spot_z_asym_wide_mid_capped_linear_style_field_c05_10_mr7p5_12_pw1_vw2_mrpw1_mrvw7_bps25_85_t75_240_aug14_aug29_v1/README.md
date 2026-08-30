# btc_twap60_native_spot_z_asym_wide_mid_capped_linear_style_field_c05_10_mr7p5_12_pw1_vw2_mrpw1_mrvw7_bps25_85_t75_240_aug14_aug29_v1

- Model: `twap_native_spot_z_price_bucket`
- Fit type: `capped_linear`
- Feature variant: `spot_z`
- Price buckets: `asymmetric_wide_mid`
- Training data: TWAP60-native Aug14-Aug29 side rows
- Entry TTE: `75-240s`
- Edge selector: style-aware price/vol field
- C field: `5c -> 10c`, price:vol `1:2`
- MR field: `7.5c -> 12c`, `price_flip_vol_inverse`, price:vol `1:7`
- Vol anchors: `25/85 bps` fixed absolute local-vol anchors
- Guards: off

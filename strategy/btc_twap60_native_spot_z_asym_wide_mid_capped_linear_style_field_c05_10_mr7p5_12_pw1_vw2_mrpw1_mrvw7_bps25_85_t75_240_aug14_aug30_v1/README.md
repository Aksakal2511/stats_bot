# TWAP60 Native Spot-Z Style Field Strategy

Trained on TWAP-native Aug14-Aug30 side rows. Uses capped-linear spot_z / asymmetric_wide_mid price buckets.

Live signal mode: Coinbase spot now versus Coinbase W60 open anchor.

Edge field: C 5-10c, MR 7.5-12c, C price:vol 1:2, MR price:vol 1:7, fixed BPS25/BPS85.

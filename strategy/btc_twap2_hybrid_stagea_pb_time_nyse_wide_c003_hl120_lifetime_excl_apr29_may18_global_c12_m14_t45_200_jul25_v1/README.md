# btc_twap2_hybrid_stagea_pb_time_nyse_wide_c003_hl120_lifetime_excl_apr29_may18_global_c12_m14_t45_200_jul25_v1

Live TWAP-2 bundle for the BTC 5-minute TWAP settlement era.

- Model: hybrid Stage A price-bucket linear model (`stagea_pb_time`)
- Signal: Coinbase 30-sample discrete 1s ffilled TWAP move_dz
- Weekday branch: weekday-only, HL120
- Weekend branch: day-type excluded, LIFETIME
- Price buckets: asymmetric, train/entry price range 0.20-0.80
- Edges: fixed global continuation 12%, mean reversion 14%
- Entry TTE: 45-200s
- Training TTE: 45-265s
- Training: 2026-02-14 to 2026-07-25, excluding 2026-04-29:2026-05-18
- Training rows: weekday 7,886,115, weekend branch 11,388,613
- Unique markets: 39,987

Runtime keeps raw Coinbase spot in logs as a diagnostic, but the live model value
uses the TWAP move_dz fields.

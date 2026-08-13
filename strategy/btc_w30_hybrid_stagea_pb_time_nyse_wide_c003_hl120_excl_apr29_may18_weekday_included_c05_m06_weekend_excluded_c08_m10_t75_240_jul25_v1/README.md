# btc_w30_hybrid_stagea_pb_time_nyse_wide_c003_hl120_excl_apr29_may18_weekday_included_c05_m06_weekend_excluded_c08_m10_t75_240_jul25_v1

Live TWAP-2 bundle for the BTC 5-minute TWAP settlement era.

- Model: hybrid Stage A price-bucket linear model (`stagea_pb_time`)
- Signal: Coinbase analytic W30 pre-settlement move_dz
- Settlement label: `cb_twap_UP_outcome`
- Weekday branch: weekday-only, HL120
- Weekend branch: day-type excluded, 120
- Price buckets: asymmetric, train/entry price range 0.20-0.80
- Weekday edges: continuation 5%, mean reversion 6%
- Weekend edges: continuation 8%, mean reversion 10%
- Entry TTE: 75-240s
- Training TTE: 75-240s
- Training: 2026-02-14 to 2026-07-25, excluding 2026-04-29:2026-05-18
- Training rows: weekday 6,005,076, weekend branch 8,678,275
- Unique markets: 39,078

Runtime keeps raw Coinbase spot in logs as a diagnostic, but the live model value
uses the analytic W30 move_dz fields.

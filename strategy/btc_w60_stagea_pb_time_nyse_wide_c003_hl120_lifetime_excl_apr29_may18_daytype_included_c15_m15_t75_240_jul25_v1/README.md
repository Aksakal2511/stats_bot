# btc_w60_stagea_pb_time_nyse_wide_c003_hl120_lifetime_excl_apr29_may18_daytype_included_c15_m15_t75_240_jul25_v1

Live W60 analytic TWAP bundle for the BTC 5-minute TWAP settlement era.

- Model: Stage A price-bucket linear model (`stagea_pb_time`)
- Signal: Coinbase spot minus Coinbase W60-TWAP-open analytic move_dz
- Settlement label: `cb_twap_UP_outcome`
- Weekday branch: weekday-only, HL120
- Weekend branch: day-type included, LIFETIME
- Price buckets: asymmetric, train/entry price range 0.20-0.80
- Default edges: continuation 15%, mean reversion 15%
- Weekend override edges: continuation 15%, mean reversion 15%
- Entry TTE: 75-240s
- Training TTE: 75-240s
- Training: 2026-02-14 to 2026-07-25, excluding 2026-04-29:2026-05-18
- Training rows: weekday 6,005,076, weekend branch 2,673,199
- Unique markets: 39,078

Runtime keeps raw Coinbase spot in logs as a diagnostic, but the live model value
uses the analytic W60 move_dz fields.

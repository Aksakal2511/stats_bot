# btc_hybrid_stagea_pb_time_nyse_wide_c003_hl120_lifetime_excl_apr29_may18_global_c04_m06_jul25_v1

Live bundle for the middle-ground hybrid global-edge candidate.

- Model: hybrid Stage A price-bucket linear surface (`stagea_pb_time`)
- Stage A time scheme: `nyse_wide` (`00-08`, `08-14:30`, `14:30-18`, `18-24` London)
- Weekday branch: day-type included, HL120 recency
- Weekend branch: day-type excluded, LIFETIME recency
- Price buckets: asymmetric, train/entry price range 0.20-0.80
- Edges: fixed global continuation 4c, mean reversion 6c
- TTE: 45-265 seconds
- Training: 2026-02-14 to 2026-07-25, excluding 2026-04-29 to 2026-05-18

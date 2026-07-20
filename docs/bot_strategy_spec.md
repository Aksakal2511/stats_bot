# Bot Strategy Specification

## Production Model

The active model is `z6_move_dz_b11_w4_s50_wd120_we120_no_session_c05_m07_v1`, trained on full/raw offset250 observations from `2026-02-14` through `2026-07-13`.

```text
strategy/z6_move_dz_b11_w4_s50_wd120_we120_no_session_c05_m07_v1/policy_bundle.json
strategy/z6_move_dz_b11_w4_s50_wd120_we120_no_session_c05_m07_v1/entry_profiles.json
```

Coinbase BTC-USD supplies the causal BTC close and local volatility inputs. Policy settlement values are fitted against resolved Polymarket/Gamma outcomes.

The shipped hierarchy is intentionally small:

1. `move_z_bucket + selected_price_bucket`
2. `day_type + move_z_bucket + selected_price_bucket`

`move_z_bucket` remains the stored schema key for compatibility, but the value being bucketed is the side-aligned distance-to-beat z-score, `move_dz`. Lookup falls back to the deepest available parent.

Weekday and weekend branches both use the `HL120/S50` recursive hierarchy. Bucket calibration uses 4h London segments with 11 move-dz buckets; the policy table itself does not include session or TTE.

## Causal Signal

Coinbase minute volatility is calculated from completed minutes only:

```text
rv_1h_bps = sqrt(sum(last 60 one-minute log returns squared)) * 10,000
minimum valid history = 45 returns
```

For each side:

```text
distance_to_beat_bps = 10,000 * (btc_close_1s - price_to_beat) / price_to_beat
remaining_vol_bps    = rv_1h_bps * sqrt(TTE / 3600)
move_dz(UP)          =  distance_to_beat_bps / max(remaining_vol_bps, 0.05)
move_dz(DOWN)        = -distance_to_beat_bps / max(remaining_vol_bps, 0.05)
```

Positive aligned distance is `continuation`; negative aligned distance is `mean_reversion`.

## Context

`day_type` uses the London calendar: Monday-Friday is `weekday`, otherwise `weekend`.

| London time | Display | Calibration label |
|---|---|---|
| 00:00-03:59 | `ASIA_00_04` | `h00_04` |
| 04:00-07:59 | `ASIA_04_08` | `h04_08` |
| 08:00-11:59 | `EUROPE_08_12` | `h08_12` |
| 12:00-15:59 | `US_OPEN_12_16` | `h12_16` |
| 16:00-19:59 | `US_OPEN_16_20` | `h16_20` |
| 20:00-23:59 | `US_LATE_20_00` | `h20_00` |

## Entry Decision

Both UP and DOWN are evaluated independently. The shipped profile is continuation `0.05 / 45-265` and mean reversion `0.07 / 45-265`.

A side is eligible when:

```text
elapsed >= 45 seconds
45 <= TTE <= 265
0.20 <= current ask <= 0.80
bid and ask are valid and ask > bid
policy value - current ask >= style profile edge
policy value - min(current ask + 0.01, 0.80) >= style profile edge
BPS_YES_GUARD is inactive for the current second/window
session/style drawdown guard is inactive by default
run peak-realised drawdown kill is inactive
```

If both sides qualify, choose larger edge, then larger value, then UP as a deterministic tie-break. The bot permits only one filled trade per five-minute market.

## Defensive Guard

The BPS/YES contradiction guard remains implemented but is disabled for the current live strategy (`STATS_BPS_YES_GUARD_ENABLED=false`). When re-enabled, it compares Coinbase signed BPS to the Polymarket UP/YES mid and blocks incoherent windows through `TTE <= 270s`: `cb_bps > +0.05` with UP mid below `0.45`, or `cb_bps < -0.05` with UP mid above `0.55`.

The FFM/pressure signed-volume hedge guard is enabled in live `.env` for this deployment. It watches Binance spot taker-signed flow while a position is open, arms in the last 60 seconds, and buys one opposite-side hedge when the held side is trading at 0.75-1.00 and the rolling 40 second adverse net flow reaches 5 BTC. The hedge uses the opposite outcome, full held share size, and a GTC buy limit of 0.99. A submitted, filled, failed, or already-attempted pressure hedge prevents any second hedge order for that position.

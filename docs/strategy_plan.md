# Strategy Plan

## Live Hypothesis

The frozen `btc_linear_dz_mom2s_asym_hl120_excl_apr29_may18_c05_m06_v1` model uses side-aligned distance-to-beat z (`move_dz`) plus 2-second side-aligned move-dz momentum inside asymmetric Polymarket price buckets. Day type and side are pooled out of the fitted value model; macro session remains dashboard context only.

## Frozen Shape

```text
Training:      2026-02-14 through 2026-06-12, excluding 2026-04-29 through 2026-05-18
Model:         btc_linear_z_momentum_price_bucket, C=0.03
Recency:       HL120
Price buckets: asymmetric selected-side ask buckets from 0.20 to 0.80
Momentum:      side_dz_delta_2s
Day type:      excluded
Side:          pooled
Default edges: continuation=0.05, mean_reversion=0.06
Price:         0.20-0.80
Default TTE:   continuation=45-265, mean_reversion=45-265
Stake:         flat five-share fallback
Execution:     existing pre-signed fast limit and maker fallback
Exit:          hold to resolution
Value:         continuous F(move_dz, side_dz_delta_2s | selected_side_ask_bucket)
```

## Evaluation Discipline

Everything after `2026-07-13` is untouched OOS evidence. Review at fixed intervals rather than responding to one bad day. Measure realised PnL, PnL/trade, weekly consistency, maximum drawdown, continuation/MR contribution, eligible-to-filled rate, actual slippage and results after an additional one-price-cent adverse-fill stress.

Do not add dynamic staking until the live strategy and execution-calibrated paper shadow agree over a meaningful sample.

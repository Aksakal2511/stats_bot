# Strategy Plan

## Live Hypothesis

The frozen `z6_move_dz_b11_w4_s50_wd120_we120_no_session_c05_m05_v1` model uses side-aligned distance-to-beat z (`move_dz`) with 4h London calibration buckets. The live lookup removes session and TTE from the policy key, using base and day-type levels only. Weekdays and weekends both use recursive HL120/S50.

## Frozen Shape

```text
Training:      2026-02-14 through 2026-07-13, full/raw offset250 observations
Calibration:   4h London move-dz buckets, b11
Day type:      weekday/weekend
Default edges: continuation=0.05, mean_reversion=0.05
Price:         0.20-0.80
Default TTE:   continuation=45-265, mean_reversion=45-265
Stake:         flat five-share fallback
Execution:     existing pre-signed fast limit and maker fallback
Exit:          hold to resolution
Value:         recursive recency, half-life=120 days, strength=50
```

## Evaluation Discipline

Everything after `2026-07-13` is untouched OOS evidence. Review at fixed intervals rather than responding to one bad day. Measure realised PnL, PnL/trade, weekly consistency, maximum drawdown, continuation/MR contribution, eligible-to-filled rate, actual slippage and results after an additional one-price-cent adverse-fill stress.

Do not add dynamic staking until the live strategy and execution-calibrated paper shadow agree over a meaningful sample.

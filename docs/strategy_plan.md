# Strategy Plan

## Live Hypothesis

The frozen `z6_fullraw_hl90_weekday_v1` model uses four regularised six-hour London segments with causal one-hour local volatility. Weekdays use a recursive 90-day half-life with strength 10 at every hierarchy level; weekends retain the full/raw lifetime hierarchy. Both trade styles use the same branch for their day type.

## Frozen Shape

```text
Training:      2026-02-14 through 2026-07-13, full/raw offset250 Coinbase observations
Sessions:      ASIA 00-06, EUROPE 06-12, US_OPEN 12-18, US_LATE 18-24 London
Day type:      weekday/weekend
Default edges: continuation=0.06, mean_reversion=0.10
Price:         0.20-0.80
Default TTE:   continuation=60-255, mean_reversion=60-255
Stake:         flat five-share fallback
Execution:     existing pre-signed fast limit and maker fallback
Exit:          hold to resolution
Weekday value: 90-day recursive recency, strength=10
Weekend value: full/raw lifetime hierarchy
```

## Evaluation Discipline

Everything after `2026-07-13` is untouched OOS evidence. Review at fixed intervals rather than responding to one bad day. Measure realised PnL, PnL/trade, weekly consistency, maximum drawdown, continuation/MR contribution, eligible-to-filled rate, actual slippage and results after an additional one-price-cent adverse-fill stress.

Do not add dynamic staking until the live strategy and execution-calibrated paper shadow agree over a meaningful sample.

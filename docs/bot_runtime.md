# Bot Runtime

## Runtime Shape

Paper and live call the same `MacroLocalPolicy.evaluate` function. Only order submission and fill confirmation differ.

At startup the bot:

1. Loads `strategy/btc_linear_dz_mom2s_asym_hl120_excl_apr29_may18_c05_m06_v1/strategy_manifest.json` and `entry_profiles.json`.
2. Verifies runtime price, global TTE, start gate and stake settings match the frozen model.
3. Starts the Coinbase websocket.
4. Backfills approximately 100 completed Coinbase one-minute candles.
5. Blocks entries until causal one-hour local volatility is ready.
6. In `event_driven_ws` mode, starts public Polymarket book streams for both outcomes and the authenticated user stream in live mode.

Run modes:

```bash
python3 run_bot.py --paper
python3 run_bot.py --live
python3 run_bot.py --live --execution-mode legacy_polling
```

Both require `STATS_STAKE_MODE=min_order_size` with `STATS_MIN_ORDER_SIZE_FALLBACK=5`. Paper should use `STATS_PAPER_FILL_MODEL=fast_limit` for live-mechanics comparison.

## Dashboard

The compact strategy fields are:

```text
strategy          : btc_linear_dz_mom2s_asym_hl120_excl_apr29_may18_c05_m06_v1 (frozen 2026-06-12)
day/session       : weekday/US_OPEN
entry_profile     : weekday/US_OPEN | C=5c@45-265s | MR=6c@45-265s
local_vol_1h      : 34.812 bps (ready)
selected_move_dz  : -0.731
move_dz_bucket    : continuous
dz_mom_2s         : -0.084
selected_price    : ask 0.5000 | bid 0.4900 | bucket p48_52
value / edge      : 0.612 / 0.112 (need 0.060)
trade_style       : mean_reversion
support / level   : 7115890 / surface
execution health  : HEALTHY
submission state  : NONE
submission        : 28 / 31 fills/attempts
```

Support is the surface model training-row count. The blocker line appears only when required. Feature failures are named `LOCAL_VOL_WARMUP`, `LOCAL_VOL_STALE`, `BTC_CLOSE_MISSING`, `PRICE_TO_BEAT_MISSING`, or the standard quote/TTE/edge blockers.

## Logs

Every run writes under `logs/<session>_<paper|live>/`:

- `stats_cadence_1s.csv`: includes macro session, local RV status/age, selected move-dz, price bucket, policy support, unique markets and hierarchy level.
- `stats_orders.csv`: placements, fills, cancellations and failures.
- `stats_trades.csv`: closed positions and realised PnL.
- `stats_execution_latency.csv`: source-event, decision, submission/response, fill and cancellation timing with quote and edge context.

`event_driven_ws` is the default. `legacy_polling` is the rollback mode. The dashboard shows the selected mode but keeps detailed timing out of the main display.

In event-driven mode, a persistent worker owns an entry-only HTTP/2 connection. Wallet refreshes are briefly deferred while an entry POST is unresolved, while market data, signals, positions and the dashboard continue updating. Responses over one second are labelled `LATE`; duplicate protection remains active and the bot does not retry blindly.

The session/style drawdown guard is disabled by default for this profile. `STATS_SESSION_STYLE_DRAWDOWN_GUARD_ENABLED=1` restores the fixed two-hour London bucket guard. `STATS_PEAK_DRAWDOWN_KILL_USD=15` separately blocks new entries for the rest of the run once `peak_realized_pnl - realized_pnl` reaches `$15`. Cadence and latency logs include the peak, drawdown and latched state for live/paper comparison.

## Validation

```bash
python3 -m pytest -q
python3 run_bot.py --paper --once --status-interval-seconds 1
```

The unit suite covers London session boundaries, right-closed z buckets, completed-minute volatility, clean warmup blocking, streamed-book updates and single-flight entry submission. Research/runtime row parity is checked against the offset250 production model before deployment.

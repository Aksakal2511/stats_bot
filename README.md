# Stats Bot

Live and paper runtime for the Polymarket BTC five-minute macro/local value strategy.

## Active Strategy

`z6_recency45_weekday_v1` is frozen from offset250 data covering `2026-02-14` through `2026-06-27`.

Coinbase provides the causal BTC signal. The bundled policy values are trained against resolved Polymarket/Gamma settlement outcomes.

```text
Policy hierarchy: move-z + selected price
                  -> weekday/weekend
                  -> TTE
                  -> four fixed 6h London segments
Signal:           side-aligned BTC move / causal Coinbase 1h local volatility
Edge:             0.05 continuation and 0.05 mean reversion
Entry price:      0.20-0.80
Default TTE:      60-255 seconds (London day/session/style overrides are active)
Stake:            flat market-minimum shares, 5-share fallback
Position limit:   one filled trade per market
Exit:             hold to resolution
Weekdays:         recursive 45-day half-life, strength 10 at every level
Weekends:         original lifetime hierarchy (50/100/80/80)
```

The default `event_driven_ws` execution mode keeps streamed UP/DOWN books in memory, evaluates policy on Coinbase or Polymarket events, and sends the existing pre-signed `GTC` fast-limit order through a persistent dedicated submission worker and isolated HTTP connection. The dashboard and public feeds continue updating during submission. The limit remains at `ask + 1 tick` and must preserve the required edge. A single-flight reservation prevents concurrent submissions for the same market. Authenticated user events accelerate fill reconciliation; REST remains the fallback.

Use `--execution-mode legacy_polling` (or `STATS_EXECUTION_MODE=legacy_polling`) to restore the previous REST-polling execution path without changing strategy settings. Each session also writes `stats_execution_latency.csv` with source-event, decision, submission, response, fill and cancellation timing.

The session risk guard latches off new entries after a `$15` decline from peak realised session equity. It does not mistake pending collateral or open-position cost for drawdown, and it does not force-sell an existing position. Configure it with `STATS_PEAK_DRAWDOWN_KILL_USD`.

`BPS_YES_GUARD` remains as the execution-integrity guard used by the simulation. Recovery, exact-cell pauses, high-volatility muting, raw BTC-momentum blocking, dynamic staking, and session-specific edge tables are not part of the active entry path.

## Key Files

- `strategy/z6_recency45_weekday_v1/policy_bundle.json`: active weekday-recency/weekend-lifetime bundle.
- `strategy/z6_recency45_weekday_v1/entry_profiles.json`: active London day/session/style edge and TTE overrides.
- `stats_bot/macro_policy.py`: production policy loader, move-z feature and entry evaluator.
- `stats_bot/coinbase.py`: second feed plus completed-minute history used for causal local volatility.
- `stats_bot/bot.py`: shared paper/live runtime, execution and dashboard.
- `docs/bot_strategy_spec.md`: exact strategy rules.
- `docs/bot_runtime.md`: operation, dashboard and logs.
- `docs/execution_plan.md`: retained fast-limit and maker mechanics.

The older `strategy/policy_cube`, `strategy/entry_policy`, and volatility-edge artifacts are retained for historical analysis and optional legacy exit support. They no longer select active entries.

## Run

```bash
cd /Volumes/Kate_DATA/stats_bot
python3 -m pip install -r requirements.txt
python3 run_bot.py --paper
python3 run_bot.py --live
```

The runtime refuses to start if environment overrides differ from the frozen `0.20-0.80`, `60-255`, 45-second start gate or flat five-share fallback.

Tests:

```bash
python3 -m pytest -q
```

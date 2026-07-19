# Execution Plan

The strategic layer is `z6_move_dz_b11_w4_s50_wd120_we120_no_session_c05_m05_v1`. Execution can run as `event_driven_ws` (default) or `legacy_polling` (rollback).

## Entry Path

1. Maintain in-memory UP and DOWN books from the public Polymarket market WebSocket.
2. Re-evaluate UP and DOWN when either the BTC signal or Polymarket book changes.
3. Require edge of at least the active style profile, currently `0.05` for continuation and `0.05` for mean reversion, at the displayed ask and at the execution-health-adjusted fast limit.
4. Atomically reserve the market entry before handing the pre-signed order to the submission worker.
5. Submit through the persistent entry-only worker and isolated keepalive HTTP connection at no more than the execution-health fast limit when edge remains valid.
6. Use the authenticated user WebSocket to trigger prompt fill reconciliation.
7. If the fast path is unavailable, use the existing post-only maker quote at `best_ask - 1 tick`.
8. Cancel pending entry immediately when side, policy key, price range or edge is no longer valid.

Paper `fast_limit` uses the same side, limit and edge-preservation checks as live. The bot permits one filled trade per five-minute market. Its single-flight state is reserved before the worker starts, so repeated market or BTC events cannot submit a second order while the first response is outstanding. Retry cooldown, wallet reconciliation, stale quote protection and market-minimum-size checks remain in force.

An unresolved submission is labelled late after one second, but it is never retried until its state is known. This prevents a delayed GTC and a fallback order from both filling. Private wallet reads are deferred during the POST; public feeds and dashboard rendering continue.

The session/style drawdown guard is disabled by default for this profile. If re-enabled with `STATS_SESSION_STYLE_DRAWDOWN_GUARD_ENABLED=1`, it blocks only the affected style after the configured realised-PnL drawdown threshold. The run-level peak-drawdown kill switch latches after the configured decline from peak realised PnL, cancels pending entries and blocks new entries while allowing an existing position to settle normally.

## Position And Exit

Stake is the market minimum order size with a five-share fallback. Dynamic staking is disabled for this deployment.

Positions are held to Polymarket resolution. Tactical taker exits and guard-triggered sells remain disabled under `hold_to_resolution` because prior exit testing found their fees damaging.

## Guard

`BPS_YES_GUARD` remains entry-blocking because the research simulation included it. Recovery, cell damage, high-volatility and raw momentum guards are not part of entry selection.

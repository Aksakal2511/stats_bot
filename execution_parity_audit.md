# Execution Parity Audit

This is the pre-edit audit of the existing `stats_bot` order and position
lifecycle. The migration must preserve these mechanics unless a mismatch is
explicitly resolved.

## Actual current entry semantics

The user's belief that every live entry is maker-only is not accurate for the
default fast path.

1. The bot pre-signs BUY limit orders across price/size levels.
2. On a viable signal it normally selects a fast-limit price around the latest
   ask (subject to the existing tolerance, edge-preservation and surplus rules).
3. `PolymarketClient.post_signed_buy_order` submits that signed order as GTC
   **without** `post_only=True`. It may immediately match and is not guaranteed
   to be maker.
4. If a presigned order is unavailable, the fallback
   `PolymarketClient.place_maker_buy` posts GTC with `post_only=True` at the
   maker quote.
5. Paper `fast_limit` is intended to mirror the pre-signed live path.

No order semantics are changed by this migration without a separate decision.

## KEEP: canonical runtime execution lifecycle

- Event-driven WebSocket execution wakeups and asynchronous submission worker.
- Existing limit construction and tick rounding.
- Pre-signed order ladder and on-demand signing fallback setting.
- Post-only maker fallback and its rejection handling.
- Pre-submit latest-ask revalidation.
- Quote maximum age and book source handling.
- Pending order edge checks, 1.5s no-longer-viable latch, stale-ask distance and
  grace handling, 3s resting maximum age, cancellation and retry.
- Live retry cooldown, maximum attempts, unresolved/ambiguous submission state,
  order polling and wallet reconciliation.
- One filled entry per market/window.
- Partial fill handling and dust rules.
- `ladder_shares` target-size selection, exchange minimum-size enforcement and
  wallet-based constraints.
- Settlement polling and hold-to-resolution accounting.

## Disable strategy exits without changing the engine

- Configure and assert `hold_to_resolution`.
- Do not call move-dz stop-loss or pressure-defence paths for the final strategy.
- Do not enable BPS-loss tactical exits or guard-forced exits.
- Keep the underlying sell/API code intact for recovery and historical tests;
  it must be unreachable from the frozen strategy.

## Execution blockers retained

- Missing/invalid market or token metadata.
- Missing, stale or invalid books/quotes.
- Invalid price/tick/minimum-order constraints.
- No private client, signing/posting failures and ambiguous-order recovery.
- Existing pending position/order and one-fill/window lifecycle.
- Insufficient wallet or invalid ladder target.
- Retry cooldown and maximum submission attempts.
- Pre-submit ask moved outside allowed execution slack.
- Pending order no longer preserves the model edge at its resting price.

## Strategy blockers removed

- Old macro-policy support and bucket blockers.
- Mean-reversion and all legacy signal/volatility/momentum guards.
- Session/style and peak-drawdown strategy pauses.
- Old recovery/momentum regime restrictions.
- Tactical stop-loss and pressure hedge triggers.

## Critical parity differences to expose

| Difference | Current runtime | Frozen research | Treatment |
| --- | --- | --- | --- |
| Fast entry order | GTC, not post-only | Event-driven simulated limit fill | Preserve runtime; disclose actual mode. |
| Execution-health edge | old base edge +1c degraded / +2c poor | fixed C2.5 candidate threshold | Threshold bump removed; health telemetry retained. |
| Startup observation | old first-window observe-only default | absent | Disabled for frozen strategy parity. |
| Retry/max attempts | 8s cooldown / 3 attempts defaults | replay lifecycle differs | Preserve execution; report. |
| Ladder sizing | dynamic shares | research reports 5 shares | Required by migration spec; preserve and log intended/actual shares. |

## P15 interaction with execution

- The detector consumes the frozen candidate/opportunity stream independent of
  whether an order is submitted.
- A genuine NORMAL-to-BAD event starts one wall-clock 15-minute block on **new
  entry requests**.
- Existing pending orders and positions remain governed by the unchanged
  execution lifecycle; the pause does not cancel or sell them.
- Continuous BAD state does not extend or retrigger the pulse.
- Full Q30 plus 15-minute recovery is required to return detector state to
  NORMAL and re-arm a future transition.
- During a pulse, every otherwise viable fixed-C2.5 opportunity is journaled as
  a counterfactual with the intended ladder size and model state.
- Detector state, recovery progress and pulse end are persisted atomically so a
  restart cannot fabricate a transition or discard an active pause.

## Non-live acceptance gate

The final smoke test must run paper/once or a dedicated offline harness. It
must never initialize a live order submission. Tests must assert the final
strategy cannot reach legacy MR, guard, stop-loss, hedge or tactical-exit
paths, while the unchanged order-construction tests continue to pass.

## Post-migration resolution

- The execution-health `+1c/+2c` adjustment has been removed from the effective
  threshold. Health state is still logged; C remains exactly 2.50c.
- Startup observe-first behavior is disabled for the frozen strategy.
- Retry cooldown/max attempts, stale-quote controls, pending-order lifecycle,
  pre-submit revalidation, one-fill semantics and wallet checks remain intact.
- The primary pre-signed entry remains ordinary GTC rather than post-only. The
  fallback remains GTC post-only. This is the actual pre-migration behavior and
  was deliberately preserved rather than relabelled as maker-only.
- Ladder sizing remains dynamic by explicit instruction, while every decision
  and P15 counterfactual logs the intended ladder quantity.
- Hold-to-resolution prevents all strategy sell paths from being invoked. The
  lower-level sell/recovery API remains present but is not called by the frozen
  strategy.

There is no unresolved probability, side-selection, threshold or P15 semantic
mismatch. The retained live retry/order-routing details and dynamic sizing are
intentional production execution differences mandated by this migration.

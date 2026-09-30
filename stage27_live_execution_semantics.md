# Stage 27 Live Execution Semantics

This document freezes the execution interpretation used by the Stage 27
historical study. It is an audit record, not a production configuration change.

## Frozen strategy inputs

- Development dates: 2026-08-14 through 2026-09-14 only.
- Probability/state: the persisted Stage 23.5 development-state cache containing
  the frozen `TWAP_DZ / VOL_1H / smoothed_lam10000 / isotonic / BPS hierarchy`
  output.
- Entry family: continuation only. Mean reversion remains disabled.
- Required edge: the requested fixed C threshold.
- P15 guard: the frozen Stage 26 detector timeline and genuine `NORMAL -> BAD`
  transitions. New entries are paused for exactly 15 minutes; continuous BAD
  does not extend or retrigger the pulse.
- Price range: selected-side ask 0.20 through 0.80.
- TTE: 60 through 220 seconds.
- Sizing: 5 shares, matching the research replay. Production ladder sizing is
  not changed by Stage 27 and is outside this fixed-size comparison.
- One fill per Polymarket market/window; hold to resolution.

## E0 canonical replay

`E0` calls the tested Stage 26D replay path directly:

- `fixed_state`
- `build_decisions_from_state`
- `replay_candidate_with_audit`
- `ReplayConfig` with the Stage 23.5 `REPLAY_BASE`

At C2.5 it must reproduce the persisted Stage 26D `P15_PAUSE` trade identity,
entry limit, and gross PnL at every date and latency. The reference is 3,249
pooled fills, $551.50 pooled three-latency PnL, and $183.8333333333 total PnL
after averaging the three latencies within each date. Failure
of any strict identity or numeric gate writes a STOP marker and prevents E1 or
the threshold sweep from running.

## E1 live-like replay

E1 preserves the same strategy state, P15 stream, latency values, and causal
event rows, but applies the current `stats_bot` entry lifecycle:

1. Build the fast limit from the currently available selected-side ask.
2. Use one base tolerance tick. Spend up to two additional ticks only when the
   configured edge-surplus rules permit it.
3. At `signal_time + latency`, re-read the latest causal quote. Do not submit if
   ask is above `limit + 2 ticks`.
4. A submitted GTC order fills immediately as taker only when the executable ask
   is at or below its limit. Its fill price is that executable ask, not the
   submitted limit.
5. Otherwise the order rests for at most 3.0 seconds. A causal ask crossing the
   limit produces a maker fill at the limit; maker fee is zero.
6. While resting, preserve the 1.5-second weak-edge latch, 2-tick stale-distance
   test with 0.6-second grace, policy/P15 revalidation, and cancellation rules.
7. After cancellation, wait 2.0 seconds before retrying. Permit at most three
   submitted attempts per market/window. A pre-submit revalidation skip counts
   as an evaluated opportunity but not a submitted order, matching the live bot.
8. Stop after the first fill in the market/window and hold to resolution.

The production fast path is a non-post-only GTC order. A post-only maker path
exists in code, but the audited live `.env` disables that fallback; on-demand
signing is the configured fallback for an unavailable pre-signed order. The historical
state cache does not encode presigner availability, transport failures, wallet
reconciliation, partial fills, queue position, order-book depth, or exchange
acknowledgement status. E1 therefore models the normal available fast path and
does not fabricate signing/fallback or transport failures. Resting fills are causal
quote-cross proxies, not claims about historical queue priority.

## Fees

- Immediate taker fill: `round(shares * 0.07 * price * (1 - price), 5)`.
- Resting maker fill: zero.
- Gross PnL: `shares * (1 - price)` on a win, otherwise `-shares * price`.
- Net PnL: gross PnL minus entry fee.

The five-decimal rounding is applied per fill, matching the reconciliation seen
in the 2026-09-29 live audit.

## T80/T85/T90 approximation

These are explicitly non-policy approximations. They start from an E1 unit and
sample additional unfilled eligible markets without outcome, resolution, or PnL
in the selection key. Selection is deterministic for each fixed seed and
stratified by selected-side price, TTE, and model-edge bands. One causal candidate
per unfilled market is eligible. Added fills use that candidate's contemporaneous
executable ask and taker fee. Results are reported over 100 fixed seeds and may
not be interpreted as exact order-book execution.

## Known limitations

- Top-of-book rows do not establish maker queue position or available depth for
  five shares.
- Exchange transport and signing availability are not present historically.
- Historical quote crossings can overstate maker fills when queue depth was
  ahead of the order.
- E1 is the closest causal replay supported by the persisted event stream, not a
  reconstruction of private exchange matching state.

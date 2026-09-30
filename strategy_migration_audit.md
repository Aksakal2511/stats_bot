# Final BTC Strategy Migration Audit

This audit records the pre-edit state of `stats_bot` for the frozen BTC
five-minute strategy migration. The authoritative strategy specification is
`/Volumes/Kate_DATA/policy_finder/stats_bot_final_strategy_migration_spec.md`.

## Frozen target

- BTC five-minute markets only.
- State: `TWAP_DZ` with canonical 60 equally weighted one-second closing-TWAP
  samples and `VOL_1H` uncertainty.
- Base model: `smoothed_lam10000`, followed by development-only isotonic
  calibration.
- Residual hierarchy: `BPS15+BPS60`, else `BPS15+ACCEL_5_15`, else base.
- Entry: continuation only, selected-side price 0.20-0.80, TTE 60-220 seconds,
  fixed required model edge 0.025.
- Exit: hold to resolution. MR is absent.
- Only strategy guard: the frozen Stage 26 detector
  `MODEL_EDGE_MEDIAN_W120_M4_EN20_EX30_R15_NONE_CE1`, with a genuine
  NORMAL-to-BAD transition causing one exact 15-minute entry pause.
- Model development dates: 2026-08-14 through 2026-09-14. Sep15-Sep18 are
  regression data only.

## Provenance status before implementation

| Component | Status | Evidence / action |
| --- | --- | --- |
| Canonical TWAP geometry | KEEP / REUSE | `policy_finder/scripts/research/model3/twap_state.py`; 60 equal one-second as-of samples over `[target-60s,target)`. |
| `VOL_1H` | KEEP / REUSE | `stats_bot.coinbase.local_volatility_1h_bps` uses the same causal 60 one-minute log-return root-sum-square convention. Must be parity-tested. |
| `smoothed_lam10000` implementation | REPLACE old bundle / REUSE math | Research `SharedOrSmoothedBucketModel`; runtime has the mathematically corresponding `SmoothedSlopePriceBucketSurfaceModel`, but currently receives old `move_dz`, not `TWAP_DZ`. |
| Isotonic calibration | REPLACE | Current runtime has no final isotonic layer. Stage 23 persisted summaries but not deployable isotonic breakpoints. An exact deterministic export from the canonical Stage 23 fit is required. |
| BPS residual hierarchy | REPLACE | Current runtime does not implement the frozen hierarchy. Stage 23 persisted coefficients but not feature-scaler state. Scalers can be rebuilt exactly from the development BPS features; export must be regression-checked against frozen Stage 23 predictions. |
| Stage 23 final-fit provenance | VERIFY BEFORE TOUCHING | `stage23_locked_holdout.../cache/final_model_fit_manifest.json` proves Aug14-Sep14 training and frozen coefficients. No serialized base model/calibrator/scalers were returned. Runtime activation must fail closed until an exact export bundle exists and passes holdout regression. |
| Side selection | REPLACE | Existing macro policy compares old UP/DOWN surface values. Final runtime must calculate canonical UP/DOWN probabilities and select only continuation opportunities with fixed C2.5, without reconstructing rules from prose. |
| P15 detector thresholds | VERIFY BEFORE TOUCHING | Exact detector timeline/reference artifacts exist in Stage 26/26B. Frozen Q20/Q30 numeric thresholds and observation semantics must be exported, not recomputed online. |

## Runtime component classification

### KEEP

- `stats_bot/polymarket.py`: market discovery, book ingestion, signing, order
  posting, order polling, cancellation, wallet and settlement APIs.
- Coinbase and Polymarket feed plumbing and event-driven wakeups.
- Pending-order lifecycle, ambiguity recovery, fill reconciliation, stale-quote
  cancellation, resting-order age, pre-submit ask revalidation and one-fill
  lifecycle semantics.
- `ladder_shares` sizing, bankroll/high-water bookkeeping and wallet handling.
- Hold-to-resolution settlement path.
- Decision/cadence/order/trade/latency journal infrastructure.
- Genuine infrastructure health reporting and startup synchronization.

### REPLACE

- Old `MacroLocalPolicy` strategy evaluation as the active entry decision
  source. It is tied to the Sep11 `spot_z`/vol-momentum bundle.
- Old live feature-to-model path (`move_dz`/spot-z surface) with the canonical
  TWAP-native probability pipeline.
- Dynamic/price-bucket/timed entry thresholds with fixed C2.5.
- Old dashboard strategy sections with final model, branch and P15 state.
- Old strategy manifest default with a final, provenance-locked artifact.

### REMOVE / DISABLE

- Mean-reversion classification and entries, including the MR edge buffer.
- Dynamic C/MR selectors and timed entry overrides.
- Low-liquidity/no-signal, ultra-low-vol, TWAP-lag, raw-TWAP-alignment,
  cheap-low-vol and weak-momentum guards.
- Session/style drawdown edge penalties and strategy blocking.
- BPS-YES strategy guard.
- Move-dz stop-loss and pressure hedge/defence, because the frozen strategy is
  hold-to-resolution with no tactical sell or hedge.
- Legacy policy-cube tactical exits when hold-to-resolution is active.
- Peak-session drawdown kill as a strategy blocker. This is not part of the
  frozen research strategy; operator-level process shutdown remains external.
- Recovery/momentum regime entry blockers and old signal persistence if they
  modify the frozen candidate decision.

### VERIFY BEFORE TOUCHING

- Startup observe-first-window: operational safety behavior not present in
  research; resolved by disabling it for the frozen strategy.
- Live retry cooldown and max-attempt limits: execution lifecycle controls,
  retained but reported as production/research differences.
- Execution-health required-edge bump: the old code added 1c in degraded and
  2c in poor CLOB-ack health. It was removed from threshold calculation because
  it changed fixed C2.5; execution-health telemetry itself remains.
- Fast-limit versus post-only maker path: preserve actual order mechanics, but
  do not describe the active pre-signed path as maker.

## Every strategy-affecting path found

| Path | Effect | Classification |
| --- | --- | --- |
| `MacroLocalPolicy.evaluate` | side, value, style, base eligibility | REPLACE |
| Macro entry profiles/dynamic selectors/timed overrides | changes C | REMOVE |
| Runtime weekday/weekend bounds | blocks entry | REPLACE with 0.20-0.80 |
| Runtime TTE/start bounds | blocks entry | REPLACE with 60-220; remove separate 45s start gate |
| Mean-reversion blocker/buffer | blocks/changes C | REMOVE MR entirely |
| Six legacy signal guards | blocks entries | DISABLE |
| BPS-YES guard | blocks entries/legacy exits | DISABLE |
| Session/style drawdown guard | blocks or raises C | DISABLE |
| Peak drawdown kill | blocks all entries | DISABLE as strategy logic |
| Staking/regime recovery blockers | blocks entries | DISABLE; retain ladder sizing only |
| Signal persistence | delays entry | DISABLE for exact candidate timing |
| P15 pause | blocks only new entries for 15m | ADD, exact frozen semantics |
| Pending-order edge/latch checks | cancels or retains pending order | KEEP execution |
| Quote freshness/revalidation | blocks/cancels submission | KEEP execution |
| One-fill/window and retry lifecycle | blocks duplicate entry | KEEP execution |
| Move-dz stop-loss | sells position | DISABLE |
| Pressure defence | hedges/sells economic exposure | DISABLE |
| Tactical exit policy | sells position | DISABLE via hold-to-resolution |
| Ladder sizing | changes size | KEEP exactly |

## Pre-implementation migration gate

The strategy was not deployable until the final artifact contained exact base
coefficients, isotonic knots, both residual scaler states and coefficients,
and frozen detector Q20/Q30 values, with hashes and Aug14-Sep14 provenance.
Regression against the already-consumed Sep15-Sep18 frozen prediction fixture
must reproduce `p_raw`, `p0`, branch and `p_final` within documented numerical
tolerances. Missing provenance remains a hard startup failure, never a fallback.

## Implementation resolution

The migration gate is now satisfied by
`model_artifacts/btc5m_v1_aug14_sep14/final_strategy.json`. The deterministic
export records Aug14-Sep14-only training provenance, model coefficients,
isotonic knots, residual scalers/coefficients, detector thresholds and source
hashes. Sep15-Sep18 was used only as a frozen regression fixture, never for
fitting or selection.

The active entry path now calls only `FinalBtc5mStrategy.evaluate` followed by
the P15 pulse decision. Startup observe-first behavior is disabled. Execution
health remains visible operationally but no longer raises C above 2.50c.
Legacy MR, strategy guards, stop-loss and pressure-defence calls are absent
from the active run path. Runtime configuration hard-fails if a legacy strategy
control is enabled.

Regression maxima against the persisted Stage23 frozen holdout predictions are
`2.15e-13` for raw probability, `1.53e-9` for isotonic base probability and
`1.53e-9` for final probability, with zero BPS-branch mismatches. The small
floating-point differences are within the exported tolerances and come from
streaming sufficient-statistic summation and equivalent isotonic knot
interpolation.

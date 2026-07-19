# Stats Bot

Runtime and active strategy bundle for the Polymarket BTC five-minute Up/Down
bot.

The active production strategy is a macro/local value model that prices each
side from causal Coinbase BTC movement and local volatility, then trades
Polymarket only when the selected-side value clears the configured edge. Live
execution uses pre-signed GTC fast-limit buys and holds positions to resolution.

## Strategy Introduction

Stats Bot is a short-horizon value betting model for Polymarket BTC five-minute
Up/Down markets. It is not trying to predict BTC perfectly. It is trying to
answer a narrower question:

```text
Given where BTC has moved so far in this five-minute window,
given current local volatility,
given the selected-side price,
given time left,
given day/session context,

is this contract underpriced?
```

If the answer is yes, the bot buys it. If not, it waits.

Each Polymarket contract settles at:

```text
1.00 if correct
0.00 if wrong
```

So a price of `0.40` means the market is roughly implying a 40% chance. If the
policy model estimates the true settlement probability is `0.47`, the trade has
value:

```text
policy value = 0.47
market price = 0.40
value edge   = 0.07
```

The bot is trying to repeatedly buy contracts for less than their estimated
settlement value.

### Why BTC Movement Is Used

These markets are path-dependent inside each five-minute window. If BTC has
moved up since the window opened, UP is naturally favoured; if BTC has moved
down, DOWN is naturally favoured.

Raw movement is not enough though. A `+4 bps` move means something different in
calm conditions than it does in violent conditions:

```text
+4 bps in calm conditions     = meaningful move
+4 bps in volatile conditions = less meaningful move
```

The bot therefore converts movement into a volatility-adjusted score:

```text
move_z = side_aligned_bps / expected_move
```

For UP:

```text
side_aligned_bps = BTC move since window open
```

For DOWN:

```text
side_aligned_bps = -BTC move since window open
```

This lets UP and DOWN share the same evidence symmetrically.

### Continuation And Mean Reversion

If the selected side agrees with BTC movement, the trade is a continuation
trade:

```text
BTC up, buying UP     = continuation
BTC down, buying DOWN = continuation
```

If the selected side opposes BTC movement, the trade is a mean-reversion trade:

```text
BTC up, buying DOWN = mean reversion
BTC down, buying UP = mean reversion
```

The model does not blindly prefer either style. It asks whether this specific
state was historically underpriced after accounting for move strength, price,
time left and context.

### Why Price Matters

Price is central to the strategy. A 30c trade and a 70c trade are not equivalent
even if the directional signal looks similar.

```text
At 30c, the trade needs to win more than 30% of the time.
At 70c, the trade needs to win more than 70% of the time.
```

The policy model therefore includes selected-side price buckets. The question is
not simply "will UP or DOWN win?" It is:

```text
At this price, in this state, did this side historically settle better than the
market price implied?
```

### The Value Edge

The value edge is:

```text
edge = policy_value - executable_price
```

Example:

```text
policy_value  = 0.62
current ask   = 0.55
fast limit    = 0.56
required edge = 0.05
```

The bot checks both the displayed ask and the actual fast-limit price:

```text
0.62 - 0.55 = 0.07
0.62 - 0.56 = 0.06
```

The trade is viable because the edge still survives after paying `ask + 1 tick`.
If the edge disappears after the fast limit, the bot does not trade.

This matters because execution is not free. Even without taker fees, paying one
or two ticks worse is still a real cost.

### Why The Strategy Can Work

The strategy can work only if three things remain true:

```text
1. The policy value is better calibrated than the live market price.
2. The bot can execute close enough to the observed ask.
3. The edge threshold is large enough to absorb noise, latency and model error.
```

The historical research suggested that some combinations of move-z, price
bucket, TTE, day type, six-hour London session and trade style had positive
out-of-sample value. The active bot is therefore not just trading "BTC up" or
"BTC down"; it is trading historically mispriced states.

The main risk is calibration drift. If market behaviour changes, a policy value
of `0.60` may no longer represent a real 60% settlement probability. That is why
live performance, execution quality and periodic policy refreshes matter.

In short:

```text
The whole game is calibration plus execution.
```

## Current Production Setup

```text
Strategy ID       : z6_move_dz_b11_w4_s50_wd120_we120_no_session_c05_m07_v1
Training window   : 2026-02-14 through 2026-07-13
Signal source     : Coinbase BTC-USD move_dz
Settlement labels : resolved Polymarket/Gamma outcomes
Entry prices      : 0.20-0.80 selected-side ask
Global TTE        : 45-265 seconds
Start gate        : elapsed >= 45 seconds
Stake             : flat market-minimum shares, 5-share fallback
Exit              : hold to resolution
Execution         : event_driven_ws by default
Fast limit        : ask + 1/2/2 ticks for HEALTHY/DEGRADED/POOR, residual-edge guarded
Position limit    : one filled trade per five-minute market
```

The source of truth for the active model is:

```text
strategy/z6_move_dz_b11_w4_s50_wd120_we120_no_session_c05_m07_v1/policy_bundle.json
strategy/z6_move_dz_b11_w4_s50_wd120_we120_no_session_c05_m07_v1/entry_profiles.json
```

The runtime refuses to start if key entry-shape settings differ from the frozen
strategy bundle:

```text
weekday price     : 0.20-0.80
weekend price     : 0.20-0.80
TTE               : 45-265 seconds
start gate        : 45 seconds
stake mode        : min_order_size or ladder_shares
fallback shares   : 5
```

This is intentional. It prevents accidental live drift from the exact strategy
that was researched and deployed.

`ladder_shares` is an opt-in stake mode. It uses the live/paper wallet to choose
share size from a bankroll ladder, keeps the current tier until the wallet falls
through that tier's downgrade buffer, and only scales up after at least one
closed trade confirms the wallet is still above the higher tier.

## Strategy Summary

The bot evaluates UP and DOWN independently.

For each side:

```text
distance_bps         = 10,000 * (btc_close_1s - price_to_beat) / price_to_beat
remaining_vol_bps    = local_1h_rv_bps * sqrt(TTE / 3600)
move_dz(UP)          =  distance_bps / max(remaining_vol_bps, 0.05)
move_dz(DOWN)        = -distance_bps / max(remaining_vol_bps, 0.05)
trade_style          = continuation if side-aligned distance is positive, otherwise mean_reversion
selected_side_edge   = policy_value - selected_side_ask
```

The policy value comes from a regularised hierarchy in the active bundle:

```text
1. move_z_bucket + selected_price_bucket
2. day_type + move_z_bucket + selected_price_bucket
```

`move_z_bucket` is retained as the schema key, but it contains the b11
side-aligned move-dz bucket. UP and DOWN share evidence through side alignment.

### Day And Session Labels

`day_type` is based on the London calendar:

```text
weekday : Monday-Friday
weekend : Saturday-Sunday
```

The active calibration display names are fixed four-hour London segments:

| London time | Display | Bundle key |
|---|---|---|
| 00:00-03:59 | ASIA_00_04 | h00_04 |
| 04:00-07:59 | ASIA_04_08 | h04_08 |
| 08:00-11:59 | EUROPE_08_12 | h08_12 |
| 12:00-15:59 | US_OPEN_12_16 | h12_16 |
| 16:00-19:59 | US_OPEN_16_20 | h16_20 |
| 20:00-23:59 | US_LATE_20_00 | h20_00 |

### Recency And Regularisation

The active bundle uses recursive weighting with a 120-day half-life and parent
strength 50 for both weekdays and weekends.

This is why support values shown in the dashboard should be read as effective
support, not always literal historical trade count.

### Entry Profiles

The default profile is:

```text
continuation    : 5c edge, 45-265s TTE
mean reversion  : 7c edge, 45-265s TTE
```

Current day/session/style overrides are stored in
`strategy/z6_move_dz_b11_w4_s50_wd120_we120_no_session_c05_m07_v1/entry_profiles.json`
and documented in the strategy folder README.

As of this README, the active shipped profile is:

| Day | Session | Style | Edge | TTE |
|---|---|---|---:|---:|
| All | All | Continuation | 0.05 | 45-265 |
| All | All | Mean reversion | 0.07 | 45-265 |

## Active Entry Rules

A side can be entered only when all of the following are true:

```text
elapsed_seconds >= 45
profile_min_tte <= TTE <= profile_max_tte
0.20 <= selected-side ask <= 0.80
bid and ask are valid
ask > bid
local Coinbase 1h volatility is ready and fresh
policy value exists
policy value - ask >= required edge
policy value - min(ask + 0.01, 0.80) >= required edge
BPS_YES_GUARD is inactive
session/style 2h drawdown kill is inactive by default
peak-realised run drawdown kill is inactive
no active position
no pending entry
no in-flight entry submission
no filled trade already exists for that market
live retry/cooldown rules allow another attempt
```

If both UP and DOWN qualify, the bot chooses:

```text
1. larger edge
2. larger policy value
3. UP as deterministic tie-break
```

## Removed From Active Entry Logic

These mechanisms/files may still exist for research history or legacy support,
but they are not part of the active `z6_move_dz_b11_w4_s50_wd120_we120_no_session_c05_m07_v1` entry path:

- twelve two-hour policy session buckets
- raw signed-BPS policy buckets
- independent volatility bucket
- separate UP and DOWN ask dimensions
- legacy twelve-session edge table
- recovery mode
- exact-cell damage pause guards
- high-volatility muting
- raw BTC-momentum blocking
- dynamic staking
- anti-martingale staking bonus

The session/style drawdown guard is disabled by default for this profile. It can
be re-enabled with `STATS_SESSION_STYLE_DRAWDOWN_GUARD_ENABLED=1`, but the final
OOS selection left it off because it reduced the selected edge profile. The
run-level peak-realised drawdown kill remains an overnight capital stop when
configured. Neither guard force-sells open positions.

## Execution Model

Default execution mode:

```text
event_driven_ws
```

In this mode the bot:

1. Maintains streamed Polymarket UP/DOWN books in memory.
2. Listens to Coinbase BTC updates.
3. Evaluates policy on Coinbase or Polymarket events.
4. Pre-signs an UP/DOWN price ladder at the start of each market.
5. Submits the selected pre-signed GTC order through a dedicated entry worker.
6. Uses authenticated user WebSocket events to speed up fill detection.
7. Falls back to REST/wallet reconciliation when needed.

The fast order is a GTC buy at:

```text
limit_price = min(current_ask + 1 tick, 0.80)
```

The order is only sent if the edge still survives at that limit price. This is
important: the bot does not blindly pay extra ticks just because execution is
poor.

Rollback mode:

```bash
python3 run_bot.py --live --execution-mode legacy_polling
```

`legacy_polling` keeps the same strategy settings but reverts the runtime to the
older REST-polling execution path.

## Risk Controls

### One Trade Per Market

The bot is designed to avoid duplicate buys in the same five-minute market:

- only one active position at a time
- only one pending entry at a time
- only one in-flight submission at a time
- per-window fill count blocks further entries
- live retry/cooldown rules limit repeated attempts


### BPS_YES_GUARD

`BPS_YES_GUARD` remains as the execution-integrity guard used by the researched
strategy. It can block entries when the bot detects a conflict condition around
the current BTC/YES state.

## Repository Layout

```text
stats_bot/
  bot.py                 main runtime, dashboard, execution and logs
  config.py              CLI and environment configuration
  macro_policy.py        active policy-bundle loader/evaluator
  coinbase.py            Coinbase websocket and local-volatility history
  polymarket.py          Polymarket/Gamma client wrapper
  features.py            live feature construction
  guards.py              runtime guards
  models.py              runtime dataclasses
  strategy.py            legacy policy helpers and shared utilities

strategy/
  z6_move_dz_b11_w4_s50_wd120_we120_no_session_c05_m07_v1/
    policy_bundle.json   active production policy bundle
    entry_profiles.json  active edge/TTE overrides
    README.md            strategy artifact notes
  bucket_config/
    volatility_bucket_edges_offset250_12bucket_2h_cb_sources.json
                         compatibility volatility bucket edges for live logs

docs/
  bot_strategy_spec.md   detailed strategy rules
  bot_runtime.md         runtime/dashboard/log notes
  execution_plan.md      execution design notes
  strategy_plan.md       strategy research notes

tests/
  test_*.py              unit tests for strategy/runtime behaviour

logs/                    ignored live/paper session logs
analysis/                ignored generated analysis outputs
```

Ignored by Git:

```text
.env
logs/
analysis/
strategy/policy_cube/
__pycache__/
.pytest_cache/
*.pyc
.DS_Store
```

## Environment Setup

Install dependencies:

```bash
cd /Volumes/Kate_DATA/stats_bot
python3 -m pip install -r requirements.txt
```

Create your local `.env` from `.env.example`:

```bash
cp .env.example .env
```

Then fill in your private Polymarket credentials in `.env`:

```text
POLY_PRIVATE_KEY=
POLY_FUNDER=
POLY_SIGNATURE_TYPE=
POLY_API_KEY=
POLY_API_SECRET=
POLY_API_PASSPHRASE=
```

Do not commit `.env`. It is ignored by Git.

The important runtime environment values are:

```text
STATS_EXECUTION_MODE=event_driven_ws
STATS_STAKE_MODE=min_order_size
STATS_MIN_ORDER_SIZE_FALLBACK=5
STATS_MIN_ENTRY_PRICE=0.20
STATS_MAX_ENTRY_PRICE=0.80
STATS_WEEKEND_MIN_ENTRY_PRICE=0.20
STATS_WEEKEND_MAX_ENTRY_PRICE=0.80
STATS_ENTRY_START_BLOCK_SECONDS=45
STATS_ENTRY_MIN_TTE_SECONDS=45
STATS_ENTRY_MAX_TTE_SECONDS=265
STATS_EXIT_POLICY_MODE=hold_to_resolution
STATS_PAPER_FILL_MODEL=fast_limit
STATS_PEAK_DRAWDOWN_KILL_USD=15
```

To enable bankroll ladder sizing instead of flat 5-share sizing:

```text
STATS_STAKE_MODE=ladder_shares
```

The ladder starts at 5 shares below `$100`, reaches 32 shares at `$1,000`, and
uses wider downgrade buffers, for example `$1,000 -> 32 shares` does not
downgrade until the wallet falls below `$900`.

```text
Wallet tier   Shares   Downgrade below
$0            5        -
$100          6        $85
$150          7        $130
$200          9        $175
$250          11       $220
$300          13       $265
$400          17       $360
$500          20       $450
$750          26       $675
$1,000        32       $900
$1,500        45       $1,350
$2,000        57       $1,800
$2,500        70       $2,250
$3,000        82       $2,700
$4,000        107      $3,600
$5,000        132      $4,500
$6,000        157      $5,400
$7,000        182      $6,300
$7,500        195      $6,750
$8,000        207      $7,200
$9,000        232      $8,100
$10,000       257      $9,000
```

## Running The Bot

Paper mode:

```bash
cd /Volumes/Kate_DATA/stats_bot
python3 run_bot.py --paper
```

Live mode:

```bash
cd /Volumes/Kate_DATA/stats_bot
python3 run_bot.py --live
```

Live mode with explicit execution mode:

```bash
python3 run_bot.py --live --execution-mode event_driven_ws
```

Legacy polling rollback:

```bash
python3 run_bot.py --live --execution-mode legacy_polling
```

Smoke-test one loop:

```bash
python3 run_bot.py --paper --once --status-interval-seconds 1
```

Stop after a fixed period:

```bash
python3 run_bot.py --paper --max-loop-seconds 60
```

## Dashboard Fields

The dashboard is intentionally compact. Important lines:

```text
strategy          : active strategy ID and frozen training end date
stake_mode        : sizing mode
target_shares     : target share count
presign           : current pre-signed ladder status
submission        : filled windows / entry attempts
execution health  : client/CLOB/path latency summary
wallet            : live wallet balance
realized_pnl      : realised session PnL
wins/losses       : session W/L
day/session       : London day type and macro session
entry_profile     : C and MR edge/TTE profile for this context
local_vol_1h      : causal Coinbase local volatility
move_z UP/DOWN    : side-aligned z-score and bucket for both sides
value / edge      : selected policy value and edge
support / level   : support and hierarchy level used
blockers          : active reason entry is not viable
```

Common blockers:

```text
LOCAL_VOL_WARMUP
LOCAL_VOL_STALE
TTE_OUTSIDE_ENTRY_RANGE
PRICE_OUTSIDE_ENTRY_RANGE
POLICY_NO_SIDE_EDGE
BPS_YES_CONFLICT_CONDITION_MET
PEAK_DRAWDOWN_KILL_ACTIVE
SKIPPED_LIVE_ENTRY_RETRY_COOLDOWN
SKIPPED_LIVE_WINDOW_MAX_ATTEMPTS
```

## Logs

Each run creates:

```text
logs/YYYYMMDD_HHMMSS_session_<id>_<paper|live>/
```

Files:

```text
stats_cadence_1s.csv        one row per dashboard/cadence snapshot
stats_orders.csv            runtime events, order placements, failures, fills
stats_trades.csv            closed trades and realised PnL
stats_execution_latency.csv event, decision, submit, response, fill and cancel timing
```

`stats_execution_latency.csv` is the main file for execution analysis. It
contains source-event timing, quote context, submission timing, response timing,
wire/local overhead where available, HTTP metadata where available, and fill or
cancel context.

## Testing

Run all tests:

```bash
cd /Volumes/Kate_DATA/stats_bot
python3 -m pytest -q
```

Compile-check runtime files:

```bash
python3 -m py_compile run_bot.py stats_bot/*.py
```

The tests cover:

- macro policy loading/evaluation
- London session boundaries
- move-z bucket handling
- local-volatility warmup behaviour
- event-driven execution state
- single-flight entry submission
- strategy/runtime parity assumptions

## Git And Version Control

The GitHub remote is:

```text
https://github.com/Aksakal2511/stats_bot.git
```

Normal workflow:

```bash
cd /Volumes/Kate_DATA/stats_bot
git status
git add .
git commit -m "Describe what changed"
git push
```

Before every commit, check:

```bash
git status
```

You should not see:

```text
.env
logs/
analysis/
strategy/policy_cube/
```

If you do see any of those as staged files, stop and fix `.gitignore` before
pushing.

## Operational Notes

- Prefer running live-only when latency matters. Running research simulations,
  VS Code, Teams or heavy browser tabs at the same time can add local jitter.
- Keep the laptop awake while live trading.
- `event_driven_ws` should be the normal live mode.
- Do not edit `.env.example` with real credentials.
- Do not manually change entry price/TTE/stake shape unless you are deliberately
  testing a new researched bundle.
- When changing strategy logic, update this README and `docs/bot_strategy_spec.md`
  together.
- When changing execution logic, update this README and `docs/bot_runtime.md`
  together.

## Known Limitations

- Paper mode cannot perfectly reproduce live maker/GTC queue behaviour.
- Historical replay can estimate counterfactual fills, but cannot prove exactly
  what the CLOB would have done live.
- Current active entry depends only on Coinbase as the signal provider.
- Policy data is frozen through `2026-07-13`; refreshing it requires rebuilding
  research artifacts in `policy_finder` and exporting a new runtime bundle.
- The legacy generated `strategy/policy_cube/`, `strategy/entry_policy/` and
  `strategy/exit_policy/` artifacts are intentionally not committed to GitHub.
  The shipped default uses `hold_to_resolution` and does not need them.

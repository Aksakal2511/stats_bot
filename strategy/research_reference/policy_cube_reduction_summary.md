# Dual-Side Policy Cube Summary

This summary describes the policy cube currently installed for `stats_bot`.

## Bot-Facing Artifacts

- `strategy/policy_cube/policy_cube_dual_side_trade_values.jsonl`
- `strategy/policy_cube/policy_cube_dual_side_trade_values.csv`

The installed cube keeps the same seven live-state key fields:

- `day_type`
- `session_bucket`
- `tte_bucket`
- `signed_bps_bucket`
- `volatility_bucket`
- `up_ask_bucket`
- `down_ask_bucket`

## Retained Policy Counts

- Retained rows: 1,175
- UP value-valid rows: 995
- DOWN value-valid rows: 965
- Both sides value-valid rows: 785
- One side value-valid rows: 390
- No side value-valid rows: 0

## Execution Values

The bot evaluates one value per side:

```text
UP edge = up_trade_value - live_up_ask
DOWN edge = down_trade_value - live_down_ask
```

The cube was built with a 0.03 minimum edge, represented by:

```text
up_max_live_ask_for_min_edge = up_trade_value - 0.03
down_max_live_ask_for_min_edge = down_trade_value - 0.03
```

A side is executable only when its value-valid flag is true, its quote is valid, its ask is inside the bot's configured entry price bounds, and its live ask is at or below the side-specific max-live-ask field.

If both sides are executable, the bot chooses the side with the larger live edge.

## Trade Style Counts

| Direction / Style | Rows |
|---|---:|
| Positive BPS direction | 580 |
| Negative BPS direction | 552 |
| Near-zero BPS direction | 43 |
| UP continuation | 580 |
| UP mean reversion | 552 |
| UP neutral | 43 |
| DOWN continuation | 552 |
| DOWN mean reversion | 580 |
| DOWN neutral | 43 |

## Rows By Session

| Session Bucket | Rows |
|---|---:|
| US_LATE_SESSION | 204 |
| EUROPE_OPEN | 196 |
| ASIA_ACTIVE | 186 |
| ASIA_LOW_LIQUIDITY | 168 |
| US_OPEN_EXPANSION | 167 |
| US_AFTERHOURS | 148 |
| US_PREOPEN | 106 |

## Diagnostics Retained

The cube keeps side-specific path, settlement, sample, and book-pressure diagnostics so runs can be audited after the fact. Settlement evidence is diagnostic only; it is not an execution gate.

For the full row-level overview, see:

- `strategy/research_reference/policy_cube_dual_side_trade_values_overview.md`
- `strategy/research_reference/policy_cube_dual_side_trade_values_validation.md`

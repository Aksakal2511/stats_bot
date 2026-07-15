# Bot Strategy Specification

## Production Model

The active model is `z6_recency45_weekday_v1`, trained on clean/FFM-omitted offset250 observations from `2026-02-14` through `2026-07-13`. Its source of truth is:

```text
strategy/z6_recency45_weekday_v1/policy_bundle.json
strategy/z6_recency45_weekday_v1/entry_profiles.json
```

Coinbase BTC-USD supplies the signal features. Policy settlement values are fitted against resolved Polymarket/Gamma outcomes, not Coinbase-derived outcomes.

It contains the fitted move-z boundaries and four regularised lookup levels:

1. `move_z_bucket + selected_price_bucket`
2. `day_type + move_z_bucket + selected_price_bucket`
3. `day_type + TTE + move_z_bucket + selected_price_bucket`
4. `day_type + 6h London segment + TTE + move_z_bucket + selected_price_bucket`

Lookup falls back to the deepest available parent. Values are settlement probabilities regularised toward their parent estimates. UP and DOWN share evidence through side alignment.

Weekday observations receive weights of `0.5 ** (age_days / 45)`. The recursively weighted base, day, TTE and final levels each use parent strength `10`. Weekend lookups and feature boundaries are copied unchanged from `z6_local_v1`, using lifetime strengths `50/100/80/80`. The bundle stores both complete feature configurations because their trained move-z boundaries differ.

## Causal Signal

Coinbase minute volatility is calculated from completed minutes only:

```text
rv_1h_bps = sqrt(sum(last 60 one-minute log returns squared)) * 10,000
minimum valid history = 45 returns
```

At a decision during minute `M`, the newest permitted close is minute `M-1`. Missing intermediate minutes are forward-filled to match the research dataset. The bot backfills Coinbase candles at startup; it blocks with `LOCAL_VOL_WARMUP` or `LOCAL_VOL_STALE` instead of substituting another feature.

For each side:

```text
aligned_bps(UP)   = signed_bps
aligned_bps(DOWN) = -signed_bps
expected_move     = rv_1h_bps * sqrt(elapsed_seconds / 3600)
move_z            = aligned_bps / max(expected_move, 0.05)
```

Positive aligned movement is `continuation`; negative movement is `mean_reversion`. Near-zero movement is neutral and is not entered.

## Context

`day_type` uses the London calendar: Monday-Friday is `weekday`, otherwise `weekend`.

| London time | Display | Policy label |
|---|---|---|
| 00:00-05:59 | `ASIA` | `h00_06` |
| 06:00-11:59 | `EUROPE` | `h06_12` |
| 12:00-17:59 | `US_OPEN` | `h12_18` |
| 18:00-23:59 | `US_LATE` | `h18_24` |

TTE and selected-side price buckets exactly match those stored in the bundle.

## Entry Decision

Both UP and DOWN are evaluated independently. The shipped profile is continuation `0.04 / 60-239` and mean reversion `0.04 / 60-255`, loaded from `entry_profiles.json`. A side is eligible when:

```text
elapsed >= 45 seconds
style profile min_tte <= TTE <= style profile max_tte
0.20 <= current ask <= 0.80
bid and ask are valid and ask > bid
policy value - current ask >= style profile edge
policy value - min(current ask + 0.01, 0.80) >= style profile edge
BPS_YES_GUARD is inactive
session peak-realised drawdown kill is inactive
```

If both sides qualify, choose larger edge, then larger value, then UP as a deterministic tie-break. There can be only one filled trade per market.

## Removed From Active Entry Logic

- Twelve two-hour session buckets
- Raw signed-BPS policy buckets
- Independent volatility bucket
- Separate UP and DOWN ask dimensions
- Legacy twelve-session edge table
- Recovery, staking and percentage high-water drawdown guards
- Exact-cell damage pauses
- High-volatility muting
- Raw BTC-momentum blocker
- Dynamic staking and anti-martingale bonus

The old files remain for research history or optional `bps_loss_only` exit experiments, but they are not required by the shipped `hold_to_resolution` default and cannot influence `z6_recency45_weekday_v1` entries.

The runtime-only `$15` peak-realised drawdown kill switch remains active as an overnight capital stop. It is not a policy feature and does not select trades.

# Previous Entry Edge Thresholds

This file documents the previous structured entry threshold table. The active bot default is now:

```text
strategy/entry_policy/offset300_hold_value_style_thresholds.csv
```

See `offset300_hold_value_style_thresholds.md` for the current profile.

This table controls whether a side has enough live policy edge to be executable.

The live edge calculation is side-specific:

```text
UP edge   = up_trade_value   - live UP ask
DOWN edge = down_trade_value - live DOWN ask
```

A side can be selected only when:

```text
side_value_valid
AND quote is valid
AND selected ask is inside configured entry price bounds
AND side edge >= entry_day_type + entry_session_bucket threshold
```

The selected side is the executable side with the highest live edge. If both sides tie, higher trade value wins, then UP is used as the deterministic final tie-break.

## Threshold Table

Session order is chronological by London hour bucket: `00-02`, `03-06`, `07-11`, `12-13`, `14-16`, `17-20`, `21-23`.

| Day Type | Session Bucket | Entry Edge Threshold | Trades | Mean PnL | Total PnL |
|---|---|---:|---:|---:|---:|
| weekday | ASIA_LOW_LIQUIDITY | 0.16 | 3,344 | 0.0376 | 125.64 |
| weekday | ASIA_ACTIVE | 0.20 | 3,257 | 0.0288 | 93.74 |
| weekday | EUROPE_OPEN | 0.22 | 4,588 | 0.0382 | 175.07 |
| weekday | US_PREOPEN | 0.20 | 1,532 | 0.0657 | 100.64 |
| weekday | US_OPEN_EXPANSION | 0.21 | 3,376 | 0.0438 | 148.03 |
| weekday | US_LATE_SESSION | 0.25 | 3,630 | 0.0401 | 145.47 |
| weekday | US_AFTERHOURS | 0.25 | 2,852 | 0.0379 | 108.17 |
| weekend | ASIA_LOW_LIQUIDITY | 0.13 | 937 | 0.0452 | 42.34 |
| weekend | ASIA_ACTIVE | 0.12 | 1,588 | 0.0335 | 53.16 |
| weekend | EUROPE_OPEN | 0.13 | 2,241 | 0.0318 | 71.27 |
| weekend | US_PREOPEN | 0.12 | 193 | 0.0209 | 4.03 |
| weekend | US_OPEN_EXPANSION | 0.12 | 990 | 0.0585 | 57.91 |
| weekend | US_LATE_SESSION | 0.12 | 1,751 | 0.0323 | 56.62 |
| weekend | US_AFTERHOURS | 0.12 | 984 | 0.0302 | 29.71 |
| **TOTAL** | **ALL** |  | **31,263** | **0.0388** | **1,211.80** |

## Research Readout

The table was selected from the dual-side edge threshold sweep:

```text
/Volumes/Kate_DATA/policy_finder/data/artifacts/policy_cube_exit_simulation_dual_side_edge_sweep_start20_cooldown10_statsbot_mechanics
```

The selected schedule targets approximately 288 trades per day:

- Simulated date range: 2026-02-14 through 2026-06-02.
- Days covered: 109.
- Trades: 31,263.
- Trades per day: 286.8.
- Raw simulator total PnL: 1,211.80.
- Raw simulator mean PnL: 0.0388.
- Estimated `$5` stake, maker-entry plus taker-exit net after crypto taker exit fees: `$10,139.75`.

This schedule intentionally gives up some unconstrained backtest PnL versus a low fixed threshold so the live edge surface is more structured and less jagged.

## Live Runtime Notes

This file is the human-readable companion to `final_edge_thresholds.csv`.

The runtime uses the CSV. The entry threshold is independent of the BPS exit threshold table in `strategy/exit_policy/final_bps_thresholds.csv`.

For exits, the held side is considered policy-supportive only while the held side still has side-specific live edge at or above the threshold fixed at entry.

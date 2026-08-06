# Stats Bot Research Artifacts

This directory keeps the research scripts and dataset metadata needed to
reproduce the PM-event/Coinbase-tick OOS work that drove the current live edge
selector.

## Dataset

Canonical short name:

```text
pm_event_cb_tick_lag100
```

Local policy-finder path:

```text
/Volumes/Kate_DATA/policy_finder/data/combined_new/session_splits/statsbot_12_bucket_2h_london/pm_event_cb_tick_lag100
```

That path is a short symlink to the original full directory:

```text
combined_asof_offset250_pm_events_cb_tick_lag100_pm_and_coinbase_price_changes
```

The full parquet dataset is about 18GB, so it is intentionally not committed to
this repository. Git tracks only the dataset manifest and daily summary:

```text
research/datasets/pm_event_cb_tick_lag100/coinbase_event_asof_manifest.json
research/datasets/pm_event_cb_tick_lag100/coinbase_event_asof_summary.csv
```

## What It Contains

The dataset is PM event-driven replay data with Coinbase raw-trade as-of values
on the same event clock:

- PM prices: Telonex quote-event rows.
- Coinbase: latest raw trade at `event_timestamp - 100ms`.
- Event clock: PM quote changes plus Coinbase price-change receive timestamps.
- Labels: Polymarket resolution labels, not Coinbase settlement.
- Date range: `2026-02-14` through `2026-07-25`.

## Key Scripts

```text
research/scripts/build_pm_event_coinbase_tick_pipeline.sh
research/scripts/build_pm_event_coinbase_asof_daily.py
research/scripts/run_layer1_edge_execution_degradation.sh
research/scripts/run_large_fold_edge_shrinkage_routes.sh
research/scripts/analyze_large_fold_edge_shrinkage_routes.py
research/scripts/run_move_dz_continuous_surface_oos.py
research/scripts/analyze_edge_selector_probe_oos.py
```

The OOS scripts are mirrored from `policy_finder` for GitHub traceability. They
still expect the policy-finder research environment and supporting modules.
Run them from `/Volumes/Kate_DATA/policy_finder`, or set paths explicitly.

## Current Live Edge Selector

The active `stats_bot` strategy uses:

```text
day_group_time_contradiction_vol_q4 if selected-edge support >= 50
fallback to day_group_time_contradiction
fallback to day_group_time
```

The frozen selector shipped with the bot is:

```text
strategy/btc_linear_z_pb_asym_hl120_excl_apr29_may18_c04_m06_jul25_v1/edge_selector_child118_support50.json
```

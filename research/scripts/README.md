# Research Scripts

These scripts are mirrored from `/Volumes/Kate_DATA/policy_finder/scripts/research`
so the live strategy has its research trail in the `stats_bot` repository.

They are intended to be run in the `policy_finder` research environment because
the OOS driver imports other policy-finder research modules. The shell wrappers
therefore keep policy-finder-style paths and support explicit path overrides.

## Dataset Alias

The event-driven replay dataset is referred to as:

```text
pm_event_cb_tick_lag100
```

Default daily directory:

```text
data/combined_new/session_splits/statsbot_12_bucket_2h_london/pm_event_cb_tick_lag100/intermediate/daily
```

Override when needed:

```bash
PM_EVENT_CB_TICK_ROOT=/path/to/pm_event_cb_tick_lag100 \
  bash research/scripts/run_large_fold_edge_shrinkage_routes.sh
```

## Main Rebuild Command

```bash
cd /Volumes/Kate_DATA/stats_bot

POLICY_FINDER_ROOT=/Volumes/Kate_DATA/policy_finder \
START_DATE=2026-02-14 \
END_DATE=2026-07-25 \
WORKERS=4 \
FEED_LAG_MS=100 \
EVENT_CLOCK=pm_and_coinbase_price_changes \
RUN_PM_EVENTS=0 \
RUN_RAW_COINBASE=0 \
RUN_EVENT_ALIGN=1 \
OVERWRITE=1 \
caffeinate -dimsu research/scripts/build_pm_event_coinbase_tick_pipeline.sh
```

## Main Large Edge Selector Run

```bash
cd /Volumes/Kate_DATA/policy_finder

C_FLOORS=4 \
WORKERS=6 \
caffeinate -dimsu /Volumes/Kate_DATA/stats_bot/research/scripts/run_large_fold_edge_shrinkage_routes.sh
```

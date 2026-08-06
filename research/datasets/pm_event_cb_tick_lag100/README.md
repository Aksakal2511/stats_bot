# pm_event_cb_tick_lag100

This is metadata for the PM-event/Coinbase-tick replay dataset used by the
latest edge-selection simulations.

The full dataset is local-only because it is about 18GB:

```text
/Volumes/Kate_DATA/policy_finder/data/combined_new/session_splits/statsbot_12_bucket_2h_london/pm_event_cb_tick_lag100
```

Tracked files:

```text
coinbase_event_asof_manifest.json
coinbase_event_asof_summary.csv
```

The manifest records the original full path, source PM event rows, raw Coinbase
source, feed lag, event clock, and Polymarket resolution label source.

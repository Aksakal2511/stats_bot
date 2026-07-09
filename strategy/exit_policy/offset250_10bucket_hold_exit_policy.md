# Offset250 10-Bucket Hold Exit Policy

The active strategy runs `hold_to_resolution`, so tactical BPS exits are disabled.

`offset250_10bucket_hold_exit_thresholds.csv` intentionally contains `2.0` rows for every day/session bucket so the shared entry evaluator has a complete day/session threshold table. These values are only used if `STATS_EXIT_POLICY_MODE=bps_loss_only` is explicitly enabled.

# macro_local_v1

`policy_bundle.json` is the retained broad-session rollback artifact trained on offset250 observations from `2026-02-14` through `2026-06-27`. The active production strategy is now `strategy/z6_recency45_weekday_v1/policy_bundle.json`.

It packages the trained move-z boundaries and the base, day-type, TTE and broad London-session regularised policy tables. Production edge thresholds are `0.05` for continuation and mean reversion. The artifact is frozen; live observations must not modify it in place.

Source research artifact:

```text
/Volumes/Kate_DATA/policy_finder/data/artifacts/stats_bot_macro_local_v1_polymarket_labeled
```

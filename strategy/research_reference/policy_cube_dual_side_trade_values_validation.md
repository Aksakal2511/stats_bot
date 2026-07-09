# Dual-Side Trade Value Validation

- input_path: `/Volumes/Kate_DATA/policy_finder/data/artifacts/price_aware_path_policy_cube/simple_reduced/policy_cube_simple_reduced.jsonl`
- output_dir: `/Volumes/Kate_DATA/policy_finder/data/artifacts/price_aware_path_policy_cube/dual_side_trade_values`

## Missing Required Fields

_None._

## Missing Optional Diagnostic Fields

_None._

## Output Fields

- `day_type`
- `session_bucket`
- `tte_bucket`
- `signed_bps_bucket`
- `volatility_bucket`
- `up_ask_bucket`
- `down_ask_bucket`
- `reliability_tier`
- `row_count`
- `unique_market_count`
- `dates_covered`
- `max_single_date_sample_share`
- `bps_direction`
- `up_trade_style`
- `down_trade_style`
- `up_trade_value`
- `down_trade_value`
- `up_max_live_ask_for_min_edge`
- `down_max_live_ask_for_min_edge`
- `up_value_valid`
- `down_value_valid`
- `both_sides_valid`
- `valid_side_count`
- `up_value_invalid_reasons`
- `down_value_invalid_reasons`
- `avg_up_entry_ask`
- `avg_down_entry_ask`
- `up_median_best_available_edge_before_terminal`
- `down_median_best_available_edge_before_terminal`
- `up_prob_profitable_bid_available_before_terminal`
- `down_prob_profitable_bid_available_before_terminal`
- `up_positive_date_rate_path`
- `down_positive_date_rate_path`
- `up_median_adverse_before_profit_before_terminal`
- `down_median_adverse_before_profit_before_terminal`
- `up_win_rate`
- `down_win_rate`
- `up_settlement_edge`
- `down_settlement_edge`
- `up_positive_date_rate_settlement`
- `down_positive_date_rate_settlement`
- `up_settlement_supported`
- `down_settlement_supported`
- `up_path_profile`
- `down_path_profile`
- `avg_up_spread`
- `avg_down_spread`
- `avg_up_book_bid_pressure`
- `avg_down_book_bid_pressure`
- `avg_relative_book_pressure`
- `book_pressure_up_rate`
- `book_pressure_down_rate`
- `book_pressure_neutral_rate`

## Confirmations

- The output cube carries UP and DOWN values separately.
- Legacy one-sided side selection was not retained in the bot-facing output fields.
- Legacy selected settlement-edge was not retained in the bot-facing output fields.
- No `btc_1s_combined.parquet` join was performed.
- No exit simulation was performed.
- The validated cube is installed into `stats_bot/strategy/policy_cube/` as the default policy source.
- Retained policies are research candidates only.

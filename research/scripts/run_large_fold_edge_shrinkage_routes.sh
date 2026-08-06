#!/usr/bin/env bash
set -euo pipefail

POLICY_FINDER_ROOT="${POLICY_FINDER_ROOT:-/Volumes/Kate_DATA/policy_finder}"
cd "$POLICY_FINDER_ROOT"

PYTHON="${PYTHON:-python3}"
WORKERS="${WORKERS:-6}"
OUTPUT_DIR="${OUTPUT_DIR:-data/artifacts/policy_cube_large_edge_shrinkage_full_train_pm_event_cb_tick_jul25_20260725}"
ANALYSIS_DIR="${ANALYSIS_DIR:-$OUTPUT_DIR/large_edge_shrinkage_analysis}"
LOG_DIR="${LOG_DIR:-data/artifacts/logs_large_edge_shrinkage}"
RUN_REPLAY="${RUN_REPLAY:-1}"
DAY_LIMIT="${DAY_LIMIT:-0}"

PM_EVENT_CB_TICK_ROOT="${PM_EVENT_CB_TICK_ROOT:-data/combined_new/session_splits/statsbot_12_bucket_2h_london/pm_event_cb_tick_lag100}"
DAILY_DIR="${DAILY_DIR:-$PM_EVENT_CB_TICK_ROOT/intermediate/daily}"
TRAIN_DAILY_DIR="${TRAIN_DAILY_DIR:-data/combined_new/session_splits/statsbot_12_bucket_2h_london/combined_asof_offset250/intermediate/daily}"
OUTCOMES="${OUTCOMES:-data/artifacts/polymarket_resolution_labels_20260214_20260725.parquet}"
MINUTE_CACHE="${MINUTE_CACHE:-data/artifacts/policy_cube_z6_fixed_specs_oos_pm_clean_ffm_4week/cache/crossvenue_minute_vol_30_60_20260725.parquet}"
DISTANCE_ROOT="${DISTANCE_ROOT:-data/artifacts/distance_to_beat_offset250_20260214_20260725}"

mkdir -p "$LOG_DIR"

if [[ -z "${SPECS:-}" ]]; then
  SPECS=""
  for c in 03 04 05 06 07 08 09 10; do
    for m in 04 05 06 07 08 09 10; do
      if [[ -n "$SPECS" ]]; then
        SPECS+=";"
      fi
      SPECS+="C${c}_M${m}:0.${c},0.${m}"
    done
  done
fi

FAMILIES="${FAMILIES:-global,day_group_time,day_group_time_contradiction,style_price_bucket,day_group_time_vol_q4,day_group_time_contradiction_vol_q4,day_group_vol_q4,price_bucket,style_move_abs_q4}"
ROUTES="${ROUTES:-direct_min500_global,direct_min1000_global,fallback_parent_min500,fallback_parent_min1000,shrink_global_k1000,shrink_parent_k1000,shrink_parent_k3000,shrink_time_first_k1000,shrink_vol_first_k1000,shrink_contradiction_first_k1000}"
C_FLOORS="${C_FLOORS:-3,4}"

echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] START large edge shrinkage routes"
echo "OUTPUT_DIR=$OUTPUT_DIR"
echo "WORKERS=$WORKERS"
echo "FOLD=train Feb14-Jul25 excl Apr29-May18; replay/test Mar01-Jul25; overlap allowed"
echo "MODEL=btc_linear_z_price_bucket asymmetric HL120 C0.05 dt_excluded pooled"
echo "EXECUTION=statsbot_current, latency=350/500/700/1000ms, latch=1.5s, coalesce=100ms"
echo "EDGE GRID=C03..C10 x M04..M10; analysis c_floors=$C_FLOORS"
echo "FAMILIES=$FAMILIES"
echo "ROUTES=$ROUTES"

if [[ "$RUN_REPLAY" == "1" ]]; then
  ARGS=(
    scripts/research/run_move_dz_continuous_surface_oos.py
    --daily-dir "$DAILY_DIR"
    --train-daily-dir "$TRAIN_DAILY_DIR"
    --outcomes "$OUTCOMES"
    --minute-cache "$MINUTE_CACHE"
    --distance-root "$DISTANCE_ROOT"
    --fold-set continuous_large_edge_selector_full_train_jul25_20260725
    --model-kinds btc_linear_z_price_bucket
    --day-type-modes excluded
    --side-modes pooled
    --c-values 0.05
    --excluded-half-lives 120
    --price-bucket-schemes asymmetric
    --specs "$SPECS"
    --tte-ranges 45-265
    --train-min-price 0.20
    --train-max-price 0.80
    --price-bucket-min-rows 5000
    --price-bucket-min-class 100
    --execution-replay pm_event_subsecond
    --execution-entry-modes statsbot_current
    --execution-latencies 350,500,700,1000
    --execution-latches 1.5s
    --decision-coalesce-ms 100
    --execution-stress
    --reuse-train-features
    --train-feature-cache-size 4
    --workers "$WORKERS"
    --output-dir "$OUTPUT_DIR"
    --overwrite
  )
  if [[ "$DAY_LIMIT" != "0" ]]; then
    ARGS+=(--day-limit "$DAY_LIMIT")
  fi
  "$PYTHON" "${ARGS[@]}"
else
  echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] SKIP replay; using $OUTPUT_DIR"
fi

"$PYTHON" scripts/research/analyze_large_fold_edge_shrinkage_routes.py \
  --input-dir "$OUTPUT_DIR" \
  --output-dir "$ANALYSIS_DIR" \
  --minute-cache "$MINUTE_CACHE" \
  --families "$FAMILIES" \
  --routes "$ROUTES" \
  --c-floors "$C_FLOORS" \
  --selection-latencies 350,500,700 \
  --diagnostic-latencies 1000 \
  --overwrite

echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] COMPLETE large edge shrinkage routes output=$OUTPUT_DIR analysis=$ANALYSIS_DIR"

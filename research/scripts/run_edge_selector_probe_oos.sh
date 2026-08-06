#!/usr/bin/env bash
set -euo pipefail

POLICY_FINDER_ROOT="${POLICY_FINDER_ROOT:-/Volumes/Kate_DATA/policy_finder}"
cd "$POLICY_FINDER_ROOT"

PYTHON="${PYTHON:-python3}"
WORKERS="${WORKERS:-4}"
OUTPUT_DIR="${OUTPUT_DIR:-data/artifacts/policy_cube_edge_selector_probe_pm_event_cb_tick_jul25_20260725}"
LOG_DIR="${LOG_DIR:-data/artifacts/logs_edge_selector_probe}"

PM_EVENT_CB_TICK_ROOT="${PM_EVENT_CB_TICK_ROOT:-data/combined_new/session_splits/statsbot_12_bucket_2h_london/pm_event_cb_tick_lag100}"
DAILY_DIR="${DAILY_DIR:-$PM_EVENT_CB_TICK_ROOT/intermediate/daily}"
TRAIN_DAILY_DIR="${TRAIN_DAILY_DIR:-data/combined_new/session_splits/statsbot_12_bucket_2h_london/combined_asof_offset250/intermediate/daily}"
OUTCOMES="${OUTCOMES:-data/artifacts/polymarket_resolution_labels_20260214_20260725.parquet}"
MINUTE_CACHE="${MINUTE_CACHE:-data/artifacts/policy_cube_z6_fixed_specs_oos_pm_clean_ffm_4week/cache/crossvenue_minute_vol_30_60_20260725.parquet}"
DISTANCE_ROOT="${DISTANCE_ROOT:-data/artifacts/distance_to_beat_offset250_20260214_20260725}"

mkdir -p "$LOG_DIR"

specs=""
for c in {3..10}; do
  for m in {3..10}; do
    pair="$(printf 'C%02d_M%02d:0.%02d,0.%02d' "$c" "$m" "$c" "$m")"
    if [[ -z "$specs" ]]; then
      specs="$pair"
    else
      specs="${specs};${pair}"
    fi
  done
done

echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] START edge selector probe"
echo "OUTPUT_DIR=$OUTPUT_DIR"
echo "WORKERS=$WORKERS"
echo "DAILY_DIR=$DAILY_DIR"
echo "OUTCOMES=$OUTCOMES"
echo "SPECS=64 edge pairs C03..C10 x M03..M10"

"$PYTHON" scripts/research/run_move_dz_continuous_surface_oos.py \
  --daily-dir "$DAILY_DIR" \
  --train-daily-dir "$TRAIN_DAILY_DIR" \
  --outcomes "$OUTCOMES" \
  --minute-cache "$MINUTE_CACHE" \
  --distance-root "$DISTANCE_ROOT" \
  --fold-set continuous_edge_selector_probe_jul25_20260725 \
  --model-kinds btc_linear_z_price_bucket \
  --day-type-modes excluded \
  --side-modes pooled \
  --c-values 0.05 \
  --excluded-half-lives 120 \
  --price-bucket-schemes asymmetric \
  --specs "$specs" \
  --tte-ranges '45-265' \
  --train-min-price 0.20 \
  --train-max-price 0.80 \
  --price-bucket-min-rows 5000 \
  --price-bucket-min-class 100 \
  --execution-replay pm_event_subsecond \
  --execution-entry-modes guarded_fast_1c \
  --execution-latencies 350,500,700,1000 \
  --execution-latches no_latch \
  --decision-coalesce-ms 100 \
  --execution-stress \
  --reuse-train-features \
  --train-feature-cache-size 4 \
  --workers "$WORKERS" \
  --output-dir "$OUTPUT_DIR" \
  "$@"

"$PYTHON" scripts/research/analyze_edge_selector_probe_oos.py \
  --input-dir "$OUTPUT_DIR" \
  --selection-latencies 350,500,700 \
  --diagnostic-latencies 1000 \
  --output-dir "$OUTPUT_DIR/edge_selector_probe_analysis" \
  --overwrite

echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] COMPLETE edge selector probe output=$OUTPUT_DIR"

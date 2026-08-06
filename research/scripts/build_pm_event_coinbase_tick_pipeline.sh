#!/usr/bin/env bash
set -euo pipefail

POLICY_FINDER_ROOT="${POLICY_FINDER_ROOT:-/Volumes/Kate_DATA/policy_finder}"
cd "${POLICY_FINDER_ROOT}"

# Build the dataset needed for PM-event/tick-level execution research:
#   raw Coinbase trades -> PM-event rows with Coinbase as-of at each event.
#
# This does not touch data/canonical or the existing 1s combined datasets.

START_DATE="${START_DATE:-2026-02-14}"
END_DATE="${END_DATE:-2026-07-25}"
WORKERS="${WORKERS:-4}"
FEED_LAG_MS="${FEED_LAG_MS:-100}"
EVENT_CLOCK="${EVENT_CLOCK:-pm_and_coinbase_price_changes}"
OVERWRITE="${OVERWRITE:-0}"

RUN_PM_EVENTS="${RUN_PM_EVENTS:-0}"
RUN_RAW_COINBASE="${RUN_RAW_COINBASE:-1}"
RUN_EVENT_ALIGN="${RUN_EVENT_ALIGN:-1}"

BASE_DAILY_DIR="${BASE_DAILY_DIR:-data/combined_new/session_splits/statsbot_12_bucket_2h_london/combined_asof_offset250/intermediate/daily}"
PM_EVENTS_DIR="${PM_EVENTS_DIR:-data/combined_new/session_splits/statsbot_12_bucket_2h_london/combined_asof_offset250_pm_events/intermediate/daily}"
PM_EVENTS_ROOT="${PM_EVENTS_ROOT:-data/combined_new/session_splits/statsbot_12_bucket_2h_london/combined_asof_offset250_pm_events}"
RAW_COINBASE_DIR="${RAW_COINBASE_DIR:-data/raw/coinbase_spot_btc_usd/trades}"
DISTANCE_ROOT="${DISTANCE_ROOT:-data/artifacts/distance_to_beat_offset250_20260214_20260725}"
RESOLUTION_LABELS="${RESOLUTION_LABELS:-data/artifacts/polymarket_resolution_labels_20260214_20260725.parquet}"
OUTPUT_ROOT="${OUTPUT_ROOT:-data/combined_new/session_splits/statsbot_12_bucket_2h_london/pm_event_cb_tick_lag${FEED_LAG_MS}}"
LOG_DIR="${LOG_DIR:-data/artifacts/logs_pm_event_coinbase_tick}"

mkdir -p "${LOG_DIR}"

timestamp() {
  date -u '+%Y-%m-%d %H:%M:%S UTC'
}

OVERWRITE_FLAG=""
if [[ "${OVERWRITE}" == "1" ]]; then
  OVERWRITE_FLAG="--overwrite"
fi

run_step() {
  local name="$1"
  shift
  local log_path="${LOG_DIR}/${name}.log"
  echo "[$(timestamp)] START ${name}" | tee -a "${LOG_DIR}/pipeline.log"
  "$@" 2>&1 | tee "${log_path}"
  echo "[$(timestamp)] DONE  ${name}" | tee -a "${LOG_DIR}/pipeline.log"
}

if [[ "${RUN_PM_EVENTS}" == "1" ]]; then
  run_step "build_pm_event_rows_${START_DATE}_${END_DATE}" \
    python3 scripts/research/build_pm_event_expanded_daily.py \
      --daily-dir "${BASE_DAILY_DIR}" \
      --output-dir "${PM_EVENTS_ROOT}" \
      --start-date "${START_DATE}" \
      --end-date "${END_DATE}" \
      --workers "${WORKERS}" \
      --price-change-only \
      --no-include-base-rows \
      ${OVERWRITE_FLAG:+"${OVERWRITE_FLAG}"}
else
  echo "[$(timestamp)] SKIP PM event expansion; using ${PM_EVENTS_DIR}" | tee -a "${LOG_DIR}/pipeline.log"
fi

if [[ "${RUN_RAW_COINBASE}" == "1" ]]; then
  run_step "download_raw_coinbase_${START_DATE}_${END_DATE}" \
    python3 scripts/research/download_coinbase_raw_trades.py \
      --from-date "${START_DATE}" \
      --to-date "${END_DATE}" \
      --output-dir "${RAW_COINBASE_DIR}" \
      ${OVERWRITE_FLAG:+"${OVERWRITE_FLAG}"} \
      --execute
else
  echo "[$(timestamp)] SKIP raw Coinbase download; using ${RAW_COINBASE_DIR}" | tee -a "${LOG_DIR}/pipeline.log"
fi

if [[ "${RUN_EVENT_ALIGN}" == "1" ]]; then
  run_step "build_pm_event_coinbase_asof_lag${FEED_LAG_MS}_${START_DATE}_${END_DATE}" \
    python3 scripts/research/build_pm_event_coinbase_asof_daily.py \
      --pm-events-dir "${PM_EVENTS_DIR}" \
      --raw-coinbase-dir "${RAW_COINBASE_DIR}" \
      --distance-root "${DISTANCE_ROOT}" \
      --resolution-labels "${RESOLUTION_LABELS:-data/artifacts/polymarket_resolution_labels_20260214_20260725.parquet}" \
      --output-dir "${OUTPUT_ROOT}" \
      --start-date "${START_DATE}" \
      --end-date "${END_DATE}" \
      --feed-lag-ms "${FEED_LAG_MS}" \
      --event-clock "${EVENT_CLOCK:-pm_and_coinbase_price_changes}" \
      --workers "${WORKERS}" \
      ${OVERWRITE_FLAG:+"${OVERWRITE_FLAG}"}
else
  echo "[$(timestamp)] SKIP event alignment" | tee -a "${LOG_DIR}/pipeline.log"
fi

echo "[$(timestamp)] COMPLETE output=${OUTPUT_ROOT}" | tee -a "${LOG_DIR}/pipeline.log"

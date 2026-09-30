#!/usr/bin/env bash
set -euo pipefail

ROOT="${OUTPUT_ROOT:-/Volumes/Kate_DATA/stats_bot/twap/data/artifacts/stage27_live_execution_fee_edge_btc}"
PHASE="${PHASE:-AUDIT}"
WORKERS="${WORKERS:-2}"
RESUME="${RESUME:-1}"
SCENARIO="${SCENARIO:-ALL}"
C_EDGE="${C_EDGE:-2.5}"
LATENCY="${LATENCY:-175,225,325}"

mkdir -p "$ROOT/logs"
resume_arg=()
[[ "$RESUME" == "1" ]] && resume_arg=(--resume)

run_replay() {
  local phase="$1" edges="$2"
  python3 scripts/stage27_replay_execution.py \
    --phase "$phase" --workers "$WORKERS" "${resume_arg[@]}" \
    --scenario "$phase" --c-edge "$edges" --latency "$LATENCY" --output-root "$ROOT"
}

case "$PHASE" in
  AUDIT)
    python3 scripts/stage27_audit_inputs.py --phase AUDIT --output-root "$ROOT"
    ;;
  E0_C25)
    run_replay E0 2.5
    ;;
  E1_C25)
    test ! -f "$ROOT/STOP_E0_REPRODUCTION_FAILED"
    test "$(find "$ROOT/replay/scenario=E0/c_edge=2.5" -name _COMPLETE.json 2>/dev/null | wc -l | tr -d ' ')" = "96"
    run_replay E1 2.5
    ;;
  SWEEP)
    test ! -f "$ROOT/STOP_E0_REPRODUCTION_FAILED"
    run_replay E0 "2.0,3.0,3.5,4.0,4.5,5.0"
    run_replay E1 "2.0,3.0,3.5,4.0,4.5,5.0"
    ;;
  TARGET)
    python3 scripts/stage27_target_conversion.py \
      --phase TARGET "${resume_arg[@]}" --scenario "$SCENARIO" \
      --c-edge "$C_EDGE" --latency "$LATENCY" --output-root "$ROOT"
    ;;
  AGGREGATE)
    python3 scripts/stage27_aggregate.py --phase AGGREGATE --output-root "$ROOT"
    ;;
  ALL)
    python3 scripts/stage27_audit_inputs.py --phase AUDIT --output-root "$ROOT"
    run_replay E0 2.5
    run_replay E1 2.5
    run_replay E0 "2.0,3.0,3.5,4.0,4.5,5.0"
    run_replay E1 "2.0,3.0,3.5,4.0,4.5,5.0"
    python3 scripts/stage27_target_conversion.py --phase TARGET "${resume_arg[@]}" \
      --scenario ALL --c-edge "2.0,2.5,3.0,3.5,4.0,4.5,5.0" \
      --latency "175,225,325" --output-root "$ROOT"
    python3 scripts/stage27_aggregate.py --phase AGGREGATE --output-root "$ROOT"
    ;;
  *)
    echo "Unknown PHASE=$PHASE" >&2
    exit 2
    ;;
esac

#!/bin/bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

STATE_PATH="${STATE_PATH:-artifacts/full_run_v2/state.db}"
OUTPUT_DIR="${OUTPUT_DIR:-evals/recovery}"
INTERRUPT_AFTER_CALLS="${INTERRUPT_AFTER_CALLS:-30}"
PYTHON_BIN="${PYTHON_BIN:-.venv/bin/python}"
ARTIFACT="experiments/distilled/al2-final-v2/distilled_model.joblib"
MANIFEST="experiments/distilled/al2-final-v2/model_manifest.json"
LEGACY_STATE="experiments/codex_luna/checkpoint-10000-normal-usage-v2/consolidated/state.db"
mkdir -p "$OUTPUT_DIR" "$OUTPUT_DIR/grading"

echo "=== interruption/resume demonstration ==="
date -u '+started_utc=%Y-%m-%dT%H:%M:%SZ'
"$PYTHON_BIN" scripts/recovery_evidence.py status --state "$STATE_PATH"
BASE_CALL_ROWID="$(sqlite3 "$STATE_PATH" 'SELECT COALESCE(MAX(rowid),0) FROM calls;')"

set +e
"$PYTHON_BIN" scripts/finalize_full_records.py \
  --state "$STATE_PATH" --artifact "$ARTIFACT" --manifest "$MANIFEST" \
  --legacy-state "$LEGACY_STATE" --output "$OUTPUT_DIR" --phase initial &
FINALIZER_PID=$!
while kill -0 "$FINALIZER_PID" 2>/dev/null; do
  NEW_CALLS="$(sqlite3 "$STATE_PATH" "SELECT COUNT(*) FROM calls WHERE rowid>$BASE_CALL_ROWID AND phase='initial' AND outcome='succeeded';" 2>/dev/null || echo 0)"
  if [ "$NEW_CALLS" -ge "$INTERRUPT_AFTER_CALLS" ]; then
    echo "sending_SIGINT_after_successful_batches=$NEW_CALLS"
    kill -INT "$FINALIZER_PID"
    break
  fi
  sleep 0.1
done
wait "$FINALIZER_PID"
INITIAL_EXIT=$?
set -e
if [ "$INITIAL_EXIT" -ne 75 ]; then
  echo "Expected controlled interruption exit 75, got $INITIAL_EXIT" >&2
  exit 1
fi
cp "$OUTPUT_DIR/finalization_report.json" "$OUTPUT_DIR/initial_finalization_report.json"
"$PYTHON_BIN" -c 'import json,sys; d=json.load(open(sys.argv[1])); assert d["status"]=="interrupted", d' "$OUTPUT_DIR/initial_finalization_report.json"
echo "=== after SIGINT ==="
"$PYTHON_BIN" scripts/recovery_evidence.py status --state "$STATE_PATH"
"$PYTHON_BIN" scripts/recovery_evidence.py snapshot --state "$STATE_PATH" \
  --output "$OUTPUT_DIR/grading/checkpoint_before.json"

echo "=== resume ==="
"$PYTHON_BIN" scripts/finalize_full_records.py \
  --state "$STATE_PATH" --artifact "$ARTIFACT" --manifest "$MANIFEST" \
  --legacy-state "$LEGACY_STATE" --output "$OUTPUT_DIR" --phase resume
cp "$OUTPUT_DIR/finalization_report.json" "$OUTPUT_DIR/resume_finalization_report.json"
"$PYTHON_BIN" -c 'import json,sys; d=json.load(open(sys.argv[1])); assert d["status"]=="completed", d' "$OUTPUT_DIR/resume_finalization_report.json"
"$PYTHON_BIN" scripts/recovery_evidence.py snapshot --state "$STATE_PATH" \
  --output "$OUTPUT_DIR/grading/checkpoint_after.json"
echo "=== after resume ==="
"$PYTHON_BIN" scripts/recovery_evidence.py status --state "$STATE_PATH"
"$PYTHON_BIN" scripts/recovery_evidence.py verify --state "$STATE_PATH" \
  --before "$OUTPUT_DIR/grading/checkpoint_before.json" \
  --after "$OUTPUT_DIR/grading/checkpoint_after.json" \
  --initial-report "$OUTPUT_DIR/initial_finalization_report.json" \
  --resume-report "$OUTPUT_DIR/resume_finalization_report.json" \
  --output "$OUTPUT_DIR/interrupt_resume_check.json"
date -u '+finished_utc=%Y-%m-%dT%H:%M:%SZ'

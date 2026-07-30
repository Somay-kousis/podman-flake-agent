#!/usr/bin/env bash
# Populate a single database with everything the fetch layer can get.
#
# Ordered so each stage has what the next one needs: issues before fixes (the
# linkage checks known_issues), runs before jobs, jobs before logs.
#
# Every stage is idempotent and resumable -- re-run this script after an
# interruption and cached responses make the completed stages nearly free.
#
#   ./hack/full_fetch.sh [DB] [DAYS]

set -u
cd "$(dirname "$0")/.."

DB="${1:-data/flakes.db}"
DAYS="${2:-30}"
LOG="data/fetch.log"

mkdir -p data
: > "$LOG"

stage() {
    local name="$1"; shift
    echo "" | tee -a "$LOG"
    echo "======== $name  ($(date -u +%H:%M:%S)) ========" | tee -a "$LOG"
    python3 -m flakeagent.fetch "$@" --db "$DB" 2>&1 | tee -a "$LOG"
}

echo "target: $DB   window: ${DAYS}d   started $(date -u +%H:%M:%SZ)" | tee -a "$LOG"

stage "issues"      issues
stage "timeline"    timeline    --limit 372
stage "fixes"       fixes       --max-pages 5
stage "runs"        runs        --days "$DAYS"
stage "jobs"        jobs        --limit 900
stage "prfiles"     prfiles     --limit 300
stage "artifacts"   artifacts   --limit 300
stage "annotations" annotations --limit 300
stage "logs"        logs        --limit 900

echo "" | tee -a "$LOG"
echo "======== done $(date -u +%H:%M:%SZ) ========" | tee -a "$LOG"
python3 -m flakeagent.fetch status --db "$DB" 2>&1 | tee -a "$LOG"

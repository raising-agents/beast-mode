#!/usr/bin/env bash
# Chained: full backfill → dimension-discovery analysis.
# Both stages log to ~/.claude/beast-mode/ledger/ and ~/.claude/beast-mode/analysis/.

set -u
PY=/opt/miniconda3/bin/python
LOG=$HOME/.claude/beast-mode/ledger/run-all.log
exec >> "$LOG" 2>&1

echo "=========================================="
echo "RUN-ALL START $(date -u +%FT%TZ)"
echo "=========================================="

echo ""
echo "[1/2] BACKFILL — batched, 30 workers, batch 15"
echo "------------------------------------------"
$PY $HOME/.claude/beast-mode/bin/backfill-ledger-batched.py --workers 30 --batch-size 15
BF_RC=$?
echo "backfill exit: $BF_RC"

echo ""
echo "[2/2] DIMENSION DISCOVERY ANALYSIS (Opus)"
echo "------------------------------------------"
$PY $HOME/.claude/beast-mode/bin/analyze-missing-dimensions.py
ANA_RC=$?
echo "analysis exit: $ANA_RC"

echo ""
echo "RUN-ALL DONE $(date -u +%FT%TZ)  bf=$BF_RC  ana=$ANA_RC"

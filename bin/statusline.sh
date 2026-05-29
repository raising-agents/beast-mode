#!/usr/bin/env bash
# Beast Mode statusline. Reads recent drift ledger entries, shows rolling
# Beast Index + top leak dimensions. Fast (<50ms).
#
# Combines with the caveman statusline badge if that plugin is installed.

set -euo pipefail

LEDGER="$HOME/.claude/beast-mode/ledger/drift.jsonl"
CAVEMAN_HOOK="$HOME/.claude/plugins/cache/caveman/caveman/84cc3c14fa1e/hooks/caveman-statusline.sh"

# Read JSON from stdin (status line input contract). We don't strictly need it
# but consuming stdin prevents EPIPE if Claude pipes data.
STDIN_JSON=$(cat 2>/dev/null || true)

# Compose left segment: caveman badge if available.
LEFT=""
if [[ -x "$CAVEMAN_HOOK" ]]; then
    CAVEMAN_OUT=$(printf '%s' "$STDIN_JSON" | "$CAVEMAN_HOOK" 2>/dev/null || true)
    if [[ -n "$CAVEMAN_OUT" ]]; then
        LEFT="$CAVEMAN_OUT "
    fi
fi

# Compose middle segment: Beast Index from ledger.
BEAST=""
if [[ -f "$LEDGER" ]]; then
    BEAST=$(/opt/miniconda3/bin/python3 - <<'PY' "$LEDGER"
import json, sys
from collections import Counter
path = sys.argv[1]
try:
    with open(path) as f:
        lines = f.readlines()[-30:]
except Exception:
    sys.exit(0)
beasts = applicable = 0
leak_dims: Counter = Counter()
for line in lines:
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        continue
    for dim, v in (obj.get("dims") or {}).items():
        if v in (0, 1):
            applicable += 1
            if v == 1:
                beasts += 1
            else:
                leak_dims[dim] += 1
if applicable == 0:
    sys.exit(0)
pct = beasts / applicable * 100
parts = [f"BEAST {beasts}/{applicable} {pct:.0f}%"]
short = {"parallelism":"par","scope":"sc","depth":"dep","boldness":"bold","action_over_announcement":"ann","verification_by_evidence":"ver","sequencing":"seq","deferrals":"def"}
top = [f"{short.get(d, d)}x{n}" for d, n in leak_dims.most_common(3)]
if top:
    parts.append("leak:" + ",".join(top))
print(" ".join(parts))
PY
)
fi

# Compose right segment: model name from stdin JSON.
MODEL=""
if [[ -n "$STDIN_JSON" ]]; then
    MODEL=$(printf '%s' "$STDIN_JSON" | /opt/homebrew/bin/jq -r '.model.display_name // .model.id // empty' 2>/dev/null || true)
fi

# Compose health badge: EVOL ERR / AUDIT ERR / EVOL STALE (empty if all healthy).
HEALTH=""
HEALTH_SCRIPT="$HOME/.claude/beast-mode/bin/health.py"
if [[ -x "$HEALTH_SCRIPT" ]]; then
    HEALTH=$(/opt/miniconda3/bin/python3 "$HEALTH_SCRIPT" status 2>/dev/null || true)
fi

# Assemble.
OUT=""
[[ -n "$LEFT" ]] && OUT="$LEFT"
[[ -n "$BEAST" ]] && OUT="$OUT$BEAST"
[[ -n "$HEALTH" ]] && OUT="$OUT  ${HEALTH}"
[[ -n "$MODEL" ]] && OUT="$OUT  ${MODEL}"
printf '%s' "${OUT:-Claude Code}"

#!/usr/bin/env python3
"""
Beast Mode Stop hook.

Fires after every assistant response. Spawns the auditor worker as a
detached background process and returns immediately. The worker reads
the transcript, calls Haiku, and appends to the drift ledger.

This hook NEVER blocks the user. If the worker fails, errors land in
~/.claude/beast-mode/ledger/auditor-errors.log.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

WORKER = Path.home() / ".claude" / "beast-mode" / "bin" / "auditor-worker.py"
PYTHON = "/opt/miniconda3/bin/python"
LOG_DIR = Path.home() / ".claude" / "beast-mode" / "ledger"


def main() -> int:
    # Recursion guard: if this Claude session is already a Beast Mode worker, skip.
    if os.environ.get("BEAST_MODE_AUDITOR_RUNNING") == "1":
        return 0
    if os.environ.get("BEAST_MODE_INTERPRETER_RUNNING") == "1":
        return 0

    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, ValueError):
        return 0

    transcript_path = data.get("transcript_path") or ""
    session_id = data.get("session_id") or "unknown"
    if not transcript_path:
        return 0

    if not WORKER.exists():
        return 0

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / "auditor.log"

    # Detached background spawn.
    # Do NOT set BEAST_MODE_AUDITOR_RUNNING here — the worker checks that flag
    # in call_haiku() to guard against recursive Haiku invocations. Setting it
    # in the worker's env would cause it to skip the Haiku call entirely.
    try:
        with log_path.open("a") as logf:
            env = os.environ.copy()
            subprocess.Popen(
                [PYTHON, str(WORKER), transcript_path, session_id],
                stdin=subprocess.DEVNULL,
                stdout=logf,
                stderr=subprocess.STDOUT,
                env=env,
                start_new_session=True,
                close_fds=True,
            )
    except Exception:
        # Fail open — never block the user.
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())

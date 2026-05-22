#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

MANAGED_LOGIC_PATHS = (
    "runtime/ibkr_compute/src/ibkr_compute/workflows/daily_scanner",
    "runtime/ibkr_compute/src/ibkr_compute/api/market/screener",
    "runtime/ibkr_compute/src/ibkr_compute/core/indicator_engine.py",
    "runtime/ibkr_compute/src/ibkr_compute/core/indicators",
    "runtime/ibkr_compute/src/ibkr_compute/core/signal_generator.py",
    "runtime/ibkr_compute/src/ibkr_compute/core/position_sizing.py",
    "runtime/ibkr_compute/src/ibkr_compute/core/exit_policy.py",
    "runtime/ibkr_compute/src/ibkr_compute/signal/signal_processor.py",
    "runtime/ibkr_compute/src/ibkr_compute/signal/reverse_signal.py",
    "runtime/ibkr_compute/src/ibkr_compute/backtest",
    "runtime/ibkr_scheduler/src/ibkr_scheduler/cron_registry.py",
    "runtime/ibkr_scheduler/src/ibkr_scheduler/scheduler_app.py",
    "runtime/ibkr_api/src/ibkr_api/system/jobs",
    "runtime/ibkr_api/src/ibkr_api/orders",
    "runtime/ibkr_api/src/ibkr_api/reverse",
    "runtime/ibkr_api/src/ibkr_api/universe/lifecycle_flow",
)

SYSTEM_LOGIC_SYNC_PATHS = (
    "runtime/ibkr_compute/src/ibkr_compute/api/market/rules_views.py",
    "runtime/ibkr_console/static/ibkr_system_logic.html",
    "runtime/ibkr_console/static/assets/js/pages/ibkr_system_logic",
    "runtime/ibkr_console/static/assets/css/pages/ibkr_system_logic",
    "extensions/ibkr_compute/tests/test_system_logic_rules.py",
    "extensions/ibkr_api/tests/test_system_logic_static.py",
    "ops/validate/check_system_logic_sync.py",
)


def _git_changed_paths() -> set[str]:
    commands = [
        ["git", "diff", "--name-only"],
        ["git", "diff", "--name-only", "--cached"],
        ["git", "ls-files", "--others", "--exclude-standard"],
    ]
    changed: set[str] = set()
    for command in commands:
        result = subprocess.run(command, cwd=REPO_ROOT, check=False, text=True, capture_output=True)
        if result.returncode != 0:
            continue
        changed.update(line.strip() for line in result.stdout.splitlines() if line.strip())
    return changed


def _matches(path: str, prefixes: tuple[str, ...]) -> bool:
    normalized = path.replace("\\", "/")
    return any(normalized == prefix or normalized.startswith(f"{prefix}/") for prefix in prefixes)


def main() -> int:
    changed = _git_changed_paths()
    managed = sorted(path for path in changed if _matches(path, MANAGED_LOGIC_PATHS))
    if not managed:
        print("system logic sync: no managed logic changes detected")
        return 0

    sync_changes = sorted(path for path in changed if _matches(path, SYSTEM_LOGIC_SYNC_PATHS))
    if sync_changes:
        print("system logic sync: ok")
        print("managed logic changes:")
        for path in managed:
            print(f"- {path}")
        print("sync changes:")
        for path in sync_changes:
            print(f"- {path}")
        return 0

    print("system logic sync: missing system logic update", file=sys.stderr)
    print("Managed logic changed, but no system logic page/API/test update was detected.", file=sys.stderr)
    print("Update at least one of:", file=sys.stderr)
    for path in SYSTEM_LOGIC_SYNC_PATHS:
        print(f"- {path}", file=sys.stderr)
    print("Managed files:", file=sys.stderr)
    for path in managed:
        print(f"- {path}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

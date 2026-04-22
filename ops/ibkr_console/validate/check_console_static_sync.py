#!/usr/bin/env python3
from __future__ import annotations

import filecmp
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SOURCE_DIR = REPO_ROOT / "runtime" / "ibkr_console" / "static"
LEGACY_DIR = REPO_ROOT / "runtime" / "pocketbase" / "pb_public"
IGNORE = {".DS_Store", "Thumbs.db"}


def compare_dirs(left: Path, right: Path, rel: str = "") -> list[str]:
    issues: list[str] = []
    cmp = filecmp.dircmp(left, right, ignore=list(IGNORE))
    for name in sorted(cmp.left_only):
        issues.append(f"only in source: {Path(rel, name)}")
    for name in sorted(cmp.right_only):
        issues.append(f"only in legacy: {Path(rel, name)}")
    for name in sorted(cmp.diff_files):
        issues.append(f"content mismatch: {Path(rel, name)}")
    for name in sorted(cmp.funny_files):
        issues.append(f"funny file: {Path(rel, name)}")
    for subdir in sorted(cmp.common_dirs):
        issues.extend(compare_dirs(left / subdir, right / subdir, str(Path(rel, subdir))))
    return issues


def main() -> int:
    if not SOURCE_DIR.is_dir():
        print(f"missing source dir: {SOURCE_DIR}", file=sys.stderr)
        return 2
    if not LEGACY_DIR.is_dir():
        print(f"missing legacy dir: {LEGACY_DIR}", file=sys.stderr)
        return 2

    issues = compare_dirs(SOURCE_DIR, LEGACY_DIR)
    if issues:
        print("console static trees diverged:")
        for item in issues:
            print(f"- {item}")
        return 1

    print(f"console static trees match: {SOURCE_DIR} == {LEGACY_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

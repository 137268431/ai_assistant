#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SOURCE_DIR = REPO_ROOT / "runtime" / "ibkr_console" / "static"
PB_PUBLIC_DIR = REPO_ROOT / "runtime" / "pocketbase" / "pb_public"
SOURCE_INDEX = SOURCE_DIR / "index.html"
PB_INDEX = PB_PUBLIC_DIR / "index.html"
PB_LEGACY_COMMON = PB_PUBLIC_DIR / "common.js"
PB_LEGACY_ASSETS = PB_PUBLIC_DIR / "assets"
PB_LANDING_MARKER = "PocketBase lives here."


def main() -> int:
    if not SOURCE_DIR.is_dir():
        print(f"missing source dir: {SOURCE_DIR}", file=sys.stderr)
        return 2
    if not PB_PUBLIC_DIR.is_dir():
        print(f"missing PocketBase public dir: {PB_PUBLIC_DIR}", file=sys.stderr)
        return 2
    if not SOURCE_INDEX.is_file():
        print(f"missing console index: {SOURCE_INDEX}", file=sys.stderr)
        return 2
    if not PB_INDEX.is_file():
        print(f"missing PocketBase landing index: {PB_INDEX}", file=sys.stderr)
        return 2

    console_index = SOURCE_INDEX.read_text(encoding="utf-8")
    pb_index = PB_INDEX.read_text(encoding="utf-8")
    if console_index == pb_index:
        print("unexpected match: PocketBase landing still mirrors console index")
        return 1
    if PB_LANDING_MARKER not in pb_index:
        print("unexpected PocketBase landing: missing PocketBase-specific landing marker")
        return 1
    if PB_LEGACY_COMMON.exists():
        print(f"unexpected PocketBase legacy bundle still present: {PB_LEGACY_COMMON}")
        return 1
    if PB_LEGACY_ASSETS.exists():
        print(f"unexpected PocketBase legacy assets tree still present: {PB_LEGACY_ASSETS}")
        return 1

    extra_html = sorted(path.name for path in PB_PUBLIC_DIR.glob("*.html") if path.name != "index.html")
    if extra_html:
        print(f"unexpected PocketBase legacy html pages still present: {extra_html}")
        return 1

    print("console and PocketBase public trees are intentionally decoupled")
    print(f"- console source: {SOURCE_DIR}")
    print(f"- pocketbase landing: {PB_PUBLIC_DIR}")
    print("- PocketBase public tree now keeps only the landing/admin-facing root")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

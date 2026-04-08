#!/usr/bin/env python3
"""
Audit PocketBase collection usage before any schema cleanup or rename.

This is intentionally read-only. It helps answer:
1. Is the table still referenced by hooks / UI / compute?
2. Is the table canonical, legacy, or isolated?
3. Is it safe to rename or delete yet?
"""

from __future__ import annotations

import argparse
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SEARCH_ROOTS = (ROOT / "pocketbase", ROOT / "ibkr_compute")
SKIP_DIRS = {
    ".git",
    "__pycache__",
    "node_modules",
    ".venv",
    "venv",
    "dist",
    "build",
    "pb_data",
}
SKIP_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".woff",
    ".woff2",
    ".ttf",
    ".pyc",
    ".db",
    ".sqlite",
}

COLLECTIONS = {
    "orders": {
        "role": "canonical_app_orders",
        "cleanup_stage": "retain",
        "rename_target": "",
        "note": "Main app order lifecycle table.",
    },
    "order_details": {
        "role": "canonical_app_order_events",
        "cleanup_stage": "rename_after_readers_switch",
        "rename_target": "ibkr_order_details",
        "note": "Main app order event log with ambiguous naming.",
    },
    "ibkr_orders": {
        "role": "legacy_broker_snapshot",
        "cleanup_stage": "deprecate_after_reader_migration",
        "rename_target": "",
        "note": "Raw broker mirror still used by tracker and legacy readers.",
    },
    "reverse_signals": {
        "role": "canonical_reverse_workflow",
        "cleanup_stage": "rename_after_readers_switch",
        "rename_target": "ibkr_reverse_signals",
        "note": "Reverse workflow table; not safe to delete while UI/hooks/compute still read it.",
    },
    "ibkr_backtest_reverse_signals": {
        "role": "backtest_only",
        "cleanup_stage": "retain_isolated",
        "rename_target": "",
        "note": "Backtest-only reverse capture table.",
    },
}


def iter_files():
    for search_root in SEARCH_ROOTS:
        for path in search_root.rglob("*"):
            if not path.is_file():
                continue
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            if path.suffix.lower() in SKIP_SUFFIXES:
                continue
            yield path


def collect_hits(target: str) -> list[tuple[str, int, str]]:
    hits: list[tuple[str, int, str]] = []
    for path in iter_files():
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (UnicodeDecodeError, OSError):
            continue
        for idx, line in enumerate(lines, start=1):
            if target in line:
                hits.append((str(path.relative_to(ROOT)), idx, line.strip()))
    return hits


def render(targets: list[str], max_refs: int) -> int:
    for name in targets:
        meta = COLLECTIONS.get(name, {})
        hits = collect_hits(name)
        print(f"collection={name}")
        print(f"  role={meta.get('role', 'unknown')}")
        print(f"  cleanup_stage={meta.get('cleanup_stage', 'unknown')}")
        print(f"  rename_target={meta.get('rename_target', '') or '-'}")
        print(f"  refs={len(hits)}")
        print(f"  note={meta.get('note', '-')}")
        for rel_path, line_no, line in hits[:max_refs]:
            print(f"    {rel_path}:{line_no}: {line}")
        if len(hits) > max_refs:
            print(f"    ... {len(hits) - max_refs} more refs")
        print()
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit collection usage before schema cleanup.")
    parser.add_argument(
        "collections",
        nargs="*",
        default=list(COLLECTIONS.keys()),
        help="Collection names to inspect. Defaults to built-in tracked collections.",
    )
    parser.add_argument(
        "--max-refs",
        type=int,
        default=12,
        help="Maximum number of sample references to print per collection.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    return render(args.collections, max(1, args.max_refs))


if __name__ == "__main__":
    raise SystemExit(main())

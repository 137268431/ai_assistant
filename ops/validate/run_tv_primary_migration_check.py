#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]

PB_SCHEMA_ROOTS = (
    Path("extensions/pocketbase/schema/pb_table"),
    Path("extensions/pocketbase/schema/table_json"),
)
TV_WEBHOOK_EVENTS_SCHEMA = Path("extensions/pocketbase/schema/pb_table/schema_tv_webhook_events.json")
TV_WEBHOOK_EVENTS_TABLE = "tv_webhook_events"

# TV-primary stores inbound TradingView payloads as webhook events. These older
# shadow/indicator collections should not remain in exported PocketBase schemas.
DELETED_SCHEMA_TABLES = (
    "ibkr_indicators",
    "tv_indicators",
    "tv_indicator_audit_snapshots",
    "tv_signals",
)

# Keep this scoped to signal/system static files so the check catches hard UI
# dependencies without policing unrelated historical/debug pages.
SIGNAL_SYSTEM_STATIC_FILES = (
    Path("runtime/ibkr_console/static/ibkr_signals.html"),
    Path("runtime/ibkr_console/static/assets/js/pages/ibkr_system/state-api.js"),
    Path("runtime/ibkr_console/static/assets/js/pages/ibkr_system/actions.js"),
    Path("runtime/ibkr_console/static/assets/js/pages/ibkr_system/renderers.js"),
)
FORBIDDEN_UI_TABLES = (
    "ibkr_indicators",
    "tv_indicators",
    "tv_indicator_audit_snapshots",
    "tv_signals",
)


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    message: str
    details: tuple[str, ...] = ()


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _load_json(path: Path) -> object:
    return json.loads(_read_text(path))


def _schema_files(repo_root: Path) -> list[Path]:
    files: list[Path] = []
    for relative_root in PB_SCHEMA_ROOTS:
        root = repo_root / relative_root
        if root.exists():
            files.extend(sorted(root.glob("*.json")))
    return sorted(files)


def _table_token_regex(table: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![A-Za-z0-9_]){re.escape(table)}(?![A-Za-z0-9_])")


def _quoted_table_regex(table: str) -> re.Pattern[str]:
    return re.compile(rf"(['\"`])(?:count:)?{re.escape(table)}\1")


def _schema_file_table_name(path: Path) -> str:
    stem = path.stem
    if stem.startswith("schema_"):
        return stem[len("schema_") :]
    return stem


def _check_new_tv_webhook_schema(repo_root: Path) -> CheckResult:
    path = repo_root / TV_WEBHOOK_EVENTS_SCHEMA
    display_path = TV_WEBHOOK_EVENTS_SCHEMA.as_posix()
    if not path.exists():
        return CheckResult(
            "schema_tv_webhook_events",
            False,
            f"missing {display_path}",
        )

    try:
        text = _read_text(path)
        _load_json(path)
    except json.JSONDecodeError as exc:
        return CheckResult(
            "schema_tv_webhook_events",
            False,
            f"invalid JSON in {display_path}: {exc}",
        )

    if not _table_token_regex(TV_WEBHOOK_EVENTS_TABLE).search(text):
        return CheckResult(
            "schema_tv_webhook_events",
            False,
            f"{display_path} does not define/reference {TV_WEBHOOK_EVENTS_TABLE}",
        )

    return CheckResult(
        "schema_tv_webhook_events",
        True,
        f"found {display_path}",
    )


def _check_schema_json_parse(repo_root: Path) -> CheckResult:
    failures: list[str] = []
    for path in _schema_files(repo_root):
        try:
            _load_json(path)
        except json.JSONDecodeError as exc:
            failures.append(f"{path.relative_to(repo_root).as_posix()}: {exc}")
    if failures:
        return CheckResult(
            "schema_json",
            False,
            "schema JSON files must parse",
            tuple(failures),
        )
    return CheckResult("schema_json", True, "schema JSON files parse")


def _check_deleted_schema_tables(repo_root: Path) -> CheckResult:
    hits: dict[str, list[str]] = {table: [] for table in DELETED_SCHEMA_TABLES}
    regexes = {table: _table_token_regex(table) for table in DELETED_SCHEMA_TABLES}

    for path in _schema_files(repo_root):
        rel_path = path.relative_to(repo_root).as_posix()
        text = _read_text(path)
        table_from_filename = _schema_file_table_name(path)
        for table, regex in regexes.items():
            if table_from_filename == table or regex.search(text):
                hits[table].append(rel_path)

    details = tuple(
        f"{table}: {', '.join(paths)}"
        for table, paths in hits.items()
        if paths
    )
    if details:
        return CheckResult(
            "schema_deleted_tables",
            False,
            "deleted TV-primary table schemas still present",
            details,
        )
    return CheckResult(
        "schema_deleted_tables",
        True,
        "deleted TV-primary table schemas are absent",
    )


def _find_static_table_literals(repo_root: Path) -> list[str]:
    regexes = {table: _quoted_table_regex(table) for table in FORBIDDEN_UI_TABLES}
    matches: list[str] = []
    for relative_path in SIGNAL_SYSTEM_STATIC_FILES:
        path = repo_root / relative_path
        display_path = relative_path.as_posix()
        if not path.exists():
            matches.append(f"{display_path}: missing static file")
            continue
        for line_no, line in enumerate(_read_text(path).splitlines(), start=1):
            for table, regex in regexes.items():
                if regex.search(line):
                    snippet = line.strip()
                    if len(snippet) > 140:
                        snippet = f"{snippet[:137]}..."
                    matches.append(f"{display_path}:{line_no}: {table}: {snippet}")
    return matches


def _check_signal_system_static(repo_root: Path) -> CheckResult:
    matches = _find_static_table_literals(repo_root)
    if matches:
        return CheckResult(
            "static_signal_system_deps",
            False,
            "signal/system UI still has hard dependencies on old indicator/TV tables",
            tuple(matches),
        )
    return CheckResult(
        "static_signal_system_deps",
        True,
        "no blocked table literals in scoped signal/system UI files",
    )


def run_checks(repo_root: Path) -> Sequence[CheckResult]:
    return (
        _check_new_tv_webhook_schema(repo_root),
        _check_schema_json_parse(repo_root),
        _check_deleted_schema_tables(repo_root),
        _check_signal_system_static(repo_root),
    )


def _print_results(results: Iterable[CheckResult]) -> bool:
    all_ok = True
    result_list = list(results)
    for result in result_list:
        status = "PASS" if result.ok else "FAIL"
        print(f"{status} {result.name}: {result.message}")
        for detail in result.details:
            print(f"  - {detail}")
        all_ok = all_ok and result.ok
    passed = sum(1 for result in result_list if result.ok)
    failed = len(result_list) - passed
    print(f"SUMMARY tv_primary_migration_check: {passed} passed, {failed} failed")
    return all_ok


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate TV-primary migration schema and static UI cleanup.")
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=REPO_ROOT,
        help="repository root to inspect (defaults to this script's repo)",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    repo_root = args.repo_root.resolve()
    results = run_checks(repo_root)
    return 0 if _print_results(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

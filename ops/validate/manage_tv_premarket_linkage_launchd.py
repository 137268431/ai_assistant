#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


ET = ZoneInfo("America/New_York")
CN = ZoneInfo("Asia/Shanghai")

REPO_ROOT = Path(__file__).resolve().parents[2]
RUN_SCRIPT = REPO_ROOT / "ops" / "validate" / "run_tv_premarket_linkage.py"
ARTIFACT_ROOT = REPO_ROOT / "artifacts" / "validation" / "tv_premarket_linkage"
LABEL_PREFIX = "com.lzwglory.ai-assistant.tv-premarket-linkage"
DEFAULT_PYTHON = Path("/Users/lzwglory/miniforge3/bin/python3.12")
CONFIRM_TEXT = "PAPER_TV_PREMARKET_LINKAGE"


def safe_label_part(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9.-]+", "-", str(value or "").strip()).strip("-").lower()
    return text or "run"


def default_run_id() -> str:
    return f"TVPREMKT_{datetime.now(ET).strftime('%Y%m%d')}_0405_ET"


def python_executable() -> Path:
    return DEFAULT_PYTHON if DEFAULT_PYTHON.exists() else Path(sys.executable)


def launch_domain() -> str:
    return f"gui/{os.getuid()}"


def launch_agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def label_for_run(run_id: str) -> str:
    return f"{LABEL_PREFIX}.{safe_label_part(run_id)}"


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=True)


def parse_launchctl_print(text: str) -> dict[str, Any]:
    result: dict[str, Any] = {"raw_available": bool(text)}
    for key, pattern in {
        "state": r"\bstate = ([^\n]+)",
        "pid": r"\bpid = (\d+)",
        "runs": r"\bruns = (\d+)",
        "last_exit_code": r"\blast exit code = ([^\n]+)",
        "path": r"\bpath = ([^\n]+)",
    }.items():
        match = re.search(pattern, text or "")
        if not match:
            continue
        value: Any = match.group(1).strip()
        if key in {"pid", "runs"}:
            try:
                value = int(value)
            except Exception:
                pass
        result[key] = value
    return result


def read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def known_plists() -> list[Path]:
    root = launch_agents_dir()
    if not root.exists():
        return []
    return sorted(root.glob(f"{LABEL_PREFIX}*.plist"))


def labels_from_plists() -> list[str]:
    labels: list[str] = []
    for path in known_plists():
        try:
            payload = plistlib.loads(path.read_bytes())
        except Exception:
            continue
        label = str(payload.get("Label") or "").strip()
        if label.startswith(LABEL_PREFIX):
            labels.append(label)
    return sorted(set(labels))


def artifact_dir(run_id: str) -> Path:
    return ARTIFACT_ROOT / run_id


def compact_artifacts(run_id: str) -> dict[str, Any]:
    root = artifact_dir(run_id)
    summary = read_json(root / "summary.json")
    progress = read_json(root / "progress.json")
    waiting = read_json(root / "waiting.json")
    meta = read_json(root / "runner.launchd.meta.json")
    return {
        "artifact_dir": str(root),
        "meta": meta,
        "summary": {
            "available": bool(summary),
            "ok": summary.get("ok"),
            "status": summary.get("premarket_validation_status"),
            "completed_count": summary.get("completed_count"),
            "target_completed_chains": summary.get("target_completed_chains"),
            "reason": summary.get("reason"),
            "premarket_trading_allowed": summary.get("premarket_trading_allowed"),
            "finished_at_et": summary.get("finished_at_et"),
        },
        "progress": {
            "available": bool(progress),
            "completed_count": progress.get("completed_count"),
            "attempts": progress.get("attempts"),
            "reason": progress.get("reason"),
        },
        "waiting": {
            "available": bool(waiting),
            "now_et": waiting.get("now_et"),
            "start_et": waiting.get("start_et"),
            "sleep_seconds": waiting.get("sleep_seconds"),
        },
    }


def build_runner_args(args: argparse.Namespace) -> list[str]:
    command = [
        str(python_executable()),
        str(RUN_SCRIPT),
        "--run-id",
        args.run_id,
        "--execute",
        "--confirm",
        CONFIRM_TEXT,
        "--wait-until-start",
        "--target-completed-chains",
        str(args.target_completed_chains),
        "--target-notional-per-chain",
        str(args.target_notional_per_chain),
        "--symbols",
        args.symbols,
        "--exclude-symbols",
        args.exclude_symbols,
        "--marketable-bps",
        str(args.marketable_bps),
        "--max-limit-slippage-bps",
        str(args.max_limit_slippage_bps),
        "--max-spread-bps",
        str(args.max_spread_bps),
        "--start-et",
        args.start_et,
        "--stop-new-entry-et",
        args.stop_new_entry_et,
        "--chain-spacing-seconds",
        str(args.chain_spacing_seconds),
        "--poll-seconds",
        str(args.poll_seconds),
        "--fill-timeout-seconds",
        str(args.fill_timeout_seconds),
        "--cleanup-timeout-seconds",
        str(args.cleanup_timeout_seconds),
        "--format",
        "json",
    ]
    if args.skip_health:
        command.append("--skip-health")
    if args.allow_existing_paper_state:
        command.append("--allow-existing-paper-state")
    return command


def write_start_files(args: argparse.Namespace) -> dict[str, Any]:
    root = artifact_dir(args.run_id)
    root.mkdir(parents=True, exist_ok=True)
    stale_files = [root / name for name in ("summary.json", "progress.json", "waiting.json")]
    existing_stale = [path for path in stale_files if path.exists()]
    archive_dir = None
    if existing_stale:
        archive_dir = root / f"prelaunch_archive_{datetime.now(ET).strftime('%Y%m%d_%H%M%S')}"
        archive_dir.mkdir(parents=True, exist_ok=True)
        for path in existing_stale:
            path.rename(archive_dir / path.name)
    label = label_for_run(args.run_id)
    plist_path = launch_agents_dir() / f"{label}.plist"
    wrapper_path = root / "runner_launchd.sh"
    stdout_path = root / "launchd.stdout.log"
    stderr_path = root / "launchd.stderr.log"
    runner_args = build_runner_args(args)

    quoted = " ".join(json.dumps(part) for part in runner_args)
    wrapper_path.write_text(
        "\n".join(
            [
                "#!/bin/zsh",
                "set -euo pipefail",
                f"cd {json.dumps(str(REPO_ROOT))}",
                "echo runner_started_at_cn=$(TZ=Asia/Shanghai date '+%Y-%m-%dT%H:%M:%S%z')",
                "echo runner_started_at_et=$(TZ=America/New_York date '+%Y-%m-%dT%H:%M:%S%z')",
                f"exec {quoted}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    wrapper_path.chmod(0o755)

    plist = {
        "Label": label,
        "ProgramArguments": [str(wrapper_path)],
        "WorkingDirectory": str(REPO_ROOT),
        "RunAtLoad": True,
        "KeepAlive": False,
        "StandardOutPath": str(stdout_path),
        "StandardErrorPath": str(stderr_path),
    }
    launch_agents_dir().mkdir(parents=True, exist_ok=True)
    plist_path.write_bytes(plistlib.dumps(plist))

    meta = {
        "label": label,
        "plist": str(plist_path),
        "wrapper": str(wrapper_path),
        "stdout": str(stdout_path),
        "stderr": str(stderr_path),
        "artifact_dir": str(root),
        "run_id": args.run_id,
        "start_et": args.start_et,
        "stop_new_entry_et": args.stop_new_entry_et,
        "target_completed_chains": args.target_completed_chains,
        "target_notional_per_chain": args.target_notional_per_chain,
        "created_at_et": datetime.now(ET).isoformat(),
        "created_at_cn": datetime.now(CN).isoformat(),
        "runner_args": runner_args,
        "archived_stale_artifacts": str(archive_dir) if archive_dir else "",
    }
    (root / "runner.launchd.meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return meta


def start(args: argparse.Namespace) -> dict[str, Any]:
    meta = write_start_files(args)
    if args.dry_run:
        return {"ok": True, "dry_run": True, "action": "would_start", "meta": meta}
    label = meta["label"]
    plist = meta["plist"]
    run(["launchctl", "bootout", launch_domain(), plist])
    bootstrap = run(["launchctl", "bootstrap", launch_domain(), plist])
    kickstart = run(["launchctl", "kickstart", "-k", f"{launch_domain()}/{label}"])
    ok = bootstrap.returncode == 0 and kickstart.returncode == 0
    return {
        "ok": ok,
        "action": "started" if ok else "start_failed",
        "meta": meta,
        "bootstrap": {"returncode": bootstrap.returncode, "stdout": bootstrap.stdout, "stderr": bootstrap.stderr},
        "kickstart": {"returncode": kickstart.returncode, "stdout": kickstart.stdout, "stderr": kickstart.stderr},
    }


def status(args: argparse.Namespace) -> dict[str, Any]:
    labels = [label_for_run(args.run_id)] if args.run_id else labels_from_plists()
    items: list[dict[str, Any]] = []
    for label in labels:
        proc = run(["launchctl", "print", f"{launch_domain()}/{label}"])
        run_id = args.run_id or label.rsplit(".", 1)[-1].upper()
        items.append(
            {
                "label": label,
                "loaded": proc.returncode == 0,
                "launch": parse_launchctl_print(proc.stdout),
                "launch_error": proc.stderr.strip(),
                "artifacts": compact_artifacts(run_id),
            }
        )
    return {"ok": True, "items": items}


def stop(args: argparse.Namespace) -> dict[str, Any]:
    labels = [label_for_run(args.run_id)] if args.run_id else labels_from_plists()
    stopped: list[dict[str, Any]] = []
    for label in labels:
        plist = launch_agents_dir() / f"{label}.plist"
        if args.dry_run:
            stopped.append({"label": label, "action": "would_stop", "plist": str(plist)})
            continue
        proc = run(["launchctl", "bootout", launch_domain(), str(plist)])
        stderr = proc.stderr.strip()
        ok = proc.returncode == 0 or "Could not find service" in stderr or "No such process" in stderr
        stopped.append({"label": label, "ok": ok, "returncode": proc.returncode, "stdout": proc.stdout, "stderr": stderr, "plist": str(plist)})
    return {"ok": all(item.get("ok", True) for item in stopped), "stopped": stopped}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage the paper-only TV premarket linkage LaunchAgent.")
    sub = parser.add_subparsers(dest="action", required=True)

    start_p = sub.add_parser("start")
    start_p.add_argument("--run-id", default=default_run_id())
    start_p.add_argument("--target-completed-chains", type=int, default=20)
    start_p.add_argument("--target-notional-per-chain", type=float, default=5000.0)
    start_p.add_argument("--symbols", default="auto")
    start_p.add_argument("--exclude-symbols", default="SPY,QQQ,DIA,IWM,VIX,UVXY,SQQQ,TQQQ")
    start_p.add_argument("--marketable-bps", type=float, default=2.0)
    start_p.add_argument("--max-limit-slippage-bps", type=float, default=15.0)
    start_p.add_argument("--max-spread-bps", type=float, default=80.0)
    start_p.add_argument("--start-et", default="04:05")
    start_p.add_argument("--stop-new-entry-et", default="09:20")
    start_p.add_argument("--chain-spacing-seconds", type=float, default=1.0)
    start_p.add_argument("--poll-seconds", type=float, default=180.0)
    start_p.add_argument("--fill-timeout-seconds", type=float, default=180.0)
    start_p.add_argument("--cleanup-timeout-seconds", type=float, default=180.0)
    start_p.add_argument("--skip-health", action="store_true")
    start_p.add_argument("--allow-existing-paper-state", action="store_true")
    start_p.add_argument("--dry-run", action="store_true")

    status_p = sub.add_parser("status")
    status_p.add_argument("--run-id", default="")

    stop_p = sub.add_parser("stop")
    stop_p.add_argument("--run-id", default="")
    stop_p.add_argument("--dry-run", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.action == "start":
        payload = start(args)
    elif args.action == "status":
        payload = status(args)
    else:
        payload = stop(args)
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

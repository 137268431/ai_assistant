#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
CN = ZoneInfo("Asia/Shanghai")

REPO_ROOT = Path(__file__).resolve().parents[2]
RUN_SCRIPT = REPO_ROOT / "ops" / "validate" / "run_today_tv_replay_stress.py"
ARTIFACT_ROOT = REPO_ROOT / "artifacts" / "validation" / "tv_replay_stress"
LABEL_PREFIX = "com.lzwglory.ai-assistant.today-tv-replay-stress"
WATCHDOG_LABEL = f"{LABEL_PREFIX}.watchdog"
WATCHDOG_ROOT = ARTIFACT_ROOT / "watchdog"
DEFAULT_PYTHON = Path("/Users/lzwglory/miniforge3/bin/python3.12")


def safe_label_part(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9.-]+", "-", str(value or "").strip()).strip("-").lower()
    return text or "run"


def run(command: list[str], *, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=True, check=check)


def launch_domain() -> str:
    return f"gui/{os.getuid()}"


def launch_agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def python_executable() -> Path:
    return DEFAULT_PYTHON if DEFAULT_PYTHON.exists() else Path(sys.executable)


def label_for_run(run_id: str) -> str:
    return f"{LABEL_PREFIX}.{safe_label_part(run_id)}"


def parse_launchctl_print(text: str) -> dict[str, Any]:
    result: dict[str, Any] = {"raw_available": bool(text)}
    patterns = {
        "state": r"\bstate = ([^\n]+)",
        "pid": r"\bpid = (\d+)",
        "runs": r"\bruns = (\d+)",
        "last_exit_code": r"\blast exit code = ([^\n]+)",
        "path": r"\bpath = ([^\n]+)",
    }
    for key, pattern in patterns.items():
        match = re.search(pattern, text)
        if match:
            value: Any = match.group(1).strip()
            if key in {"pid", "runs"}:
                try:
                    value = int(value)
                except Exception:
                    pass
            result[key] = value
    return result


def known_plists() -> list[Path]:
    root = launch_agents_dir()
    if not root.exists():
        return []
    return sorted(root.glob(f"{LABEL_PREFIX}*.plist"))


def labels_from_plists() -> list[str]:
    labels: list[str] = []
    for path in known_plists():
        try:
            data = plistlib.loads(path.read_bytes())
        except Exception:
            continue
        label = str(data.get("Label") or "").strip()
        if label.startswith(LABEL_PREFIX) and label != WATCHDOG_LABEL:
            labels.append(label)
    return sorted(set(labels))


def read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def meta_for_label(label: str) -> dict[str, Any]:
    for root in sorted(ARTIFACT_ROOT.glob("SIMTV_AUTO_*"), reverse=True):
        meta = read_json(root / "runner.launchd.meta.json")
        if meta.get("label") == label:
            meta.setdefault("artifact_dir", str(root))
            return meta
    return {}


def artifact_sort_key(path: Path) -> tuple[float, str]:
    try:
        mtime = path.stat().st_mtime
    except Exception:
        mtime = 0.0
    return (mtime, path.name)


def latest_terminal_artifact_item() -> dict[str, Any]:
    roots = sorted((path for path in ARTIFACT_ROOT.glob("SIMTV_AUTO_*") if path.is_dir()), key=artifact_sort_key, reverse=True)
    for root in roots:
        summary = read_json(root / "retry_summary.json")
        classification = classify_retry_summary(summary, launch_state="")
        if not classification.get("terminal"):
            continue
        meta = read_json(root / "runner.launchd.meta.json")
        meta.setdefault("artifact_dir", str(root))
        return {
            "label": meta.get("label") or root.name,
            "loaded": False,
            "launch": {"state": "artifact_terminal"},
            "meta": meta,
            "retry_summary": compact_retry_summary(root, launch={"state": "artifact_terminal"}),
        }
    return {}


def parse_iso_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text).astimezone(ET)
    except Exception:
        return None


def classify_retry_summary(data: dict[str, Any], *, launch_state: str = "", now_et: datetime | None = None) -> dict[str, Any]:
    if not data:
        return {
            "phase": "no_summary",
            "terminal": False,
            "goal_complete": False,
            "abandoned": False,
            "stale": True,
        }
    reason = str(data.get("reason") or "").strip()
    attempts = data.get("attempts") if isinstance(data.get("attempts"), list) else []
    ok = bool(data.get("ok"))
    terminal = reason in {"stable_success_reached", "market_end_reached"}
    goal_complete = ok and reason == "stable_success_reached"
    abandoned = (not ok) and reason == "market_end_reached"
    if goal_complete:
        phase = "success"
    elif abandoned:
        phase = "abandoned_market_end"
    elif reason == "waiting_market_window":
        phase = "waiting_market_window"
    elif reason == "retrying_until_market_end":
        phase = "retrying_intraday"
    elif attempts:
        phase = "attempts_recorded"
    elif reason:
        phase = reason
    else:
        phase = "unknown"

    updated_at = parse_iso_datetime(data.get("updated_at_et"))
    current = now_et.astimezone(ET) if now_et else datetime.now(ET)
    age_seconds: float | None = None
    stale = False
    if updated_at:
        age_seconds = max(0.0, (current - updated_at).total_seconds())
        stale = bool(not terminal and str(launch_state or "").strip() == "running" and age_seconds > 180.0)
    elif str(launch_state or "").strip() == "running":
        stale = not terminal
    return {
        "phase": phase,
        "terminal": terminal,
        "goal_complete": goal_complete,
        "abandoned": abandoned,
        "stale": stale,
        "age_seconds": round(age_seconds, 3) if age_seconds is not None else None,
    }


def compact_retry_summary(artifact_dir: str | Path, *, launch: dict[str, Any] | None = None) -> dict[str, Any]:
    path = Path(artifact_dir) / "retry_summary.json"
    data = read_json(path)
    if not data:
        return {"available": False, "path": str(path)}
    wait = data.get("market_window_wait") if isinstance(data.get("market_window_wait"), dict) else {}
    attempts = data.get("attempts") if isinstance(data.get("attempts"), list) else []
    classification = classify_retry_summary(data, launch_state=str((launch or {}).get("state") or ""))
    return {
        "available": True,
        "path": str(path),
        **classification,
        "ok": data.get("ok"),
        "reason": data.get("reason"),
        "updated_at_et": data.get("updated_at_et"),
        "attempts": len(attempts),
        "stable_success_runs": data.get("stable_success_runs"),
        "market_start_et": data.get("market_start_et"),
        "market_end_et": data.get("market_end_et"),
        "window_start_et": wait.get("window_start_et"),
        "window_end_et": wait.get("window_end_et"),
        "wait_event_count": len(wait.get("wait_events") or []),
        "last_attempt": attempts[-1] if attempts else {},
    }


def runner_item_mtime(item: dict[str, Any]) -> float:
    meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
    retry = item.get("retry_summary") if isinstance(item.get("retry_summary"), dict) else {}
    updated_at = parse_iso_datetime(retry.get("updated_at_et"))
    if updated_at:
        return updated_at.timestamp()
    artifact_dir = str(meta.get("artifact_dir") or "").strip()
    if artifact_dir:
        try:
            return Path(artifact_dir).stat().st_mtime
        except Exception:
            pass
    return 0.0


def runner_item_priority(item: dict[str, Any]) -> tuple[int, float, str]:
    launch = item.get("launch") if isinstance(item.get("launch"), dict) else {}
    retry = item.get("retry_summary") if isinstance(item.get("retry_summary"), dict) else {}
    loaded = bool(item.get("loaded"))
    state = str(launch.get("state") or "")
    if loaded and state == "running" and not retry.get("stale"):
        priority = 4
    elif loaded and state == "running":
        priority = 3
    elif retry.get("terminal") or retry.get("goal_complete") or retry.get("abandoned"):
        priority = 2
    elif retry.get("available"):
        priority = 1
    else:
        priority = 0
    return (priority, runner_item_mtime(item), str(item.get("label") or ""))


def select_current_runner_item(items: list[dict[str, Any]]) -> dict[str, Any]:
    candidates = [item for item in items if isinstance(item, dict)]
    if not candidates:
        return {}
    return max(candidates, key=runner_item_priority)


def resolve_artifact_path(value: Any, *, base_dir: Path) -> Path:
    text = str(value or "").strip()
    if not text:
        return Path("")
    path = Path(text)
    if path.is_absolute():
        return path
    for candidate in (REPO_ROOT / path, base_dir / path):
        if candidate.exists():
            return candidate
    return REPO_ROOT / path


def add_check(checks: list[dict[str, Any]], name: str, ok: bool, **extra: Any) -> None:
    item = {"name": name, "ok": bool(ok)}
    item.update(extra)
    checks.append(item)


def load_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return read_json(path)


def audit_success_attempt(base_dir: Path, retry_summary: dict[str, Any], checks: list[dict[str, Any]]) -> dict[str, Any]:
    attempts = retry_summary.get("attempts") if isinstance(retry_summary.get("attempts"), list) else []
    success_attempt = next((item for item in reversed(attempts) if isinstance(item, dict) and item.get("ok")), {})
    add_check(checks, "success_attempt_found", bool(success_attempt), attempts=len(attempts))
    if not success_attempt:
        return {}

    attempt_artifact_dir = resolve_artifact_path(success_attempt.get("artifact_dir"), base_dir=base_dir)
    add_check(checks, "success_attempt_artifact_dir_exists", bool(attempt_artifact_dir and attempt_artifact_dir.exists()), path=str(attempt_artifact_dir))
    if not attempt_artifact_dir or not attempt_artifact_dir.exists():
        return {"success_attempt": success_attempt, "attempt_artifact_dir": str(attempt_artifact_dir)}

    summary = load_json_file(attempt_artifact_dir / "summary.json")
    health = load_json_file(attempt_artifact_dir / "health_and_metrics.json")
    chains_path = attempt_artifact_dir / "chains.jsonl"
    chain_lines = chains_path.read_text(encoding="utf-8").splitlines() if chains_path.exists() else []
    chain_reports: list[dict[str, Any]] = []
    for line in chain_lines:
        try:
            row = json.loads(line)
            if isinstance(row, dict):
                chain_reports.append(row)
        except Exception:
            pass

    add_check(checks, "success_attempt_summary_exists", bool(summary), path=str(attempt_artifact_dir / "summary.json"))
    add_check(checks, "success_attempt_summary_ok", bool(summary.get("ok")), summary_ok=summary.get("ok"))
    add_check(checks, "success_attempt_health_artifact_exists", bool(health), path=str(attempt_artifact_dir / "health_and_metrics.json"))
    add_check(checks, "success_attempt_chains_artifact_exists", bool(chain_reports), path=str(chains_path), chain_reports=len(chain_reports))

    flow = summary.get("flow_requirements") if isinstance(summary.get("flow_requirements"), dict) else {}
    account = summary.get("account_flat") if isinstance(summary.get("account_flat"), dict) else {}
    stability = summary.get("stability") if isinstance(summary.get("stability"), dict) else {}
    burst = summary.get("burst_results") if isinstance(summary.get("burst_results"), dict) else {}
    cleanup = summary.get("cleanup_results") if isinstance(summary.get("cleanup_results"), dict) else {}
    post_cleanup = summary.get("post_cleanup_results") if isinstance(summary.get("post_cleanup_results"), dict) else {}
    chains = summary.get("chains") if isinstance(summary.get("chains"), list) else []
    flow_counts = flow.get("counts") if isinstance(flow.get("counts"), dict) else {}
    flow_thresholds = flow.get("thresholds") if isinstance(flow.get("thresholds"), dict) else {}
    selected_chains = int(summary.get("selected_chains") or flow_counts.get("selected_chains") or len(chains) or 0)
    selected_full_chains = int(summary.get("selected_full_chains") or flow_counts.get("full_chains") or 0)

    add_check(checks, "multi_signal_selected_chains", selected_chains >= 3, selected_chains=selected_chains, required=3)
    add_check(checks, "multi_signal_full_chains", selected_full_chains >= 3, selected_full_chains=selected_full_chains, required=3)
    add_check(
        checks,
        "followup_concurrent_stress_enabled",
        bool(summary.get("followup_stress_concurrent"))
        and int(summary.get("risk_burst_workers") or 0) >= 3
        and int(summary.get("exit_burst_workers") or 0) >= 3,
        followup_stress_concurrent=summary.get("followup_stress_concurrent"),
        risk_burst_workers=summary.get("risk_burst_workers"),
        exit_burst_workers=summary.get("exit_burst_workers"),
    )
    add_check(
        checks,
        "chain_reports_cover_selected_chains",
        bool(chain_reports) and len(chain_reports) >= selected_chains >= 3,
        chain_reports=len(chain_reports),
        selected_chains=selected_chains,
    )
    add_check(checks, "flow_requirements_ok", bool(flow.get("ok")), failures=flow.get("failures") or [], counts=flow.get("counts") or {})
    for count_name, threshold_name in (
        ("bracket_chains", "min_bracket_chains"),
        ("filled_entry_chains", "min_filled_entry_chains"),
        ("routed_exit_chains", "min_routed_exit_chains"),
        ("closed_reverse_chains", "min_closed_reverse_chains"),
    ):
        threshold = int(flow_thresholds.get(threshold_name) or 0)
        if threshold <= 0:
            continue
        value = int(flow_counts.get(count_name) or 0)
        add_check(checks, f"flow_{count_name}_meets_threshold", value >= threshold, value=value, threshold=threshold)
    add_check(checks, "account_flat_before_ok", bool((account.get("before") or {}).get("ok")), details=account.get("before") or {})
    add_check(checks, "account_flat_after_ok", bool((account.get("after") or {}).get("ok")), details=account.get("after") or {})
    add_check(checks, "stability_before_ok", bool((stability.get("before") or {}).get("ok")), details=stability.get("before") or {})
    add_check(checks, "stability_after_ok", bool((stability.get("after") or {}).get("ok")), details=stability.get("after") or {})
    add_check(checks, "burst_no_failed", int(burst.get("failed") or 0) == 0 and int(burst.get("total") or 0) > 0, burst_results=burst)
    add_check(checks, "cleanup_no_failed", int(cleanup.get("failed") or 0) == 0, cleanup_results=cleanup)
    add_check(checks, "post_cleanup_no_failed", int(post_cleanup.get("failed") or 0) == 0, post_cleanup_results=post_cleanup)

    failed_chain_checks: list[dict[str, Any]] = []
    for chain in chains:
        if not isinstance(chain, dict):
            continue
        for check in chain.get("checks") or []:
            if isinstance(check, dict) and check.get("ok") is False:
                failed_chain_checks.append(
                    {
                        "signal_id": chain.get("synthetic_signal_id"),
                        "symbol": chain.get("symbol"),
                        "check": check.get("name"),
                        "reason": check.get("reason") or check.get("error") or check.get("error_msg"),
                    }
                )
    add_check(checks, "chain_checks_no_failures", not failed_chain_checks and bool(chains), failures=failed_chain_checks[:20], chain_count=len(chains))

    return {
        "success_attempt": success_attempt,
        "attempt_artifact_dir": str(attempt_artifact_dir),
        "attempt_summary": {
            "run_id": summary.get("run_id"),
            "market_date": summary.get("market_date"),
            "selected_chains": summary.get("selected_chains"),
            "selected_full_chains": summary.get("selected_full_chains"),
            "followup_stress_concurrent": summary.get("followup_stress_concurrent"),
            "risk_burst_workers": summary.get("risk_burst_workers"),
            "exit_burst_workers": summary.get("exit_burst_workers"),
            "classifications": summary.get("classifications"),
            "flow_counts": flow_counts,
            "flow_thresholds": flow_thresholds,
            "burst_results": burst,
            "cleanup_results": cleanup,
            "post_cleanup_results": post_cleanup,
            "account_flat_before_ok": bool((account.get("before") or {}).get("ok")),
            "account_flat_after_ok": bool((account.get("after") or {}).get("ok")),
            "stability_before_ok": bool((stability.get("before") or {}).get("ok")),
            "stability_after_ok": bool((stability.get("after") or {}).get("ok")),
        },
    }


def build_evidence_summary(audit_payload: dict[str, Any]) -> dict[str, Any]:
    retry = audit_payload.get("retry_summary") if isinstance(audit_payload.get("retry_summary"), dict) else {}
    attempt = audit_payload.get("attempt_summary") if isinstance(audit_payload.get("attempt_summary"), dict) else {}
    checks = audit_payload.get("checks") if isinstance(audit_payload.get("checks"), list) else []
    return {
        "objective_status": audit_payload.get("objective_status"),
        "outcome": audit_payload.get("outcome"),
        "phase": audit_payload.get("phase"),
        "terminal": audit_payload.get("terminal"),
        "goal_complete": audit_payload.get("goal_complete"),
        "abandoned": audit_payload.get("abandoned"),
        "retry_reason": retry.get("reason"),
        "retry_attempts": retry.get("attempts"),
        "attempt_run_id": attempt.get("run_id"),
        "market_date": attempt.get("market_date"),
        "selected_chains": attempt.get("selected_chains"),
        "selected_full_chains": attempt.get("selected_full_chains"),
        "followup_stress_concurrent": attempt.get("followup_stress_concurrent"),
        "risk_burst_workers": attempt.get("risk_burst_workers"),
        "exit_burst_workers": attempt.get("exit_burst_workers"),
        "flow_counts": attempt.get("flow_counts") or {},
        "flow_thresholds": attempt.get("flow_thresholds") or {},
        "burst_results": attempt.get("burst_results") or {},
        "cleanup_results": attempt.get("cleanup_results") or {},
        "post_cleanup_results": attempt.get("post_cleanup_results") or {},
        "account_flat_before_ok": attempt.get("account_flat_before_ok"),
        "account_flat_after_ok": attempt.get("account_flat_after_ok"),
        "stability_before_ok": attempt.get("stability_before_ok"),
        "stability_after_ok": attempt.get("stability_after_ok"),
        "failed_checks": [item.get("name") for item in checks if isinstance(item, dict) and not item.get("ok")],
    }


def audit_retry_artifacts(artifact_dir: str | Path, *, launch: dict[str, Any] | None = None) -> dict[str, Any]:
    base_dir = Path(artifact_dir)
    summary_path = base_dir / "retry_summary.json"
    retry_summary = read_json(summary_path)
    launch_state = str((launch or {}).get("state") or "")
    classification = classify_retry_summary(retry_summary, launch_state=launch_state)
    checks: list[dict[str, Any]] = []
    add_check(checks, "retry_summary_exists", bool(retry_summary), path=str(summary_path))
    if retry_summary:
        add_check(checks, "retry_summary_not_stale", not classification.get("stale"), updated_at_et=retry_summary.get("updated_at_et"))
    outcome = "pending"
    details: dict[str, Any] = {}
    if classification.get("goal_complete"):
        add_check(checks, "retry_success_reason", retry_summary.get("ok") is True and retry_summary.get("reason") == "stable_success_reached")
        details = audit_success_attempt(base_dir, retry_summary, checks)
        outcome = "success" if all(item.get("ok") for item in checks) else "attention"
    elif classification.get("abandoned"):
        add_check(checks, "retry_abandoned_reason", retry_summary.get("ok") is False and retry_summary.get("reason") == "market_end_reached")
        outcome = "abandoned" if all(item.get("ok") for item in checks) else "attention"
    elif not retry_summary:
        outcome = "missing"
    elif classification.get("stale"):
        outcome = "stale"
    elif classification.get("phase") in {"waiting_market_window", "retrying_intraday", "attempts_recorded"}:
        outcome = "pending"
    else:
        outcome = "attention"

    audit_ok = all(item.get("ok") for item in checks) if checks else False
    payload = {
        "ok": audit_ok,
        "outcome": outcome,
        "objective_status": "complete" if outcome == "success" else "abandoned" if outcome == "abandoned" else "pending",
        "goal_complete": outcome == "success",
        "abandoned": outcome == "abandoned",
        "phase": classification.get("phase"),
        "terminal": classification.get("terminal"),
        "artifact_dir": str(base_dir),
        "retry_summary": compact_retry_summary(base_dir, launch=launch or {}),
        "checks": checks,
        **details,
    }
    payload["evidence_summary"] = build_evidence_summary(payload)
    return payload


def status_payload() -> dict[str, Any]:
    labels = labels_from_plists()
    items: list[dict[str, Any]] = []
    for label in labels:
        proc = run(["launchctl", "print", f"{launch_domain()}/{label}"])
        launch = parse_launchctl_print(proc.stdout if proc.returncode == 0 else proc.stderr)
        meta = meta_for_label(label)
        artifact_dir = meta.get("artifact_dir") or ""
        items.append(
            {
                "label": label,
                "loaded": proc.returncode == 0,
                "launch": launch,
                "meta": meta,
                "retry_summary": compact_retry_summary(artifact_dir, launch=launch) if artifact_dir else {},
            }
        )
    return {
        "ok": True,
        "labels": labels,
        "count": len(items),
        "running_count": sum(1 for item in items if item.get("loaded") and item.get("launch", {}).get("state") == "running"),
        "items": items,
    }


def ensure_decision(status: dict[str, Any], *, latest_terminal_item: dict[str, Any] | None = None) -> dict[str, Any]:
    items = status.get("items") if isinstance(status.get("items"), list) else []
    healthy_running: list[dict[str, Any]] = []
    terminal: list[dict[str, Any]] = []
    stale_running: list[dict[str, Any]] = []
    for item in items:
        launch = item.get("launch") if isinstance(item.get("launch"), dict) else {}
        retry = item.get("retry_summary") if isinstance(item.get("retry_summary"), dict) else {}
        loaded = bool(item.get("loaded"))
        state = str(launch.get("state") or "")
        if retry.get("goal_complete") or retry.get("abandoned"):
            terminal.append(item)
            continue
        if loaded and state == "running" and not retry.get("stale"):
            healthy_running.append(item)
            continue
        if loaded and state == "running" and retry.get("stale"):
            stale_running.append(item)
    if healthy_running:
        return {"action": "already_running", "ok": True, "items": healthy_running}
    if terminal:
        return {"action": "terminal_no_restart", "ok": True, "items": terminal}
    if latest_terminal_item:
        return {"action": "terminal_no_restart", "ok": True, "items": [latest_terminal_item], "source": "latest_terminal_artifact"}
    if stale_running:
        return {"action": "stale_running_requires_force_restart", "ok": False, "items": stale_running}
    return {"action": "start_required", "ok": True, "items": items}


def default_run_id() -> str:
    return "SIMTV_AUTO_" + datetime.now(ET).strftime("%Y%m%d_%H%M%S_ET") + "_LA"


def watchdog_plist_path() -> Path:
    return launch_agents_dir() / f"{WATCHDOG_LABEL}.plist"


def build_watchdog_plist(interval_seconds: int, *, stdout_path: Path | None = None, stderr_path: Path | None = None) -> dict[str, Any]:
    interval = max(30, int(interval_seconds or 60))
    out_path = stdout_path or WATCHDOG_ROOT / "watchdog.out.log"
    err_path = stderr_path or WATCHDOG_ROOT / "watchdog.err.log"
    return {
        "Label": WATCHDOG_LABEL,
        "ProgramArguments": [
            str(python_executable()),
            str(Path(__file__).resolve()),
            "ensure",
            "--text",
        ],
        "WorkingDirectory": str(REPO_ROOT),
        "RunAtLoad": True,
        "StartInterval": interval,
        "StandardOutPath": str(out_path),
        "StandardErrorPath": str(err_path),
    }


def write_watchdog_plist(interval_seconds: int) -> Path:
    WATCHDOG_ROOT.mkdir(parents=True, exist_ok=True)
    launch_agents_dir().mkdir(parents=True, exist_ok=True)
    plist_path = watchdog_plist_path()
    plist_path.write_bytes(plistlib.dumps(build_watchdog_plist(interval_seconds), sort_keys=False))
    return plist_path


def watchdog_status_payload() -> dict[str, Any]:
    proc = run(["launchctl", "print", f"{launch_domain()}/{WATCHDOG_LABEL}"])
    plist_data: dict[str, Any] = {}
    plist_path = watchdog_plist_path()
    if plist_path.exists():
        try:
            loaded = plistlib.loads(plist_path.read_bytes())
            if isinstance(loaded, dict):
                plist_data = loaded
        except Exception:
            plist_data = {}
    return {
        "ok": True,
        "kind": "watchdog",
        "action": "status",
        "label": WATCHDOG_LABEL,
        "loaded": proc.returncode == 0,
        "launch": parse_launchctl_print(proc.stdout if proc.returncode == 0 else proc.stderr),
        "plist": str(watchdog_plist_path()),
        "interval_seconds": plist_data.get("StartInterval"),
        "log_dir": str(WATCHDOG_ROOT),
        "stdout": proc.stdout if proc.returncode != 0 else "",
        "stderr": proc.stderr,
    }


def write_start_files(run_id: str) -> tuple[str, Path, Path, Path]:
    label = label_for_run(run_id)
    artifact_dir = ARTIFACT_ROOT / run_id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    wrapper = artifact_dir / "runner_launchd.sh"
    plist_path = launch_agents_dir() / f"{label}.plist"
    stdout_path = artifact_dir / "runner.launchd.out.log"
    stderr_path = artifact_dir / "runner.launchd.err.log"
    python_bin = str(python_executable())

    wrapper.write_text(
        "\n".join(
            [
                "#!/bin/zsh",
                "set -euo pipefail",
                f"cd {json.dumps(str(REPO_ROOT))}",
                "echo runner_started_at_cn=$(TZ=Asia/Shanghai date '+%Y-%m-%d %H:%M:%S %Z')",
                "echo runner_started_at_et=$(TZ=America/New_York date '+%Y-%m-%d %H:%M:%S %Z')",
                (
                    f"exec {json.dumps(python_bin)} "
                    f"{json.dumps(str(RUN_SCRIPT.relative_to(REPO_ROOT)))} "
                    f"--strict-canary --format json --run-id {json.dumps(run_id)}"
                ),
                "",
            ]
        ),
        encoding="utf-8",
    )
    wrapper.chmod(0o755)

    launch_agents_dir().mkdir(parents=True, exist_ok=True)
    plist = {
        "Label": label,
        "ProgramArguments": ["/bin/zsh", str(wrapper)],
        "WorkingDirectory": str(REPO_ROOT),
        "RunAtLoad": True,
        "StandardOutPath": str(stdout_path),
        "StandardErrorPath": str(stderr_path),
    }
    plist_path.write_bytes(plistlib.dumps(plist, sort_keys=False))

    meta = {
        "run_id": run_id,
        "label": label,
        "plist": str(plist_path),
        "wrapper": str(wrapper),
        "artifact_dir": str(artifact_dir),
        "created_at_cn": datetime.now(CN).strftime("%Y-%m-%d %H:%M:%S %Z"),
        "created_at_et": datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S %Z"),
    }
    (artifact_dir / "runner.launchd.meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return label, artifact_dir, plist_path, wrapper


def start(args: argparse.Namespace) -> dict[str, Any]:
    current = status_payload()
    running = [item for item in current["items"] if item.get("loaded") and item.get("launch", {}).get("state") == "running"]
    if running and not args.allow_multiple:
        return {"ok": False, "error": "runner_already_running", "running": running}

    run_id = args.run_id or default_run_id()
    label, artifact_dir, plist_path, wrapper = write_start_files(run_id)
    proc = run(["launchctl", "bootstrap", launch_domain(), str(plist_path)])
    if proc.returncode != 0:
        return {
            "ok": False,
            "error": "launchctl_bootstrap_failed",
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "label": label,
            "plist": str(plist_path),
        }
    return {
        "ok": True,
        "run_id": run_id,
        "label": label,
        "artifact_dir": str(artifact_dir),
        "plist": str(plist_path),
        "wrapper": str(wrapper),
    }


def stop(args: argparse.Namespace) -> dict[str, Any]:
    labels = [args.label] if args.label else labels_from_plists()
    if not labels:
        return {"ok": True, "stopped": [], "reason": "no_known_runner"}
    stopped: list[dict[str, Any]] = []
    for label in labels:
        meta = meta_for_label(label)
        plist = Path(meta.get("plist") or launch_agents_dir() / f"{label}.plist")
        proc = run(["launchctl", "bootout", launch_domain(), str(plist)])
        stopped.append(
            {
                "label": label,
                "plist": str(plist),
                "ok": proc.returncode == 0,
                "returncode": proc.returncode,
                "stdout": proc.stdout,
                "stderr": proc.stderr,
            }
        )
    return {"ok": all(item["ok"] for item in stopped), "stopped": stopped}


def stop_known_runners() -> dict[str, Any]:
    args = argparse.Namespace(label="")
    return stop(args)


def audit(args: argparse.Namespace) -> dict[str, Any]:
    if args.artifact_dir:
        return audit_retry_artifacts(args.artifact_dir)
    status = status_payload()
    items = status.get("items") if isinstance(status.get("items"), list) else []
    if not items:
        terminal_item = latest_terminal_artifact_item()
        if terminal_item:
            artifact_dir = (terminal_item.get("meta") or {}).get("artifact_dir") or ""
            payload = audit_retry_artifacts(artifact_dir, launch=terminal_item.get("launch") or {})
            payload["label"] = terminal_item.get("label")
            payload["source"] = "latest_terminal_artifact"
            return payload
        return {"ok": False, "outcome": "missing", "error": "no_known_runner"}
    item = select_current_runner_item(items)
    artifact_dir = (item.get("meta") or {}).get("artifact_dir") or ""
    if not artifact_dir:
        return {"ok": False, "outcome": "missing", "error": "runner_artifact_dir_not_found", "label": item.get("label")}
    payload = audit_retry_artifacts(artifact_dir, launch=item.get("launch") or {})
    payload["label"] = item.get("label")
    return payload


def finalize(args: argparse.Namespace) -> dict[str, Any]:
    audit_payload = audit(argparse.Namespace(artifact_dir=getattr(args, "artifact_dir", "")))
    if not audit_payload.get("terminal") and not getattr(args, "force", False):
        return {
            "ok": False,
            "kind": "finalize",
            "action": "not_terminal_noop",
            "error": "audit_not_terminal_use_force_to_stop_anyway",
            "audit": audit_payload,
        }
    if getattr(args, "dry_run", False):
        return {
            "ok": True,
            "kind": "finalize",
            "action": "would_finalize",
            "audit": audit_payload,
        }
    stopped_runners = stop_known_runners()
    stopped_watchdog = watchdog_stop(argparse.Namespace())
    marker_path = ""
    artifact_dir = str(audit_payload.get("artifact_dir") or "")
    if artifact_dir:
        marker = Path(artifact_dir) / "finalize.json"
        marker.write_text(
            json.dumps(
                {
                    "finalized_at_et": datetime.now(ET).isoformat(),
                    "audit_outcome": audit_payload.get("outcome"),
                    "objective_status": audit_payload.get("objective_status"),
                    "stopped_runners": stopped_runners,
                    "stopped_watchdog": stopped_watchdog,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        marker_path = str(marker)
    return {
        "ok": bool(stopped_runners.get("ok")) and bool(stopped_watchdog.get("ok")),
        "kind": "finalize",
        "action": "finalized",
        "audit": audit_payload,
        "stopped_runners": stopped_runners,
        "stopped_watchdog": stopped_watchdog,
        "marker": marker_path,
    }


def ensure(args: argparse.Namespace) -> dict[str, Any]:
    status = status_payload()
    latest_terminal = latest_terminal_artifact_item()
    decision = ensure_decision(status, latest_terminal_item=latest_terminal)
    action = decision.get("action")
    if action in {"already_running", "terminal_no_restart"}:
        return {
            "ok": True,
            "action": action,
            "status": status,
            "decision": decision,
        }
    if action == "stale_running_requires_force_restart" and not getattr(args, "force_restart", False):
        return {
            "ok": False,
            "action": action,
            "error": "runner_stale_but_still_running_use_force_restart",
            "status": status,
            "decision": decision,
        }
    if getattr(args, "dry_run", False):
        return {
            "ok": True,
            "action": "would_restart" if action == "stale_running_requires_force_restart" else "would_start",
            "status": status,
            "decision": decision,
        }
    stopped: dict[str, Any] = {}
    if action == "stale_running_requires_force_restart":
        stopped = stop_known_runners()
        if not stopped.get("ok"):
            return {
                "ok": False,
                "action": "force_restart_stop_failed",
                "stopped": stopped,
                "status": status,
                "decision": decision,
            }
    start_args = argparse.Namespace(run_id=getattr(args, "run_id", ""), allow_multiple=False)
    started = start(start_args)
    return {
        "ok": bool(started.get("ok")),
        "action": "force_restarted" if stopped else "started",
        "stopped": stopped,
        "started": started,
        "previous_status": status,
        "decision": decision,
    }


def readiness_from_components(
    *,
    status: dict[str, Any],
    watchdog: dict[str, Any],
    audit_payload: dict[str, Any],
    replay_readiness: dict[str, Any] | None = None,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    add_check(checks, "watchdog_loaded", bool(watchdog.get("loaded")), label=watchdog.get("label"))
    add_check(
        checks,
        "single_runner_running",
        int(status.get("running_count") or 0) == 1,
        running_count=status.get("running_count"),
        count=status.get("count"),
    )
    add_check(
        checks,
        "runner_audit_healthy_or_pending",
        bool(audit_payload.get("ok")) and audit_payload.get("outcome") in {"pending", "success", "abandoned"},
        outcome=audit_payload.get("outcome"),
        objective_status=audit_payload.get("objective_status"),
    )
    if replay_readiness is not None:
        add_check(checks, "replay_readiness_ok", bool(replay_readiness.get("ok")), failures=replay_readiness.get("failures") or [])
    return {
        "ok": all(item.get("ok") for item in checks),
        "checks": checks,
        "status": status,
        "watchdog": watchdog,
        "audit": audit_payload,
        "replay_readiness": replay_readiness or {},
    }


def collect_replay_readiness(*, retries: int = 2, retry_sleep_seconds: float = 2.0) -> dict[str, Any]:
    import run_today_tv_replay_stress as replay  # Local import keeps status-only commands lightweight.

    args = replay.parse_args(["--strict-canary", "--dry-run", "--preview-payloads", "0"])
    chains, _excluded, summary = replay.build_replay_plan(args)
    account = replay.verify_account_flat_for_chains(args, chains, phase="readiness")
    attempts: list[dict[str, Any]] = []
    health: dict[str, Any] = {}
    stability: dict[str, Any] = {"ok": False, "failures": [{"name": "stability_readiness", "reason": "not_run"}]}
    for attempt_no in range(1, max(1, int(retries or 1)) + 1):
        health = replay.phase0_health(args)
        stability = replay.evaluate_stability(args, health, "readiness")
        attempts.append({"attempt": attempt_no, "ok": bool(stability.get("ok")), "failures": stability.get("failures") or []})
        if stability.get("ok"):
            break
        if attempt_no < max(1, int(retries or 1)):
            time.sleep(max(0.0, float(retry_sleep_seconds or 0.0)))

    min_selected = int(getattr(args, "min_selected_chains", 0) or 0)
    min_full = int(getattr(args, "min_full_chains", 0) or 0)
    checks: list[dict[str, Any]] = []
    add_check(checks, "plan_selected_chains", int(summary.get("selected_chains") or 0) >= min_selected, value=summary.get("selected_chains"), threshold=min_selected)
    add_check(checks, "plan_selected_full_chains", int(summary.get("selected_full_chains") or 0) >= min_full, value=summary.get("selected_full_chains"), threshold=min_full)
    add_check(
        checks,
        "plan_followup_concurrent_stress",
        bool(summary.get("followup_stress_concurrent"))
        and int(summary.get("risk_burst_workers") or 0) >= 3
        and int(summary.get("exit_burst_workers") or 0) >= 3,
        followup_stress_concurrent=summary.get("followup_stress_concurrent"),
        risk_burst_workers=summary.get("risk_burst_workers"),
        exit_burst_workers=summary.get("exit_burst_workers"),
    )
    add_check(checks, "account_flat_readiness", bool(account.get("ok")), failures=account.get("failures") or [], symbols=account.get("symbols") or [])
    add_check(checks, "stack_health_readiness", bool((health.get("stack") or {}).get("ok")), failures=(health.get("stack") or {}).get("failures") or [])
    add_check(checks, "monitoring_health_readiness", bool((health.get("monitoring") or {}).get("ok")), failures=(health.get("monitoring") or {}).get("failures") or [])
    add_check(checks, "stability_readiness", bool(stability.get("ok")), failures=stability.get("failures") or [], values=stability.get("values") or {})
    return {
        "ok": all(item.get("ok") for item in checks),
        "checks": checks,
        "summary": {
            "market_date": summary.get("market_date"),
            "selected_chains": summary.get("selected_chains"),
            "selected_full_chains": summary.get("selected_full_chains"),
            "followup_stress_concurrent": summary.get("followup_stress_concurrent"),
            "risk_burst_workers": summary.get("risk_burst_workers"),
            "exit_burst_workers": summary.get("exit_burst_workers"),
            "selected_symbols": [item.get("symbol") for item in summary.get("selected") or []],
            "exclude_reasons": summary.get("exclude_reasons") or {},
        },
        "account_flat": account,
        "stability": stability,
        "stability_attempts": attempts,
    }


def readiness(args: argparse.Namespace) -> dict[str, Any]:
    status = status_payload()
    watchdog = watchdog_status_payload()
    audit_payload = audit(argparse.Namespace(artifact_dir=""))
    replay_readiness = (
        None
        if getattr(args, "skip_replay_readiness", False)
        else collect_replay_readiness(retries=getattr(args, "readiness_retries", 2), retry_sleep_seconds=getattr(args, "readiness_retry_sleep_seconds", 2.0))
    )
    payload = readiness_from_components(
        status=status,
        watchdog=watchdog,
        audit_payload=audit_payload,
        replay_readiness=replay_readiness,
    )
    payload["kind"] = "readiness"
    return payload


def watchdog_start(args: argparse.Namespace) -> dict[str, Any]:
    current = watchdog_status_payload()
    if current.get("loaded"):
        return {"ok": True, "kind": "watchdog", "action": "already_loaded", "watchdog": current}
    plist_path = write_watchdog_plist(int(getattr(args, "interval_seconds", 60) or 60))
    if getattr(args, "dry_run", False):
        return {"ok": True, "kind": "watchdog", "action": "would_start", "plist": str(plist_path)}
    proc = run(["launchctl", "bootstrap", launch_domain(), str(plist_path)])
    return {
        "ok": proc.returncode == 0,
        "kind": "watchdog",
        "action": "started" if proc.returncode == 0 else "start_failed",
        "label": WATCHDOG_LABEL,
        "plist": str(plist_path),
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "status": watchdog_status_payload() if proc.returncode == 0 else current,
    }


def watchdog_stop(_args: argparse.Namespace) -> dict[str, Any]:
    plist_path = watchdog_plist_path()
    proc = run(["launchctl", "bootout", launch_domain(), str(plist_path)])
    already_unloaded = proc.returncode != 0 and "No such process" in (proc.stderr or proc.stdout)
    return {
        "ok": proc.returncode == 0 or already_unloaded,
        "kind": "watchdog",
        "action": "stopped" if proc.returncode == 0 else "already_unloaded" if already_unloaded else "stop_failed",
        "label": WATCHDOG_LABEL,
        "plist": str(plist_path),
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }


def print_payload(payload: dict[str, Any], *, text: bool = False) -> None:
    if not text:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    if "outcome" in payload:
        retry = payload.get("retry_summary") or {}
        print(
            "ok={ok} outcome={outcome} objective_status={objective_status} phase={phase} "
            "goal_complete={goal_complete} abandoned={abandoned} reason={reason} attempts={attempts}".format(
                ok=payload.get("ok"),
                outcome=payload.get("outcome"),
                objective_status=payload.get("objective_status"),
                phase=payload.get("phase"),
                goal_complete=payload.get("goal_complete"),
                abandoned=payload.get("abandoned"),
                reason=retry.get("reason"),
                attempts=retry.get("attempts"),
            )
        )
        failed = [item for item in payload.get("checks") or [] if not item.get("ok")]
        if failed:
            print("failed_checks=" + ",".join(str(item.get("name")) for item in failed))
        evidence = payload.get("evidence_summary") if isinstance(payload.get("evidence_summary"), dict) else {}
        if payload.get("outcome") in {"success", "abandoned"}:
            print(
                "evidence selected_chains={selected_chains} selected_full_chains={selected_full_chains} "
                "account_before={account_before} account_after={account_after} stability_before={stability_before} "
                "stability_after={stability_after}".format(
                    selected_chains=evidence.get("selected_chains"),
                    selected_full_chains=evidence.get("selected_full_chains"),
                    account_before=evidence.get("account_flat_before_ok"),
                    account_after=evidence.get("account_flat_after_ok"),
                    stability_before=evidence.get("stability_before_ok"),
                    stability_after=evidence.get("stability_after_ok"),
                )
            )
        return
    if payload.get("kind") == "watchdog":
        launch = (payload.get("status") or payload.get("watchdog") or payload).get("launch") or {}
        print(
            "ok={ok} action={action} label={label} loaded={loaded} state={state} pid={pid} interval={interval}".format(
                ok=payload.get("ok"),
                action=payload.get("action", ""),
                label=payload.get("label") or WATCHDOG_LABEL,
                loaded=(payload.get("status") or payload.get("watchdog") or payload).get("loaded"),
                state=launch.get("state"),
                pid=launch.get("pid"),
                interval=(payload.get("status") or payload.get("watchdog") or payload).get("interval_seconds") or "",
            )
        )
        return
    if payload.get("kind") == "readiness":
        replay = payload.get("replay_readiness") or {}
        summary = replay.get("summary") or {}
        failed = [item for item in payload.get("checks") or [] if not item.get("ok")]
        replay_failed = [item for item in replay.get("checks") or [] if not item.get("ok")]
        print(
            "ok={ok} runner_count={runner_count} watchdog_loaded={watchdog_loaded} audit_status={audit_status} "
            "selected_chains={selected_chains} selected_full_chains={selected_full_chains} failed_checks={failed_checks}".format(
                ok=payload.get("ok"),
                runner_count=(payload.get("status") or {}).get("running_count"),
                watchdog_loaded=(payload.get("watchdog") or {}).get("loaded"),
                audit_status=(payload.get("audit") or {}).get("objective_status"),
                selected_chains=summary.get("selected_chains", ""),
                selected_full_chains=summary.get("selected_full_chains", ""),
                failed_checks=",".join(str(item.get("name")) for item in [*failed, *replay_failed]),
            )
        )
        if summary.get("selected_symbols"):
            print("selected_symbols=" + ",".join(str(item) for item in summary.get("selected_symbols") or []))
        return
    if payload.get("kind") == "finalize":
        audit_payload = payload.get("audit") or {}
        print(
            "ok={ok} action={action} audit_outcome={audit_outcome} objective_status={objective_status} marker={marker} error={error}".format(
                ok=payload.get("ok"),
                action=payload.get("action"),
                audit_outcome=audit_payload.get("outcome"),
                objective_status=audit_payload.get("objective_status"),
                marker=payload.get("marker", ""),
                error=payload.get("error", ""),
            )
        )
        return
    if "action" in payload and "status" in payload:
        print(f"ok={payload.get('ok')} action={payload.get('action')} error={payload.get('error', '')}")
        status = payload.get("status") or payload.get("previous_status") or {}
        print(f"count={status.get('count', '')} running_count={status.get('running_count', '')}")
        return
    print(f"ok={payload.get('ok')} count={payload.get('count', '')} running_count={payload.get('running_count', '')}")
    for item in payload.get("items") or []:
        launch = item.get("launch") or {}
        retry = item.get("retry_summary") or {}
        print(
            (
                "label={label} loaded={loaded} state={state} pid={pid} phase={phase} reason={reason} "
                "attempts={attempts} stale={stale} updated_at_et={updated}"
            ).format(
                label=item.get("label"),
                loaded=item.get("loaded"),
                state=launch.get("state"),
                pid=launch.get("pid"),
                phase=retry.get("phase"),
                reason=retry.get("reason"),
                attempts=retry.get("attempts"),
                stale=retry.get("stale"),
                updated=retry.get("updated_at_et"),
            )
        )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage the macOS LaunchAgent for the strict today TV replay stress runner.")
    parser.add_argument(
        "action",
        choices=[
            "start",
            "status",
            "stop",
            "audit",
            "ensure",
            "readiness",
            "finalize",
            "watchdog-start",
            "watchdog-status",
            "watchdog-stop",
        ],
    )
    parser.add_argument("--run-id", default="")
    parser.add_argument("--label", default="", help="Only stop this label.")
    parser.add_argument("--artifact-dir", default="", help="Audit this replay artifact directory instead of the known runner.")
    parser.add_argument("--interval-seconds", type=int, default=60, help="For watchdog-start: ensure interval in seconds, minimum 30.")
    parser.add_argument("--allow-multiple", action="store_true", help="Allow starting another runner even if one is already loaded.")
    parser.add_argument("--force-restart", action="store_true", help="For ensure: stop a stale running LaunchAgent and start a fresh one.")
    parser.add_argument("--force", action="store_true", help="For finalize: stop runner/watchdog even when audit is not terminal.")
    parser.add_argument("--dry-run", action="store_true", help="For ensure: report what would be done without changing LaunchAgents.")
    parser.add_argument("--skip-replay-readiness", action="store_true", help="For readiness: skip dry-run plan, account, stack, monitoring, and stability checks.")
    parser.add_argument("--readiness-retries", type=int, default=2, help="For readiness: retry health/stability checks to avoid transient SSH/Prometheus false negatives.")
    parser.add_argument("--readiness-retry-sleep-seconds", type=float, default=2.0, help="For readiness: sleep between health/stability retries.")
    parser.add_argument("--text", action="store_true", help="Print compact text instead of JSON.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.action == "start":
        payload = start(args)
    elif args.action == "stop":
        payload = stop(args)
    elif args.action == "audit":
        payload = audit(args)
    elif args.action == "ensure":
        payload = ensure(args)
    elif args.action == "readiness":
        payload = readiness(args)
    elif args.action == "finalize":
        payload = finalize(args)
    elif args.action == "watchdog-start":
        payload = watchdog_start(args)
    elif args.action == "watchdog-status":
        payload = watchdog_status_payload()
    elif args.action == "watchdog-stop":
        payload = watchdog_stop(args)
    else:
        payload = status_payload()
    print_payload(payload, text=bool(args.text))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

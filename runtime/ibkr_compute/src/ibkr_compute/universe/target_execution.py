from __future__ import annotations

import json
from typing import Any


EXECUTION_TARGET_LAYER = "execution"
OBSERVE_TARGET_LAYER = "observe"
WATCH_ONLY_SETUP_TYPES = {"watch_only"}


def _to_text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return _to_text(value).lower() in {"1", "true", "yes", "y", "active", "passed", "pass"}


def parse_target_extra(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def normalize_execution_sides(value: Any) -> list[str]:
    if isinstance(value, str):
        raw_items = value.replace("\n", ",").split(",")
    elif isinstance(value, (list, tuple, set)):
        raw_items = list(value)
    else:
        raw_items = []
    sides: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        side = _to_text(item).lower()
        if side not in {"long", "short"} or side in seen:
            continue
        sides.append(side)
        seen.add(side)
    return sides


def _source_is_manual(extra: dict[str, Any]) -> bool:
    source = _to_text(extra.get("source")).lower()
    return source.startswith("manual_") or source in {
        "ibkr_screener",
        "manual_page",
        "manual_page_add",
        "manual_page_edit",
        "manual_page_remove",
        "screener_targets_tab",
    }


def _source_uses_active_gate(extra: dict[str, Any]) -> bool:
    return _to_text(extra.get("source")).lower() in {"daily_scan", "intraday_window_admission"}


def _data_quality_ready(extra: dict[str, Any], default: bool = True) -> bool:
    data_quality = extra.get("data_quality")
    if not isinstance(data_quality, dict):
        return default
    status = _to_text(data_quality.get("status")).lower()
    if status and status not in {"ready", "ok", "fresh"}:
        return False
    if _truthy(data_quality.get("needs_repair")):
        return False
    return True


def build_target_execution_metadata(
    extra: dict[str, Any] | None,
    *,
    direction_bias: Any = "",
    status: Any = "active",
    active_gate_passed: Any = None,
    data_quality_ready: bool | None = None,
) -> dict[str, Any]:
    payload = dict(extra or {})
    direction = _to_text(direction_bias or payload.get("direction_bias")).lower()
    target_status = _to_text(status).lower()

    strategy_policy = payload.get("strategy_policy") if isinstance(payload.get("strategy_policy"), dict) else {}
    strategy_sides = normalize_execution_sides(strategy_policy.get("allowed_sides"))
    execution_allowed_sides = strategy_sides or ([direction] if direction in {"long", "short"} else [])
    context_allowed_sides = normalize_execution_sides(
        payload.get("context_allowed_sides")
        or payload.get("allowed_sides")
        or payload.get("signal_pressure_sides")
    )

    if active_gate_passed is None:
        active_gate = (
            _truthy(payload.get("active_gate_passed"))
            or _truthy(payload.get("context_active"))
            or _truthy(payload.get("context_gate_passed"))
        )
        if not _source_uses_active_gate(payload) and not _source_is_manual(payload):
            active_gate = True
    else:
        active_gate = _truthy(active_gate_passed)

    if data_quality_ready is None:
        data_ready = _data_quality_ready(payload, default=True)
    else:
        data_ready = bool(data_quality_ready)

    setup_type = _to_text(strategy_policy.get("setup_type")).lower()
    blockers: list[str] = []
    if target_status != "active":
        blockers.append("target_not_active")
    if not active_gate:
        blockers.append("active_gate_not_passed")
    if not data_ready:
        blockers.append("data_quality_not_ready")
    if _truthy(strategy_policy.get("avoid_new_entries")):
        blockers.append("avoid_new_entries")
    if setup_type in WATCH_ONLY_SETUP_TYPES:
        blockers.append("watch_only")
    if direction not in {"long", "short"}:
        blockers.append("direction_missing")
    elif execution_allowed_sides and direction not in execution_allowed_sides:
        blockers.append("direction_not_allowed")

    seen: set[str] = set()
    normalized_blockers = []
    for blocker in blockers:
        if blocker and blocker not in seen:
            normalized_blockers.append(blocker)
            seen.add(blocker)

    execution_eligible = not normalized_blockers
    return {
        "execution_eligible": execution_eligible,
        "execution_blockers": normalized_blockers,
        "target_layer": EXECUTION_TARGET_LAYER if execution_eligible else OBSERVE_TARGET_LAYER,
        "execution_allowed_sides": execution_allowed_sides,
        "context_allowed_sides": context_allowed_sides,
    }


def apply_target_execution_metadata(
    extra: dict[str, Any] | None,
    *,
    direction_bias: Any = "",
    status: Any = "active",
    active_gate_passed: Any = None,
    data_quality_ready: bool | None = None,
) -> dict[str, Any]:
    payload = dict(extra or {})
    metadata = build_target_execution_metadata(
        payload,
        direction_bias=direction_bias,
        status=status,
        active_gate_passed=active_gate_passed,
        data_quality_ready=data_quality_ready,
    )
    payload.update(metadata)
    payload["allowed_sides"] = list(metadata["execution_allowed_sides"])
    return payload


def target_row_execution_metadata(row: dict[str, Any] | None) -> dict[str, Any]:
    row = row or {}
    extra = parse_target_extra(row.get("extra"))
    return build_target_execution_metadata(
        extra,
        direction_bias=row.get("direction_bias"),
        status=row.get("status"),
    )


def target_row_execution_eligible(row: dict[str, Any] | None) -> bool:
    return bool(target_row_execution_metadata(row).get("execution_eligible"))


__all__ = [
    "EXECUTION_TARGET_LAYER",
    "OBSERVE_TARGET_LAYER",
    "apply_target_execution_metadata",
    "build_target_execution_metadata",
    "normalize_execution_sides",
    "parse_target_extra",
    "target_row_execution_eligible",
    "target_row_execution_metadata",
]

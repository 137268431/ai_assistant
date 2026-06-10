from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from ibkr_compute.core.time_utils import ET
from ibkr_compute.observability.prometheus import set_trade_result_today_metrics


def trade_result_metrics_enabled(config: Any, environment: str, default: bool = True) -> bool:
    getter = getattr(config, "get_bool_for_environment", None)
    if callable(getter):
        try:
            return bool(getter("ibkr_trade_result_metrics_enabled", environment, default))
        except Exception:
            return bool(default)
    return bool(default)


def trade_result_metrics_interval_sec(config: Any, environment: str, default: float = 60.0) -> float:
    getter = getattr(config, "get_float_for_environment", None)
    if callable(getter):
        try:
            return max(10.0, float(getter("ibkr_trade_result_metrics_interval_sec", environment, default)))
        except Exception:
            return float(default)
    return float(default)


def _trade_result_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except Exception:
        return float(default)
    return number if number == number else float(default)


def _trade_result_truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return bool(value)
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _trade_result_extra(row: dict[str, Any]) -> dict[str, Any]:
    raw = (row or {}).get("raw")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def trade_result_group_key(row: dict[str, Any]) -> str:
    extra = _trade_result_extra(row)
    for source in (row, extra):
        if not isinstance(source, dict):
            continue
        for field in ("trade_group_id", "bracket_group", "oca_group", "order_ref", "order_id", "exec_id", "id"):
            value = str(source.get(field) or "").strip()
            if value:
                return value
    return ""


def build_trade_result_today_summary(rows: list[dict[str, Any]], *, environment: str) -> dict[str, Any]:
    groups: dict[str, dict[str, object]] = {}
    known_fill_count = 0
    unknown_fill_count = 0
    realized_pnl = 0.0
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        key = trade_result_group_key(row)
        if not key:
            continue
        known = _trade_result_truthy(row.get("realized_pnl_known"))
        group = groups.setdefault(key, {"known": False, "pnl": 0.0})
        if not known:
            unknown_fill_count += 1
            continue
        pnl = _trade_result_float(row.get("realized_pnl"), 0.0)
        known_fill_count += 1
        realized_pnl += pnl
        group["known"] = True
        group["pnl"] = _trade_result_float(group.get("pnl"), 0.0) + pnl

    result_counts = {"win": 0, "loss": 0, "flat": 0, "unknown": 0}
    for group in groups.values():
        if not bool(group.get("known")):
            result_counts["unknown"] += 1
            continue
        pnl = _trade_result_float(group.get("pnl"), 0.0)
        if pnl > 1e-9:
            result_counts["win"] += 1
        elif pnl < -1e-9:
            result_counts["loss"] += 1
        else:
            result_counts["flat"] += 1

    return {
        "environment": str(environment or "paper").strip().lower() or "paper",
        "basis": "realized",
        "grain": "trade_group",
        "realized_pnl": round(realized_pnl, 6),
        "result_counts": result_counts,
        "known_fill_count": known_fill_count,
        "unknown_fill_count": unknown_fill_count,
        "group_count": len(groups),
    }


def refresh_trade_result_today_metrics(
    pb: Any,
    *,
    environment: str,
    service_name: str | None = None,
    max_pages: int = 20,
) -> dict[str, Any]:
    env = str(environment or "paper").strip().lower() or "paper"
    getter = getattr(pb, "get_all_records", None)
    rows: list[dict[str, Any]] = []
    if callable(getter):
        now_et = datetime.now(ET)
        start_et = now_et.replace(hour=0, minute=0, second=0, microsecond=0)
        end_et = start_et + timedelta(days=1)
        start_ms = int(start_et.timestamp() * 1000)
        end_ms = int(end_et.timestamp() * 1000)
        filter_expr = (
            f'environment = "{env}" && '
            f"trade_time_ms >= {start_ms} && "
            f"trade_time_ms < {end_ms}"
        )
        raw_rows = getter(
            "ibkr_execution_fills",
            filter=filter_expr,
            sort="trade_time_ms",
            max_pages=max_pages,
        )
        rows = [row for row in raw_rows or [] if isinstance(row, dict)]

    summary = build_trade_result_today_summary(rows, environment=env)
    set_trade_result_today_metrics(
        summary,
        environment=env,
        service_name=service_name,
        grain="trade_group",
    )
    return summary


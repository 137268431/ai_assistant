"""Stage-aware activity gates for target universe admission."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from ibkr_compute.core.time_utils import ET
from ibkr_compute.market.timeframe_utils import REGULAR_OPEN_MINUTE

REGULAR_SESSION_MINUTES = 390.0

DEFAULT_ACTIVITY_GATE_STAGES = [
    {"id": "preopen_early", "start_et": "08:20", "end_et": "08:55", "any_of": {"premarket_volume_gte": 3000}},
    {"id": "preopen_final", "start_et": "09:00", "end_et": "09:25", "any_of": {"premarket_volume_gte": 5000}},
    {
        "id": "open_discovery",
        "start_et": "09:30",
        "end_et": "09:45",
        "any_of": {"premarket_volume_gte": 5000, "regular_volume_gte": 10000, "elapsed_rvol_gte": 1.5},
    },
    {
        "id": "open_followthrough",
        "start_et": "09:50",
        "end_et": "10:30",
        "any_of": {"premarket_volume_gte": 10000, "regular_volume_gte": 30000, "elapsed_rvol_gte": 1.2},
    },
    {"id": "late_morning", "start_et": "10:35", "end_et": "11:00", "any_of": {"regular_volume_gte": 60000, "elapsed_rvol_gte": 1.0}},
]

DEFAULT_ACTIVITY_GATE_STAGES_JSON = json.dumps(DEFAULT_ACTIVITY_GATE_STAGES, separators=(",", ":"))


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
        if parsed != parsed:
            return float(default)
        return parsed
    except Exception:
        return float(default)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except Exception:
        return int(default)


def _parse_hhmm(value: Any) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        hour_text, minute_text = text.split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
    except Exception:
        return None
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return hour * 60 + minute
    return None


def _coerce_now_et(value: Any = None, *, settings: dict | None = None) -> datetime:
    settings = settings if isinstance(settings, dict) else {}
    raw = value
    if raw is None:
        raw = (
            settings.get("activity_gate_now_et")
            or settings.get("activity_gate_current_time_et")
            or settings.get("current_time_et")
        )
    if isinstance(raw, datetime):
        return raw.astimezone(ET) if raw.tzinfo else raw.replace(tzinfo=ET)
    if isinstance(raw, (int, float)) and raw:
        return datetime.fromtimestamp(float(raw) / 1000.0, ET)
    text = str(raw or "").strip()
    if text:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%H:%M"):
            try:
                parsed = datetime.strptime(text[:19] if fmt.startswith("%Y") else text[:5], fmt)
                if fmt == "%H:%M":
                    now = datetime.now(ET)
                    parsed = now.replace(hour=parsed.hour, minute=parsed.minute, second=0, microsecond=0)
                return parsed.replace(tzinfo=ET)
            except Exception:
                continue
        try:
            parsed = datetime.fromisoformat(text)
            return parsed.astimezone(ET) if parsed.tzinfo else parsed.replace(tzinfo=ET)
        except Exception:
            pass
    return datetime.now(ET)


def _stage_source(settings: dict | None = None, raw: Any = None) -> Any:
    if raw is not None:
        return raw
    settings = settings if isinstance(settings, dict) else {}
    return (
        settings.get("activity_gate_stages")
        or settings.get("activity_gate_stages_json")
        or settings.get("target_activity_gate_stages_json")
        or settings.get("ibkr_target_activity_gate_stages_json")
    )


def parse_activity_gate_stages(raw: Any = None) -> list[dict]:
    data = _stage_source(raw=raw)
    if not data:
        data = DEFAULT_ACTIVITY_GATE_STAGES
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            data = DEFAULT_ACTIVITY_GATE_STAGES
    if isinstance(data, dict):
        data = data.get("stages") or data.get("items") or []
    if not isinstance(data, list):
        data = DEFAULT_ACTIVITY_GATE_STAGES

    stages: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        start_min = _parse_hhmm(item.get("start_et"))
        end_min = _parse_hhmm(item.get("end_et"))
        if start_min is None or end_min is None:
            continue
        any_of = item.get("any_of") if isinstance(item.get("any_of"), dict) else {}
        clean_thresholds = {
            str(key or "").strip(): _safe_float(value)
            for key, value in any_of.items()
            if str(key or "").strip() and _safe_float(value) > 0
        }
        if not clean_thresholds:
            continue
        stages.append(
            {
                "id": str(item.get("id") or f"{item.get('start_et')}-{item.get('end_et')}").strip(),
                "start_et": f"{start_min // 60:02d}:{start_min % 60:02d}",
                "end_et": f"{end_min // 60:02d}:{end_min % 60:02d}",
                "start_minute": start_min,
                "end_minute": end_min,
                "any_of": clean_thresholds,
            }
        )
    return stages or parse_activity_gate_stages(DEFAULT_ACTIVITY_GATE_STAGES)


def select_activity_gate_stage(now_et: Any = None, *, settings: dict | None = None, stages: list[dict] | None = None) -> dict:
    current = _coerce_now_et(now_et, settings=settings)
    current_minute = current.hour * 60 + current.minute
    for stage in stages or parse_activity_gate_stages(_stage_source(settings=settings)):
        start_minute = _safe_int(stage.get("start_minute"), _parse_hhmm(stage.get("start_et")) or -1)
        end_minute = _safe_int(stage.get("end_minute"), _parse_hhmm(stage.get("end_et")) or -1)
        if start_minute <= current_minute <= end_minute:
            return dict(stage)
    return {}


def regular_elapsed_minutes(now_et: Any = None, *, settings: dict | None = None) -> int:
    current = _coerce_now_et(now_et, settings=settings)
    current_minute = current.hour * 60 + current.minute
    return max(0, min(int(REGULAR_SESSION_MINUTES), current_minute - REGULAR_OPEN_MINUTE))


def enrich_activity_metrics(metrics: dict | None, *, settings: dict | None = None, now_et: Any = None) -> dict:
    row = dict(metrics or {})
    premarket_volume = _safe_float(row.get("premarket_volume"))
    today_volume = _safe_float(row.get("today_volume"))
    regular_volume = max(
        _safe_float(row.get("regular_volume")),
        _safe_float(row.get("regular_session_volume")),
        _safe_float(row.get("regular_hours_volume")),
    )
    if regular_volume <= 0 and today_volume > 0:
        regular_volume = max(0.0, today_volume - premarket_volume)
    elapsed_minutes = regular_elapsed_minutes(now_et, settings=settings)
    avg_10d_volume = _safe_float(row.get("avg_10d_volume") or row.get("avg_volume_10d"))
    elapsed_rvol = max(_safe_float(row.get("elapsed_rvol")), _safe_float(row.get("regular_elapsed_rvol")))
    if elapsed_rvol <= 0 and elapsed_minutes > 0 and avg_10d_volume > 0:
        expected_volume = avg_10d_volume * (elapsed_minutes / REGULAR_SESSION_MINUTES)
        if expected_volume > 0:
            elapsed_rvol = regular_volume / expected_volume
    row.update(
        {
            "premarket_volume": premarket_volume,
            "today_volume": today_volume,
            "regular_volume": regular_volume,
            "elapsed_regular_minutes": elapsed_minutes,
            "elapsed_rvol": elapsed_rvol,
        }
    )
    return row


def evaluate_activity_gate(
    metrics: dict | None,
    *,
    settings: dict | None = None,
    now_et: Any = None,
    stages: list[dict] | None = None,
) -> dict:
    stage = select_activity_gate_stage(now_et, settings=settings, stages=stages)
    enriched = enrich_activity_metrics(metrics, settings=settings, now_et=now_et)
    if not stage:
        return {
            "applied": False,
            "passed": True,
            "stage_id": "",
            "thresholds": {},
            "actuals": {
                "premarket_volume": enriched.get("premarket_volume", 0.0),
                "today_volume": enriched.get("today_volume", 0.0),
                "regular_volume": enriched.get("regular_volume", 0.0),
                "elapsed_rvol": enriched.get("elapsed_rvol", 0.0),
            },
            "elapsed_regular_minutes": enriched.get("elapsed_regular_minutes", 0),
            "reason": "activity_stage_not_active",
        }

    thresholds = dict(stage.get("any_of") or {})
    actuals = {
        "premarket_volume": _safe_float(enriched.get("premarket_volume")),
        "today_volume": _safe_float(enriched.get("today_volume")),
        "regular_volume": _safe_float(enriched.get("regular_volume")),
        "elapsed_rvol": _safe_float(enriched.get("elapsed_rvol")),
        "rvol_20": _safe_float(enriched.get("rvol_20")),
    }
    passed_keys = []
    failed_keys = []
    for key, threshold in thresholds.items():
        metric_name = key[:-4] if key.endswith("_gte") else key
        actual = _safe_float(actuals.get(metric_name))
        if actual >= _safe_float(threshold):
            passed_keys.append(key)
        else:
            failed_keys.append(key)
    passed = bool(passed_keys)
    return {
        "applied": True,
        "passed": passed,
        "stage_id": str(stage.get("id") or "").strip(),
        "stage": {
            "id": str(stage.get("id") or "").strip(),
            "start_et": str(stage.get("start_et") or ""),
            "end_et": str(stage.get("end_et") or ""),
        },
        "thresholds": thresholds,
        "actuals": actuals,
        "passed_keys": passed_keys,
        "failed_keys": failed_keys,
        "elapsed_regular_minutes": enriched.get("elapsed_regular_minutes", 0),
        "reason": "" if passed else "activity_below_stage_threshold",
    }

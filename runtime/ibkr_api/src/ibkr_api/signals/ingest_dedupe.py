from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from ibkr_api.orders.values import ensure_object, to_float, to_text
from ibkr_api.signals.ingest_payloads import first_defined, normalize_signal_source_meta
from ibkr_api.signals.values import get_signal_extra


EscapeFilterString = Callable[[Any], str]


def _now_iso_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _to_number_or_default(value: Any, default: float = 0) -> float:
    parsed = to_float(value)
    return parsed if parsed is not None else float(default)


def build_signal_bar_dedupe_key(data: dict[str, Any], environment: str) -> str:
    symbol = to_text(data.get("symbol")).upper()
    direction = to_text(data.get("direction")).lower()
    bar_time_ms = int(_to_number_or_default(data.get("bar_time_ms"), 0))
    interval = to_text(data.get("interval"))
    chart_tf = to_text(data.get("chart_tf"))
    script_tag = to_text(data.get("script_tag"))
    runtime_environment = to_text(environment or data.get("environment") or "live").lower() or "live"
    if not symbol or not direction or not bar_time_ms:
        return ""
    return "|".join(
        [
            runtime_environment,
            symbol,
            direction,
            str(bar_time_ms),
            interval or "-",
            chart_tf or "-",
            script_tag or "-",
        ]
    )


def find_signal_duplicate_by_bar_key(
    pb: Any,
    data: dict[str, Any],
    environment: str,
    *,
    exclude_signal_id: str,
    escape_filter_string: EscapeFilterString,
) -> dict[str, Any] | None:
    symbol = to_text(data.get("symbol")).upper()
    direction = to_text(data.get("direction")).lower()
    bar_time_ms = int(_to_number_or_default(data.get("bar_time_ms"), 0))
    runtime_environment = to_text(environment or data.get("environment") or "live").lower() or "live"
    if not symbol or not direction or not bar_time_ms:
        return None

    filters = [
        f'environment = "{escape_filter_string(runtime_environment)}"',
        f'symbol = "{escape_filter_string(symbol)}"',
        f'direction = "{escape_filter_string(direction)}"',
        f"bar_time_ms = {bar_time_ms}",
    ]
    interval = to_text(data.get("interval"))
    chart_tf = to_text(data.get("chart_tf"))
    script_tag = to_text(data.get("script_tag"))
    signal_id = to_text(exclude_signal_id or data.get("signal_id"))
    if interval:
        filters.append(f'interval = "{escape_filter_string(interval)}"')
    if chart_tf:
        filters.append(f'chart_tf = "{escape_filter_string(chart_tf)}"')
    if script_tag:
        filters.append(f'script_tag = "{escape_filter_string(script_tag)}"')
    if signal_id:
        filters.append(f'signal_id != "{escape_filter_string(signal_id)}"')

    rows = pb.get_records(
        "ibkr_signals",
        filter=" && ".join(filters),
        sort="-updated,-created",
        per_page=5,
        page=1,
    )
    return dict(rows[0]) if rows else None


def annotate_signal_duplicate(
    pb: Any,
    record: dict[str, Any],
    incoming_data: dict[str, Any],
    environment: str,
) -> dict[str, Any]:
    extra = get_signal_extra(record)
    duplicate_signal_ids = list(extra.get("duplicate_signal_ids") or []) if isinstance(extra.get("duplicate_signal_ids"), list) else []
    incoming_signal_id = to_text(incoming_data.get("signal_id"))
    if incoming_signal_id and incoming_signal_id not in duplicate_signal_ids:
        duplicate_signal_ids.append(incoming_signal_id)

    incoming_extra = ensure_object(incoming_data.get("extra"))
    source_meta = normalize_signal_source_meta(
        first_defined(
            incoming_data.get("signal_source"),
            incoming_extra.get("signal_source"),
            incoming_data.get("source"),
            incoming_extra.get("source"),
        )
    )
    patch_extra = {
        **extra,
        "duplicate_signal_ids": duplicate_signal_ids,
        "duplicate_signal_count": len(duplicate_signal_ids),
        "last_duplicate_signal_id": incoming_signal_id,
        "last_duplicate_signal_at": _now_iso_utc(),
        "last_duplicate_signal_source": to_text(
            first_defined(incoming_extra.get("signal_source"), incoming_data.get("signal_source"), source_meta.get("signal_source"))
        ),
        "last_duplicate_signal_source_label": to_text(
            first_defined(
                incoming_extra.get("signal_source_label"),
                incoming_data.get("signal_source_label"),
                source_meta.get("signal_source_label"),
            )
        ),
        "duplicate_bar_dedupe_key": build_signal_bar_dedupe_key(incoming_data, environment),
    }
    record_id = to_text(record.get("id"))
    if record_id:
        updated = pb.update_record("ibkr_signals", record_id, {"extra": patch_extra})
        return dict(updated) if isinstance(updated, dict) else {**record, "extra": patch_extra}
    return {**record, "extra": patch_extra}


__all__ = [
    "annotate_signal_duplicate",
    "build_signal_bar_dedupe_key",
    "find_signal_duplicate_by_bar_key",
]

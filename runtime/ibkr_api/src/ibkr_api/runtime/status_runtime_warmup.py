from __future__ import annotations

from typing import Any

from .status_symbol_partition import split_reason_map_by_monitor, split_symbol_list_by_monitor
from .status_types import AsDict, NormalizeSymbolList, TrimArray


def _list_total(values: Any, fallback: Any = 0) -> int:
    if isinstance(values, list):
        return len(values)
    return int(fallback or 0)


def _trim_detail_list(values: Any, include_warmup_details: bool, *, trim_array: TrimArray) -> list[Any]:
    if not include_warmup_details or not isinstance(values, list):
        return []
    return trim_array(values, len(values))


def build_runtime_warmup_payload(
    warmup_payload: Any,
    include_warmup_details: bool,
    *,
    as_dict: AsDict,
    normalize_symbol_list: NormalizeSymbolList,
    trim_array: TrimArray,
) -> dict[str, Any]:
    warmup = as_dict(warmup_payload)
    monitor_symbols = normalize_symbol_list(warmup.get("monitor_symbols"))
    pending_symbols = normalize_symbol_list(warmup.get("pending_symbols"))
    integrity_pending_symbols_all = normalize_symbol_list(warmup.get("integrity_pending_symbols"))

    pending_split = split_symbol_list_by_monitor(
        pending_symbols,
        monitor_symbols,
        normalize_symbol_list=normalize_symbol_list,
    )
    integrity_pending_split = split_symbol_list_by_monitor(
        integrity_pending_symbols_all,
        monitor_symbols,
        normalize_symbol_list=normalize_symbol_list,
    )
    integrity_repair_reasons = as_dict(warmup.get("integrity_repair_reasons")) if include_warmup_details else {}
    reason_split = split_reason_map_by_monitor(
        integrity_repair_reasons,
        monitor_symbols,
        normalize_symbol_list=normalize_symbol_list,
    )

    return {
        "phase": str(warmup.get("phase") or ""),
        "trading_gate_open": bool(warmup.get("trading_gate_open")),
        "trading_gate_reason": str(warmup.get("trading_gate_reason") or ""),
        "required_interval": str(warmup.get("required_interval") or ""),
        "symbols_total": int(warmup.get("symbols_total") or 0),
        "trade_symbols_total": int(warmup.get("trade_symbols_total") or 0),
        "monitor_symbols_total": int(warmup.get("monitor_symbols_total") or 0),
        "ready_symbols": int(warmup.get("ready_symbols") or 0),
        "ready_trade_symbols": int(warmup.get("ready_trade_symbols") or 0),
        "ready_monitor_symbols": int(warmup.get("ready_monitor_symbols") or 0),
        "pending_symbols": (
            trim_array(pending_symbols, len(pending_symbols))
            if include_warmup_details
            else trim_array(pending_symbols, 12)
        ),
        "pending_symbols_total": _list_total(
            warmup.get("pending_symbols"),
            warmup.get("pending_symbols_total") or len(pending_symbols),
        ),
        "blocking_pending_symbols": trim_array(
            pending_split.get("blocking"),
            len(pending_split.get("blocking", [])) if include_warmup_details else 12,
        ),
        "blocking_pending_symbols_total": len(pending_split.get("blocking") or []),
        "monitor_pending_symbols": trim_array(
            pending_split.get("monitor"),
            len(pending_split.get("monitor", [])) if include_warmup_details else 12,
        ),
        "monitor_pending_symbols_total": len(pending_split.get("monitor") or []),
        "requested_at": warmup.get("requested_at") or "",
        "started_at": warmup.get("started_at") or "",
        "finished_at": warmup.get("finished_at") or "",
        "last_success_at": warmup.get("last_success_at") or "",
        "last_error": str(warmup.get("last_error") or ""),
        "reason": str(warmup.get("reason") or ""),
        "target_date": str(warmup.get("target_date") or ""),
        "symbols": _trim_detail_list(warmup.get("symbols"), include_warmup_details, trim_array=trim_array),
        "trade_symbols": _trim_detail_list(warmup.get("trade_symbols"), include_warmup_details, trim_array=trim_array),
        "monitor_symbols": _trim_detail_list(
            warmup.get("monitor_symbols"),
            include_warmup_details,
            trim_array=trim_array,
        ),
        "ready_symbols_list": _trim_detail_list(
            warmup.get("ready_symbols_list"),
            include_warmup_details,
            trim_array=trim_array,
        ),
        "symbol_status": _trim_detail_list(warmup.get("symbol_status"), include_warmup_details, trim_array=trim_array),
        "integrity_pending_symbols": (
            trim_array(integrity_pending_symbols_all, len(integrity_pending_symbols_all))
            if include_warmup_details
            else []
        ),
        "integrity_pending_symbols_total": _list_total(
            warmup.get("integrity_pending_symbols"),
            len(integrity_pending_symbols_all),
        ),
        "blocking_integrity_pending_symbols": (
            trim_array(integrity_pending_split.get("blocking"), len(integrity_pending_split.get("blocking", [])))
            if include_warmup_details
            else []
        ),
        "blocking_integrity_pending_symbols_total": len(integrity_pending_split.get("blocking") or []),
        "monitor_integrity_pending_symbols": (
            trim_array(integrity_pending_split.get("monitor"), len(integrity_pending_split.get("monitor", [])))
            if include_warmup_details
            else []
        ),
        "monitor_integrity_pending_symbols_total": len(integrity_pending_split.get("monitor") or []),
        "integrity_repair_reasons": integrity_repair_reasons,
        "blocking_integrity_repair_reasons": reason_split.get("blocking") or {},
        "monitor_integrity_repair_reasons": reason_split.get("monitor") or {},
        "preflight_repair": as_dict(warmup.get("preflight_repair")) if include_warmup_details else {},
    }


__all__ = ["build_runtime_warmup_payload"]

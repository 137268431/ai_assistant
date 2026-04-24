from __future__ import annotations

from typing import Any

from .status_types import NormalizeSymbolList


def split_symbol_list_by_monitor(
    values: Any,
    monitor_symbols: Any,
    *,
    normalize_symbol_list: NormalizeSymbolList,
) -> dict[str, list[str]]:
    monitor_set = set(normalize_symbol_list(monitor_symbols))
    blocking: list[str] = []
    monitor: list[str] = []
    for symbol in normalize_symbol_list(values):
        if symbol in monitor_set:
            monitor.append(symbol)
        else:
            blocking.append(symbol)
    return {"blocking": blocking, "monitor": monitor}


def split_reason_map_by_monitor(
    value: Any,
    monitor_symbols: Any,
    *,
    normalize_symbol_list: NormalizeSymbolList,
) -> dict[str, dict[str, Any]]:
    monitor_set = set(normalize_symbol_list(monitor_symbols))
    blocking: dict[str, Any] = {}
    monitor: dict[str, Any] = {}
    if not isinstance(value, dict):
        return {"blocking": blocking, "monitor": monitor}
    for symbol, reason in value.items():
        normalized_symbol = str(symbol or "").strip().upper()
        if not normalized_symbol:
            continue
        if normalized_symbol in monitor_set:
            monitor[normalized_symbol] = reason
        else:
            blocking[normalized_symbol] = reason
    return {"blocking": blocking, "monitor": monitor}


__all__ = [
    "split_reason_map_by_monitor",
    "split_symbol_list_by_monitor",
]

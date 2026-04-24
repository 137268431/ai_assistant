from __future__ import annotations

from typing import Any

from .status_types import AsDict


def build_statusz_compute_payload(
    compute_payload: dict[str, Any],
    include_engines: bool,
    *,
    as_dict: AsDict,
) -> dict[str, Any]:
    payload = as_dict(compute_payload)
    engine_map = payload.get("engines") if isinstance(payload.get("engines"), dict) else {}
    total_engines = int(payload.get("total_engines") or len(engine_map))
    payload["total_engines"] = total_engines
    payload["ready_engines"] = int(payload.get("ready_engines") or 0)
    payload["engines_available"] = total_engines > 0
    payload["engines_included"] = bool(include_engines)
    payload["statusz_mode"] = "full" if include_engines else "lite"
    if include_engines:
        payload["engines"] = dict(engine_map)
    else:
        payload.pop("engines", None)
    return payload


__all__ = ["build_statusz_compute_payload"]

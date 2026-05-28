from __future__ import annotations

from typing import Any, Callable


def build_upsert_tv_indicator(
    *,
    globals_dict: dict[str, Any],
    pb: Any,
    support: Callable[..., Any],
):
    def _upsert_tv_indicator(payload: dict[str, Any]):
        return support(
            payload,
            pb=pb,
            normalize_environment=globals_dict["_normalize_environment"],
            escape_filter_string=globals_dict["_escape_filter_string"],
            jsonify_fn=globals_dict["jsonify"],
        )

    return _upsert_tv_indicator


def build_upsert_tv_indicator_audit(
    *,
    globals_dict: dict[str, Any],
    pb: Any,
    support: Callable[..., Any],
):
    def _upsert_tv_indicator_audit(payload: dict[str, Any]):
        return support(
            payload,
            pb=pb,
            normalize_environment=globals_dict["_normalize_environment"],
            escape_filter_string=globals_dict["_escape_filter_string"],
            jsonify_fn=globals_dict["jsonify"],
        )

    return _upsert_tv_indicator_audit


def build_upsert_tv_signal(
    *,
    globals_dict: dict[str, Any],
    pb: Any,
    support: Callable[..., Any],
):
    def _upsert_tv_signal(payload: dict[str, Any]):
        return support(
            payload,
            pb=pb,
            normalize_environment=globals_dict["_normalize_environment"],
            escape_filter_string=globals_dict["_escape_filter_string"],
            jsonify_fn=globals_dict["jsonify"],
            time_strings=globals_dict["_time_strings"],
        )

    return _upsert_tv_signal


__all__ = [
    "build_upsert_tv_indicator",
    "build_upsert_tv_indicator_audit",
    "build_upsert_tv_signal",
]

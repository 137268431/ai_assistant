from __future__ import annotations


def _api_app():
    from ... import app as api_app

    return api_app


def _app_coerce_float(value, default: float | None = None) -> float | None:
    return _api_app()._coerce_float(value, default)


__all__ = ["_api_app", "_app_coerce_float"]

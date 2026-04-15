from __future__ import annotations


def _api_app():
    from ... import app as api_app

    return api_app


__all__ = ["_api_app"]

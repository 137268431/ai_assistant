from __future__ import annotations


def get_api_app():
    from ... import app as api_app

    return api_app


__all__ = ["get_api_app"]

from __future__ import annotations

import traceback

from ibkr_compute.market.pocketbase_sqlite import open_pb_sqlite, upsert_indicators

from .common import _api_app


def _direct_sqlite_timeout(api_app) -> float:
    cfg = getattr(api_app, "cfg", None)
    environment = str(getattr(api_app, "ENVIRONMENT", "") or "live").strip().lower() or "live"
    if cfg is not None and hasattr(cfg, "get_float_for_environment"):
        try:
            return max(1.0, float(cfg.get_float_for_environment("ibkr_bar_direct_sqlite_timeout_sec", environment, 30.0)))
        except Exception:
            return 30.0
    return 30.0


def _direct_sqlite_indicator_write_enabled(api_app) -> bool:
    cfg = getattr(api_app, "cfg", None)
    environment = str(getattr(api_app, "ENVIRONMENT", "") or "live").strip().lower() or "live"
    if cfg is not None and hasattr(cfg, "get_bool_for_environment"):
        try:
            return bool(cfg.get_bool_for_environment("ibkr_indicator_direct_sqlite_enabled", environment, True))
        except Exception:
            return True
    return True


def flush_indicator_batch(batch):
    api_app = _api_app()
    if not batch:
        return {"ok": True, "written": 0, "errors": 0}

    if _direct_sqlite_indicator_write_enabled(api_app):
        try:
            with open_pb_sqlite(readonly=False, timeout=_direct_sqlite_timeout(api_app)) as conn:
                with conn:
                    written = upsert_indicators(conn, batch)
            return {"ok": True, "written": int(written or 0), "errors": 0, "write_path": "direct_sqlite"}
        except Exception:
            traceback.print_exc()

    try:
        result = api_app.pb.upsert_indicators(batch)
        written = int(result.get("success", 0) or 0)
        errors = int(result.get("errors", 0) or 0)
        if not result.get("ok", False) and written == 0 and errors == 0:
            errors = len(batch)
        if result.get("ok", False) or (written > 0 and errors == 0):
            return {"ok": True, "written": written, "errors": errors}
    except Exception:
        traceback.print_exc()

    written = 0
    errors = 0
    for item in batch:
        try:
            result = api_app.pb.upsert_indicator(item)
            if str(result.get("action", "")).strip().lower() != "skipped":
                written += 1
        except Exception:
            errors += 1
            traceback.print_exc()
    return {"ok": errors == 0, "written": written, "errors": errors}


def flush_signal_batch(batch):
    api_app = _api_app()
    if not batch:
        return {"ok": True, "written": 0, "errors": 0}

    try:
        result = api_app.pb.upsert_signals(batch)
        written = int(result.get("success", 0) or 0)
        errors = int(result.get("errors", 0) or 0)
        if not result.get("ok", False) and written == 0 and errors == 0:
            errors = len(batch)
        if result.get("ok", False) or (written > 0 and errors == 0):
            return {"ok": True, "written": written, "errors": errors}
    except Exception:
        traceback.print_exc()

    written = 0
    errors = 0
    for item in batch:
        try:
            result = api_app.pb.upsert_signal(item)
            if str(result.get("action", "")).strip().lower() != "skipped":
                written += 1
        except Exception:
            errors += 1
            traceback.print_exc()
    return {"ok": errors == 0, "written": written, "errors": errors}

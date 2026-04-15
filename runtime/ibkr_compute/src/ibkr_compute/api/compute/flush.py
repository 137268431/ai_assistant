from __future__ import annotations

import traceback

from .common import _api_app


def flush_indicator_batch(batch):
    api_app = _api_app()
    if not batch:
        return {"ok": True, "written": 0, "errors": 0}

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

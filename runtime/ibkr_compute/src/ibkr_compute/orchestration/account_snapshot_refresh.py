from __future__ import annotations

import os
import time


def _service_mod():
    from . import trading_service as service_mod

    return service_mod


def _env_bool(name: str, default: bool) -> bool:
    value = str(os.environ.get(name, str(default).lower()) or "").strip().lower()
    return value not in {"0", "false", "no", "off"}


def _env_float(name: str, default: float) -> float:
    try:
        return max(1.0, float(os.environ.get(name, default) or default))
    except Exception:
        return max(1.0, float(default))


class TradingServiceAccountSnapshotRefreshMixin:
    def _account_snapshot_refresh_enabled(self) -> bool:
        service_mod = _service_mod()
        default_enabled = _env_bool("IBKR_ACCOUNT_SNAPSHOT_REFRESH_ENABLED", True)
        try:
            return self.config.get_bool_for_environment(
                "ibkr_account_snapshot_refresh_enabled",
                service_mod.ENVIRONMENT,
                default_enabled,
            )
        except Exception:
            return default_enabled

    def _account_snapshot_refresh_interval_sec(self, *, active: bool = False, error: bool = False) -> float:
        if error:
            return _env_float("IBKR_ACCOUNT_SNAPSHOT_ERROR_BACKOFF_SEC", 60.0)
        if active:
            return _env_float("IBKR_ACCOUNT_SNAPSHOT_ACTIVE_REFRESH_INTERVAL_SEC", 10.0)
        return _env_float("IBKR_ACCOUNT_SNAPSHOT_REFRESH_INTERVAL_SEC", 30.0)

    def _refresh_account_snapshot_once(self, *, reason: str = "loop") -> dict:
        if not self._account_snapshot_refresh_enabled():
            return {"ok": True, "skipped": True, "reason": "account_snapshot_refresh_disabled"}
        try:
            self.config.refresh()
        except Exception:
            pass
        from ibkr_compute.api.account.snapshot import (
            _build_ibkr_account_buying_power_snapshot,
            refresh_account_snapshot_cache,
        )
        from ibkr_compute.observability.prometheus import set_account_snapshot_metrics

        payload = refresh_account_snapshot_cache(self, include_pnl=False)
        if isinstance(payload, dict):
            payload["refresh_reason"] = reason
            try:
                set_account_snapshot_metrics(payload, source="account_snapshot")
                buying_power_payload = _build_ibkr_account_buying_power_snapshot(self)
                set_account_snapshot_metrics(buying_power_payload, source="")
                if isinstance(buying_power_payload, dict):
                    payload["buying_power_metrics"] = {
                        "ok": bool(buying_power_payload.get("ok")),
                        "source": buying_power_payload.get("source"),
                        "state": (
                            (buying_power_payload.get("buying_power_guard") or {}).get("state")
                            if isinstance(buying_power_payload.get("buying_power_guard"), dict)
                            else ""
                        ),
                    }
            except Exception as exc:
                payload["metrics_error"] = str(exc)
            return payload
        return {"ok": False, "error": "account_snapshot_refresh_empty_result", "refresh_reason": reason}

    def _account_snapshot_refresh_loop(self):
        service_mod = _service_mod()
        service_mod.logger.info("Account snapshot refresh loop started")
        time.sleep(_env_float("IBKR_ACCOUNT_SNAPSHOT_INITIAL_DELAY_SEC", 2.0))
        while getattr(self, "_running", False):
            sleep_seconds = self._account_snapshot_refresh_interval_sec()
            try:
                payload = self._refresh_account_snapshot_once(reason="runtime_loop")
                if not isinstance(payload, dict) or payload.get("ok") is False:
                    sleep_seconds = self._account_snapshot_refresh_interval_sec(error=True)
                else:
                    counts = payload.get("counts") if isinstance(payload.get("counts"), dict) else {}
                    active = int(counts.get("open_positions") or 0) > 0 or int(counts.get("open_orders") or 0) > 0
                    sleep_seconds = self._account_snapshot_refresh_interval_sec(active=active)
            except Exception as exc:
                service_mod.logger.warning("Account snapshot refresh loop failed: %s", exc)
                sleep_seconds = self._account_snapshot_refresh_interval_sec(error=True)
            time.sleep(sleep_seconds)


__all__ = ["TradingServiceAccountSnapshotRefreshMixin"]

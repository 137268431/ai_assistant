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


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _component_status(component) -> dict:
    status_fn = getattr(component, "status", None)
    if not callable(status_fn):
        return {}
    try:
        payload = status_fn()
    except Exception:
        return {}
    return dict(payload) if isinstance(payload, dict) else {}


def _account_data_circuit_from_status(status: dict) -> dict:
    payload = status if isinstance(status, dict) else {}
    circuit = payload.get("account_data_circuit") if isinstance(payload.get("account_data_circuit"), dict) else {}
    if circuit:
        return dict(circuit)
    broker = payload.get("broker") if isinstance(payload.get("broker"), dict) else {}
    circuit = broker.get("account_data_circuit") if isinstance(broker.get("account_data_circuit"), dict) else {}
    return dict(circuit) if circuit else {}


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
            return _env_float("IBKR_ACCOUNT_SNAPSHOT_ERROR_BACKOFF_SEC", 180.0)
        if active:
            return _env_float("IBKR_ACCOUNT_SNAPSHOT_ACTIVE_REFRESH_INTERVAL_SEC", 60.0)
        return _env_float("IBKR_ACCOUNT_SNAPSHOT_REFRESH_INTERVAL_SEC", 180.0)

    def _account_snapshot_refresh_account_data_guard(self) -> dict:
        details = {
            "account_data_circuit_active": False,
            "account_data_circuit_remaining_s": 0.0,
            "account_data_circuit_reason": "",
            "account_lifecycle_backoff_remaining_s": 0.0,
            "account_lifecycle_backoff_reason": "",
            "order_tracker_backoff_remaining_s": 0.0,
            "order_tracker_backoff_reason": "",
        }

        for component_name in ("broker", "gateway_manager"):
            circuit = _account_data_circuit_from_status(_component_status(getattr(self, component_name, None)))
            if not circuit:
                continue
            remaining_s = _safe_float(circuit.get("remaining_s"), 0.0)
            if bool(circuit.get("active")) or remaining_s > 0:
                details["account_data_circuit_active"] = True
                details["account_data_circuit_remaining_s"] = max(
                    details["account_data_circuit_remaining_s"],
                    remaining_s,
                )
                if not details["account_data_circuit_reason"]:
                    details["account_data_circuit_reason"] = str(circuit.get("reason") or component_name)

        for attr_name, detail_prefix in (
            ("order_lifecycle", "account_lifecycle"),
            ("order_tracker", "order_tracker"),
        ):
            component = getattr(self, attr_name, None)
            remaining_fn = getattr(component, "_account_data_backoff_remaining", None)
            try:
                remaining_s = float(remaining_fn()) if callable(remaining_fn) else 0.0
            except Exception:
                remaining_s = 0.0
            remaining_s = max(0.0, remaining_s)
            reason = str(getattr(component, "_account_data_backoff_reason", "") or "")
            details[f"{detail_prefix}_backoff_remaining_s"] = round(remaining_s, 1)
            details[f"{detail_prefix}_backoff_reason"] = reason

        details["account_data_guard_active"] = bool(
            details["account_data_circuit_active"]
            or details["account_lifecycle_backoff_remaining_s"] > 0
            or details["order_tracker_backoff_remaining_s"] > 0
        )
        return details

    def _account_snapshot_refresh_orders_fast_needed(self) -> tuple[bool, dict]:
        details = {
            "symbol_queue_active": 0,
            "symbol_queue_queued": 0,
            "cached_open_orders": 0,
            "buying_power_reservations": 0,
        }
        details.update(self._account_snapshot_refresh_account_data_guard())
        scheduler = getattr(getattr(self, "order_placer", None), "symbol_scheduler", None)
        status_fn = getattr(scheduler, "status", None)
        if callable(status_fn):
            try:
                status = status_fn()
            except Exception:
                status = {}
            active_symbols = status.get("active_symbols") if isinstance(status, dict) else []
            queued_symbols = status.get("queued_symbols") if isinstance(status, dict) else {}
            details["symbol_queue_active"] = len(active_symbols or [])
            details["symbol_queue_queued"] = int(status.get("queued_total") or 0) if isinstance(status, dict) else 0
            if isinstance(queued_symbols, dict) and not details["symbol_queue_queued"]:
                details["symbol_queue_queued"] = sum(int(value or 0) for value in queued_symbols.values())

        tracker = getattr(self, "order_tracker", None)
        cached_getter = getattr(tracker, "get_cached_live_orders", None)
        if callable(cached_getter):
            try:
                details["cached_open_orders"] = len(list(cached_getter(include_all=False) or []))
            except TypeError:
                try:
                    details["cached_open_orders"] = len(list(cached_getter() or []))
                except Exception:
                    details["cached_open_orders"] = 0
            except Exception:
                details["cached_open_orders"] = 0

        reservations = getattr(self, "buying_power_reservations", None)
        snapshotter = getattr(reservations, "snapshot", None)
        if callable(snapshotter):
            try:
                snapshot = snapshotter()
                details["buying_power_reservations"] = int((snapshot or {}).get("count") or 0)
            except Exception:
                details["buying_power_reservations"] = 0

        needed = bool(details.get("account_data_guard_active")) or any(
            int(value or 0) > 0
            for key, value in details.items()
            if key in {
                "symbol_queue_active",
                "symbol_queue_queued",
                "cached_open_orders",
                "buying_power_reservations",
            }
        )
        return needed, details

    def _refresh_account_snapshot_once(self, *, reason: str = "loop") -> dict:
        if not self._account_snapshot_refresh_enabled():
            return {"ok": True, "skipped": True, "reason": "account_snapshot_refresh_disabled"}
        try:
            self.config.refresh()
        except Exception:
            pass
        from ibkr_compute.api.account.snapshot import (
            _build_ibkr_account_buying_power_snapshot,
            _build_ibkr_account_snapshot,
            refresh_account_snapshot_cache,
        )
        from ibkr_compute.observability.prometheus import set_account_snapshot_metrics

        use_orders_fast, orders_fast_reason = self._account_snapshot_refresh_orders_fast_needed()
        if use_orders_fast:
            payload = _build_ibkr_account_snapshot(
                self,
                include_pnl=False,
                force_refresh=True,
                allow_stale=True,
                orders_fast=True,
                orders_fast_open_only=True,
                fast_status=True,
            )
            if isinstance(payload, dict):
                payload["refresh_profile"] = "orders_fast_during_order_pressure"
                payload["refresh_profile_reason"] = orders_fast_reason
        else:
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

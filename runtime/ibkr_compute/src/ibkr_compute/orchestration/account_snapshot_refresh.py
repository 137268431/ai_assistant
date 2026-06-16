from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone


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


def _env_nonnegative_float(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.environ.get(name, default) or default))
    except Exception:
        return max(0.0, float(default))


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _parse_timestamp(value) -> float:
    if value in (None, ""):
        return 0.0
    try:
        number = float(value)
        if number > 0:
            return number / 1000.0 if number > 10_000_000_000 else number
    except (TypeError, ValueError):
        pass
    try:
        text = str(value or "").strip()
        if not text:
            return 0.0
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except Exception:
        return 0.0


def _payload_age_s(payload: dict, *keys: str) -> float | None:
    data = payload if isinstance(payload, dict) else {}
    for key in keys:
        raw = data.get(key)
        if raw in (None, ""):
            continue
        try:
            age = float(raw)
            if age >= 0:
                return max(0.0, age)
        except (TypeError, ValueError):
            pass
    for key in ("summary_fetched_at", "fetched_at", "snapshot_fetched_at"):
        epoch = _parse_timestamp(data.get(key))
        if epoch > 0:
            return max(0.0, time.time() - epoch)
    return None


def _summary_has_safety_values(summary: dict) -> bool:
    if not isinstance(summary, dict) or not summary:
        return False
    for key in (
        "remaining_buying_power",
        "buying_power",
        "net_liquidation",
        "available_funds",
        "excess_liquidity",
        "equity_with_loan",
    ):
        raw = summary.get(key)
        if raw in (None, ""):
            continue
        if _safe_float(raw, 0.0) != 0.0:
            return True
    return bool(str(summary.get("account_type") or "").strip())


def _snapshot_payload_safety_available(payload: dict) -> bool:
    if not isinstance(payload, dict) or not payload:
        return False
    health = payload.get("account_snapshot_health") if isinstance(payload.get("account_snapshot_health"), dict) else {}
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    summary_available = bool(payload.get("summary_available") or health.get("summary_available"))
    if not summary_available:
        summary_available = _summary_has_safety_values(summary)
    guard = payload.get("buying_power_guard") if isinstance(payload.get("buying_power_guard"), dict) else {}
    guard_state = str(guard.get("state") or "").strip().lower()
    return bool(summary_available and guard.get("available") is True and guard_state != "unavailable")


def _pacing_item_blocked(item: dict) -> tuple[bool, float, str]:
    payload = item if isinstance(item, dict) else {}
    retry_after_s = _safe_float(payload.get("retry_after_s"), 0.0)
    reason = str(
        payload.get("blocked_reason")
        or payload.get("cooldown_reason")
        or payload.get("reason")
        or ""
    ).strip().lower()
    return bool(payload.get("blocked") or retry_after_s > 0), retry_after_s, reason


def _pacing_block_is_soft_min_interval(kind: str, item: dict) -> bool:
    normalized = str(kind or "").strip().lower()
    if normalized not in {"positions", "open_orders", "open_orders_all"}:
        return False
    payload = item if isinstance(item, dict) else {}
    _blocked, _retry_after_s, reason = _pacing_item_blocked(payload)
    return reason == "min_interval" and not bool(payload.get("cooldown_active"))


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


def _account_data_gate_from_status(status: dict) -> dict:
    payload = status if isinstance(status, dict) else {}
    gate = payload.get("account_data_request_gate") if isinstance(payload.get("account_data_request_gate"), dict) else {}
    if gate:
        return dict(gate)
    broker = payload.get("broker") if isinstance(payload.get("broker"), dict) else {}
    gate = broker.get("account_data_request_gate") if isinstance(broker.get("account_data_request_gate"), dict) else {}
    return dict(gate) if gate else {}


def _account_data_pacing_from_status(status: dict) -> dict:
    payload = status if isinstance(status, dict) else {}
    pacing = payload.get("account_data_pacing") if isinstance(payload.get("account_data_pacing"), dict) else {}
    if pacing:
        return dict(pacing)
    broker = payload.get("broker") if isinstance(payload.get("broker"), dict) else {}
    pacing = broker.get("account_data_pacing") if isinstance(broker.get("account_data_pacing"), dict) else {}
    return dict(pacing) if pacing else {}


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
            "account_data_gate_active": False,
            "account_data_gate_owner_kind": "",
            "account_data_gate_owner_age_s": 0.0,
            "account_data_pacing_blocked": False,
            "account_data_pacing_blocked_kinds": [],
            "account_data_pacing_hard_blocked": False,
            "account_data_pacing_hard_blocked_kinds": [],
            "account_data_pacing_soft_blocked": False,
            "account_data_pacing_soft_blocked_kinds": [],
            "account_lifecycle_backoff_remaining_s": 0.0,
            "account_lifecycle_backoff_reason": "",
            "order_tracker_backoff_remaining_s": 0.0,
            "order_tracker_backoff_reason": "",
            "account_data_guard_hard_active": False,
            "account_data_guard_soft_active": False,
            "safety_cache_available": False,
            "safety_cache_source": "",
            "safety_cache_state": "",
            "safety_cache_error": "",
        }

        for component_name in ("broker", "gateway_manager"):
            component_status = _component_status(getattr(self, component_name, None))
            circuit = _account_data_circuit_from_status(component_status)
            if not circuit:
                circuit = {}
            else:
                remaining_s = _safe_float(circuit.get("remaining_s"), 0.0)
                if bool(circuit.get("active")) or remaining_s > 0:
                    details["account_data_circuit_active"] = True
                    details["account_data_circuit_remaining_s"] = max(
                        details["account_data_circuit_remaining_s"],
                        remaining_s,
                    )
                    if not details["account_data_circuit_reason"]:
                        details["account_data_circuit_reason"] = str(circuit.get("reason") or component_name)

            gate = _account_data_gate_from_status(component_status)
            owner_kind = str(gate.get("owner_kind") or "").strip()
            if owner_kind:
                details["account_data_gate_active"] = True
                details["account_data_gate_owner_kind"] = details["account_data_gate_owner_kind"] or owner_kind
                details["account_data_gate_owner_age_s"] = max(
                    details["account_data_gate_owner_age_s"],
                    _safe_float(gate.get("owner_age_s"), 0.0),
                )

            pacing = _account_data_pacing_from_status(component_status)
            by_kind = pacing.get("by_kind") if isinstance(pacing.get("by_kind"), dict) else {}
            blocked_kinds = set(details["account_data_pacing_blocked_kinds"])
            hard_blocked_kinds = set(details["account_data_pacing_hard_blocked_kinds"])
            soft_blocked_kinds = set(details["account_data_pacing_soft_blocked_kinds"])
            for kind in (
                "positions",
                "open_orders",
                "open_orders_all",
                "account_updates",
                "account_summary",
                "account_pnl",
                "account_snapshot",
            ):
                item = by_kind.get(kind) if isinstance(by_kind.get(kind), dict) else {}
                blocked, _retry_after_s, _reason = _pacing_item_blocked(item)
                if blocked:
                    blocked_kinds.add(kind)
                    if _pacing_block_is_soft_min_interval(kind, item):
                        soft_blocked_kinds.add(kind)
                    else:
                        hard_blocked_kinds.add(kind)
            if blocked_kinds:
                details["account_data_pacing_blocked"] = True
                details["account_data_pacing_blocked_kinds"] = sorted(blocked_kinds)
            if hard_blocked_kinds:
                details["account_data_pacing_hard_blocked"] = True
                details["account_data_pacing_hard_blocked_kinds"] = sorted(hard_blocked_kinds)
            if soft_blocked_kinds:
                details["account_data_pacing_soft_blocked"] = True
                details["account_data_pacing_soft_blocked_kinds"] = sorted(soft_blocked_kinds)

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

        details["account_data_guard_hard_active"] = bool(
            details["account_data_circuit_active"]
            or details["account_data_gate_active"]
            or details["account_data_pacing_hard_blocked"]
            or details["account_lifecycle_backoff_remaining_s"] > 0
            or details["order_tracker_backoff_remaining_s"] > 0
        )
        details["account_data_guard_soft_active"] = bool(
            details["account_data_pacing_soft_blocked"]
            and not details["account_data_guard_hard_active"]
        )
        details["account_data_guard_active"] = bool(
            details["account_data_guard_hard_active"]
            or details["account_data_guard_soft_active"]
        )
        return details

    def _account_snapshot_refresh_warmup_state(self) -> dict:
        grace_s = _env_nonnegative_float("IBKR_ACCOUNT_SNAPSHOT_STARTUP_WARMUP_SEC", 90.0)
        starting = bool(getattr(self, "_starting", False))
        started_at = _safe_float(getattr(self, "_runtime_started_at", 0.0), 0.0)
        now = time.time()
        remaining_s = grace_s if starting and grace_s > 0 else 0.0
        if not starting and grace_s > 0 and started_at > 0:
            remaining_s = max(0.0, grace_s - max(0.0, now - started_at))
        active = bool(starting or remaining_s > 0)
        return {
            "startup_warmup_active": active,
            "startup_warmup_remaining_s": round(remaining_s, 1) if active else 0.0,
            "startup_warmup_grace_s": grace_s,
            "service_starting": starting,
        }

    def _account_snapshot_refresh_safety_cache_state(self) -> dict:
        state = {
            "safety_cache_available": False,
            "safety_cache_source": "",
            "safety_cache_state": "",
            "safety_cache_error": "",
        }
        try:
            from ibkr_compute.api.account.snapshot_builder.context import (
                build_snapshot_context,
                load_cached_snapshot,
            )
            from ibkr_compute.api.account.snapshot_builder.payload import _decorate_account_snapshot_health

            context = build_snapshot_context(self, include_pnl=False, fast_status=True)
            runtime_environment = str(context.get("runtime_environment") or "").strip()
            account_id = str(context.get("account_id") or "").strip()
            candidate_keys = [
                ("full", context["cache_key"]),
                ("full", (runtime_environment, account_id, False)),
                ("full", (runtime_environment, account_id, True)),
                ("buying_power", (runtime_environment, f"{account_id}::buying_power", False)),
            ]
            seen: set[tuple] = set()
            for source, cache_key in candidate_keys:
                if not cache_key or cache_key in seen:
                    continue
                seen.add(cache_key)
                cached = load_cached_snapshot(context["api_app"], cache_key, allow_stale=True)
                if not isinstance(cached, dict) or not cached:
                    continue
                decorated = _decorate_account_snapshot_health(dict(cached))
                if _snapshot_payload_safety_available(decorated):
                    health = decorated.get("account_snapshot_health") if isinstance(decorated.get("account_snapshot_health"), dict) else {}
                    state.update(
                        {
                            "safety_cache_available": True,
                            "safety_cache_source": source,
                            "safety_cache_state": str(decorated.get("cache_state") or health.get("cache_state") or ""),
                        }
                    )
                    return state
        except Exception as exc:
            state["safety_cache_error"] = str(exc)
        return state

    def _account_snapshot_refresh_orders_fast_needed(self) -> tuple[bool, dict]:
        details = {
            "symbol_queue_active": 0,
            "symbol_queue_queued": 0,
            "cached_open_orders": 0,
            "buying_power_reservations": 0,
        }
        details.update(self._account_snapshot_refresh_warmup_state())
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

        order_pressure_active = any(
            int(value or 0) > 0
            for key, value in details.items()
            if key in {
                "symbol_queue_active",
                "symbol_queue_queued",
                "cached_open_orders",
                "buying_power_reservations",
            }
        )
        details["order_pressure_active"] = bool(order_pressure_active)
        hard_guard_active = bool(details.get("account_data_guard_hard_active"))
        soft_guard_active = bool(details.get("account_data_guard_soft_active"))
        startup_warmup_active = bool(details.get("startup_warmup_active"))
        if not hard_guard_active and not order_pressure_active and (startup_warmup_active or soft_guard_active):
            details.update(self._account_snapshot_refresh_safety_cache_state())

        if hard_guard_active:
            details["orders_fast_decision_reason"] = "account_data_hard_guard"
            needed = True
        elif order_pressure_active:
            details["orders_fast_decision_reason"] = "order_pressure"
            needed = True
        elif (startup_warmup_active or soft_guard_active) and bool(details.get("safety_cache_available")):
            details["orders_fast_decision_reason"] = (
                "startup_warmup_with_safety_cache"
                if startup_warmup_active
                else "soft_pacing_with_safety_cache"
            )
            needed = True
        elif startup_warmup_active or soft_guard_active:
            details["orders_fast_decision_reason"] = (
                "bootstrap_full_refresh_without_safety_cache"
                if startup_warmup_active
                else "soft_pacing_full_refresh_without_safety_cache"
            )
            needed = False
        else:
            details["orders_fast_decision_reason"] = "full_refresh"
            needed = False
        return needed, details

    def _account_snapshot_bootstrap_full_refresh_enabled(self) -> bool:
        return _env_bool("IBKR_ACCOUNT_SNAPSHOT_BOOTSTRAP_FULL_REFRESH_ENABLED", True)

    def _account_snapshot_summary_cache_max_age_sec(self) -> float:
        default = _env_float("IBKR_BUYING_POWER_STALE_SAFE_MAX_AGE_SEC", 1800.0)
        getter = getattr(getattr(self, "config", None), "get_float_for_environment", None)
        if callable(getter):
            try:
                service_mod = _service_mod()
                return max(1.0, float(getter("ibkr_buying_power_stale_safe_max_age_sec", service_mod.ENVIRONMENT, default)))
            except Exception:
                pass
        return max(1.0, default)

    def _account_snapshot_buying_power_max_age_sec(self) -> float:
        default = _env_float("IBKR_BUYING_POWER_MAX_SNAPSHOT_AGE_SEC", 180.0)
        getter = getattr(getattr(self, "config", None), "get_float_for_environment", None)
        if callable(getter):
            try:
                service_mod = _service_mod()
                return max(1.0, float(getter("ibkr_buying_power_max_snapshot_age_sec", service_mod.ENVIRONMENT, default)))
            except Exception:
                pass
        return max(1.0, default)

    def _account_snapshot_refresh_next_at(self, delay_s: float) -> str:
        return (datetime.now(timezone.utc) + timedelta(seconds=max(1.0, float(delay_s or 0.0)))).isoformat()

    def _buying_power_payload_fresh_enough(self, payload: dict) -> tuple[bool, dict]:
        guard = payload.get("buying_power_guard") if isinstance((payload or {}).get("buying_power_guard"), dict) else {}
        summary = payload.get("summary") if isinstance((payload or {}).get("summary"), dict) else {}
        age_s = _payload_age_s(payload, "summary_cache_age_s", "cache_age_s")
        max_age_s = self._account_snapshot_buying_power_max_age_sec()
        guard_state = str(guard.get("state") or "").strip().lower()
        available = bool(summary and guard.get("available") is not False and guard_state != "unavailable")
        fresh = bool(available and age_s is not None and age_s <= max_age_s and not bool((payload or {}).get("stale")))
        return fresh, {
            "buying_power_snapshot_age_s": round(float(age_s), 1) if age_s is not None else None,
            "buying_power_snapshot_max_age_s": round(float(max_age_s), 1),
            "buying_power_guard_state": guard_state,
            "buying_power_summary_available": bool(summary),
            "buying_power_source": (payload or {}).get("source"),
            "buying_power_refresh_state": (payload or {}).get("buying_power_refresh_state"),
        }

    def _buying_power_idle_probe_block_reason(self, details: dict) -> tuple[str, float]:
        if details.get("account_data_circuit_active"):
            return str(details.get("account_data_circuit_reason") or "account_data_circuit_open"), _safe_float(
                details.get("account_data_circuit_remaining_s"),
                0.0,
            )
        if details.get("account_data_gate_active"):
            return "account_data_request_in_flight", 30.0
        if details.get("account_data_pacing_hard_blocked"):
            kinds = ",".join(details.get("account_data_pacing_hard_blocked_kinds") or [])
            return f"account_data_pacing_blocked:{kinds}" if kinds else "account_data_pacing_blocked", 30.0
        for prefix in ("account_lifecycle", "order_tracker"):
            remaining = _safe_float(details.get(f"{prefix}_backoff_remaining_s"), 0.0)
            if remaining > 0:
                return str(details.get(f"{prefix}_backoff_reason") or f"{prefix}_backoff"), remaining
        return "", 0.0

    @staticmethod
    def _orders_fast_summary_unavailable(payload: dict) -> bool:
        if not isinstance(payload, dict) or not bool(payload.get("orders_fast")):
            return False
        if bool(payload.get("summary_available")):
            return False
        errors = payload.get("errors") if isinstance(payload.get("errors"), dict) else {}
        reason = str(errors.get("summary") or "").strip().lower()
        return reason in {"orders_fast_summary_cache_unavailable", "account_snapshot_unavailable"}

    def _orders_fast_summary_stale(self, payload: dict) -> tuple[bool, dict]:
        if not isinstance(payload, dict) or not bool(payload.get("orders_fast")):
            return False, {}
        if not bool(payload.get("summary_available")):
            return False, {}
        max_age_s = self._account_snapshot_summary_cache_max_age_sec()
        age_s = _payload_age_s(payload, "summary_cache_age_s", "cache_age_s")
        details = {
            "reason": "orders_fast_summary_cache_stale",
            "summary_cache_age_s": round(float(age_s), 1) if age_s is not None else None,
            "summary_cache_max_age_s": round(float(max_age_s), 1),
            "orders_fast_source": payload.get("source"),
            "orders_fast_summary_source": payload.get("summary_source"),
            "orders_fast_summary_fetched_at": payload.get("summary_fetched_at") or payload.get("fetched_at") or "",
        }
        return bool(age_s is not None and age_s > max_age_s), details

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
                payload["refresh_profile"] = (
                    "orders_fast_during_startup_warmup"
                    if bool(orders_fast_reason.get("startup_warmup_active"))
                    else "orders_fast_during_order_pressure"
                )
                payload["refresh_profile_reason"] = orders_fast_reason
                if (
                    self._account_snapshot_bootstrap_full_refresh_enabled()
                    and not bool(orders_fast_reason.get("account_data_guard_hard_active"))
                    and not bool(orders_fast_reason.get("order_pressure_active"))
                ):
                    fallback_reason = ""
                    fallback_profile = ""
                    fallback_details = {}
                    if self._orders_fast_summary_unavailable(payload):
                        fallback_reason = str(
                            (payload.get("errors") or {}).get("summary")
                            if isinstance(payload.get("errors"), dict)
                            else ""
                        ) or "orders_fast_summary_cache_unavailable"
                        fallback_profile = "full_bootstrap_after_orders_fast_unavailable"
                    else:
                        summary_stale, stale_details = self._orders_fast_summary_stale(payload)
                        if summary_stale:
                            fallback_reason = stale_details.get("reason") or "orders_fast_summary_cache_stale"
                            fallback_profile = "full_bootstrap_after_orders_fast_stale_summary"
                            fallback_details = stale_details
                    if fallback_reason:
                        fallback_payload = refresh_account_snapshot_cache(self, include_pnl=False)
                        if isinstance(fallback_payload, dict):
                            fallback_payload["refresh_profile"] = fallback_profile
                            fallback_payload["refresh_profile_reason"] = orders_fast_reason
                            fallback_payload["orders_fast_bootstrap_fallback"] = {
                                "enabled": True,
                                "reason": fallback_reason,
                                "orders_fast_source": payload.get("source"),
                                "orders_fast_summary_source": payload.get("summary_source"),
                                **fallback_details,
                            }
                            payload = fallback_payload
                            use_orders_fast = False
        else:
            payload = refresh_account_snapshot_cache(self, include_pnl=False)
            if isinstance(payload, dict):
                payload["refresh_profile"] = "full_account_snapshot_refresh"
                payload["refresh_profile_reason"] = orders_fast_reason
        if isinstance(payload, dict):
            payload["refresh_reason"] = reason
            try:
                set_account_snapshot_metrics(payload, source="account_snapshot")
                if payload.get("source"):
                    set_account_snapshot_metrics(payload, source=str(payload.get("source") or ""))
                if use_orders_fast:
                    payload_guard = payload.get("buying_power_guard") if isinstance(payload.get("buying_power_guard"), dict) else {}
                    buying_power_payload = {
                        "ok": bool(payload_guard.get("available", payload.get("summary_available"))),
                        "environment": payload.get("environment"),
                        "account_id": payload.get("account_id"),
                        "summary": dict(payload.get("summary") or {}),
                        "buying_power_guard": dict(payload_guard),
                        "source": payload.get("source") or "account_snapshot_orders_fast",
                        "account_snapshot_health": dict(payload.get("account_snapshot_health") or {}),
                        "cache_state": payload.get("cache_state"),
                        "stale": payload.get("stale"),
                        "fetched_at": payload.get("fetched_at"),
                    }
                else:
                    buying_power_payload = _build_ibkr_account_buying_power_snapshot(self)
                idle_probe = {
                    "enabled": True,
                    "refresh_profile": "buying_power_idle_probe",
                    "idle_phase": not bool(orders_fast_reason.get("order_pressure_active")),
                    "request_kind": "account_summary",
                    "request_attempted": False,
                    "request_block_reason": "",
                    "next_refresh_at": "",
                }
                fresh_enough, freshness_details = self._buying_power_payload_fresh_enough(
                    buying_power_payload if isinstance(buying_power_payload, dict) else {}
                )
                idle_probe.update(freshness_details)
                idle_probe["needed"] = bool(idle_probe["idle_phase"] and not fresh_enough)
                if idle_probe["needed"]:
                    if bool(orders_fast_reason.get("account_data_guard_hard_active")):
                        block_reason, retry_after_s = self._buying_power_idle_probe_block_reason(orders_fast_reason)
                        idle_probe.update(
                            {
                                "request_block_reason": block_reason or "account_data_guard_hard_active",
                                "retry_after_s": round(float(retry_after_s), 1),
                                "next_refresh_at": self._account_snapshot_refresh_next_at(max(30.0, retry_after_s)),
                            }
                        )
                    else:
                        probe_payload = _build_ibkr_account_buying_power_snapshot(self, force_refresh=True)
                        probe_guard = (
                            probe_payload.get("buying_power_guard")
                            if isinstance(probe_payload, dict) and isinstance(probe_payload.get("buying_power_guard"), dict)
                            else {}
                        )
                        retry_after_s = _safe_float((probe_payload or {}).get("retry_after_s"), 0.0)
                        request_block_reason = str(
                            (probe_payload or {}).get("last_refresh_error")
                            or (probe_payload or {}).get("refresh_error")
                            or probe_guard.get("reason")
                            or ""
                        )
                        idle_probe.update(
                            {
                                "request_attempted": True,
                                "request_result": (probe_payload or {}).get("buying_power_refresh_state")
                                or ("ok" if (probe_payload or {}).get("ok") else "failed"),
                                "request_block_reason": request_block_reason if (probe_payload or {}).get("hard_blocked") else "",
                                "retry_after_s": round(float(retry_after_s), 1),
                                "next_refresh_at": self._account_snapshot_refresh_next_at(max(30.0, retry_after_s)),
                            }
                        )
                        probe_fresh, probe_freshness = self._buying_power_payload_fresh_enough(
                            probe_payload if isinstance(probe_payload, dict) else {}
                        )
                        idle_probe["post_probe"] = probe_freshness
                        if isinstance(probe_payload, dict) and (probe_fresh or probe_payload.get("summary_available")):
                            buying_power_payload = probe_payload
                if not idle_probe.get("next_refresh_at"):
                    idle_probe["next_refresh_at"] = self._account_snapshot_refresh_next_at(
                        self._account_snapshot_refresh_interval_sec(active=False)
                    )
                payload["buying_power_idle_probe"] = idle_probe
                payload["buying_power_request_attempted"] = bool(idle_probe.get("request_attempted"))
                payload["buying_power_request_kind"] = idle_probe.get("request_kind")
                payload["buying_power_request_block_reason"] = idle_probe.get("request_block_reason")
                payload["buying_power_next_refresh_at"] = idle_probe.get("next_refresh_at")
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

from __future__ import annotations

from .market_universe_support import *
from .market_universe_support import (
    _classify_daily_scan_failure,
    _compact_daily_scan_diagnostics,
    _compact_daily_scan_result_for_state,
    _daily_scan_all_snapshotless,
    _daily_scan_running_age_seconds,
    _extract_daily_scan_failure_evidence,
    _parse_iso_datetime,
    _safe_extra,
    _safe_float,
    _safe_int,
    _service_mod,
    _target_row_is_daily_scan_active,
    _target_row_is_manual,
)

from ibkr_compute.market.bar_freshness import DEFAULT_CLOSE_DELAY_SECONDS, latest_expected_extended_5m_ms

from . import market_universe_support as _market_universe_support

WATCHLIST_IDLE_TOPUP_MAX_SYMBOLS_HARD_CAP = 200
WATCHLIST_IDLE_TOPUP_SIGNAL_BLOCKED_SYMBOLS = {"BOXX", "IBKR"}
WATCHLIST_IDLE_TOPUP_ROLLUP_INTERVALS = ["15m", "30m", "1h", "4h", "1d"]


def _service_mod():
    # Keep tests and legacy callers that patch market_universe._service_mod effective.
    import sys

    facade = sys.modules.get(f"{__package__}.market_universe")
    patched = getattr(facade, "_service_mod", None) if facade is not None else None
    if patched is not None:
        return patched()
    return _market_universe_support._service_mod()


class TradingServiceMarketUniverseWatchlistIdleTopupMixin:
    def _bar_integrity_market_date(self) -> str:
        return str(self._current_market_date or self._market_date())

    def _bar_integrity_cursor_payload(self) -> dict:
        service_mod = _service_mod()
        return {
            "market_date": self._bar_integrity_market_date(),
            "cursor": int(self._watchlist_integrity_cursor or 0),
            "last_scan_at": (
                datetime.fromtimestamp(self._last_watchlist_integrity_at, service_mod.ET).isoformat()
                if self._last_watchlist_integrity_at else ""
            ),
            "last_symbols": list(self._last_watchlist_integrity_symbols),
            "watchlist_pool_count": len(self._watchlist_symbols),
        }

    def _persist_watchlist_integrity_cursor(self):
        service_mod = _service_mod()
        try:
            self.pb.upsert_state(
                service_mod.BAR_INTEGRITY_STATE_KEY,
                service_mod.DATA_ENVIRONMENT,
                self._bar_integrity_cursor_payload(),
                date=service_mod.BAR_INTEGRITY_STATE_DATE,
            )
        except Exception as exc:
            service_mod.logger.warning("Failed to persist watchlist integrity cursor: %s", exc)

    def _restore_watchlist_integrity_cursor(self):
        service_mod = _service_mod()
        try:
            record = self.pb.get_state(
                service_mod.BAR_INTEGRITY_STATE_KEY,
                service_mod.DATA_ENVIRONMENT,
                date=service_mod.BAR_INTEGRITY_STATE_DATE,
            )
        except Exception as exc:
            service_mod.logger.warning("Failed to load watchlist integrity cursor: %s", exc)
            return

        payload = record.get("data") if isinstance(record, dict) else {}
        if not isinstance(payload, dict):
            return
        if str(payload.get("market_date") or "") != self._bar_integrity_market_date():
            self._watchlist_integrity_cursor = 0
            return

        try:
            self._watchlist_integrity_cursor = max(0, int(payload.get("cursor", 0) or 0))
        except Exception:
            self._watchlist_integrity_cursor = 0

    def _watchlist_integrity_candidates(self):
        service_mod = _service_mod()
        with self._subscription_lock:
            active_symbols = set(self._active_subscription_symbols)

        pool = [symbol for symbol in self._watchlist_symbols if symbol not in active_symbols]
        if not pool:
            return []

        batch_size = max(
            1,
            self.config.get_int_for_environment(
                "ibkr_watchlist_integrity_batch_size",
                service_mod.DATA_ENVIRONMENT,
                service_mod.DEFAULT_WATCHLIST_INTEGRITY_BATCH_SIZE,
            ),
        )
        start = self._watchlist_integrity_cursor % len(pool)
        ordered = pool[start:] + pool[:start]
        self._watchlist_integrity_cursor = (start + batch_size) % max(len(pool), 1)
        self._persist_watchlist_integrity_cursor()
        return ordered[:batch_size]

    def _watchlist_backfill_candidates(self):
        service_mod = _service_mod()
        with self._subscription_lock:
            active_symbols = set(self._active_subscription_symbols)

        pool = [symbol for symbol in self._watchlist_symbols if symbol not in active_symbols]
        if not pool:
            return []

        batch_size = max(1, self.config.get_int_for_environment("ibkr_watchlist_backfill_batch_size", service_mod.DATA_ENVIRONMENT, 12))
        start = self._watchlist_backfill_cursor % len(pool)
        ordered = pool[start:] + pool[:start]
        self._watchlist_backfill_cursor = (start + batch_size) % max(len(pool), 1)
        return ordered[:batch_size]

    def _initial_watchlist_idle_topup_state(self) -> dict:
        return {
            "enabled": True,
            "running": False,
            "status": "idle",
            "skip_reason": "",
            "last_admission": {},
            "last_started_at": "",
            "last_finished_at": "",
            "last_duration_s": 0.0,
            "last_symbol": "",
            "last_symbols": [],
            "last_written_bars": 0,
            "total_written_bars": 0,
            "cycle_count": 0,
            "skipped_count": 0,
            "error_count": 0,
            "last_error": "",
            "request_period": "1d",
            "mode": "continuous_until_active_due",
            "batch_size": 160,
            "max_symbols_per_cycle": 160,
            "dynamic_enabled": True,
            "active_first_enabled": True,
            "estimated_bars_budget": 0,
            "estimated_bars_selected": 0,
            "history_concurrency": 8,
            "request_spacing_s": 0.05,
            "selected_symbol_count": 0,
            "dynamic_reason": "",
            "loop_interval_sec": 2,
            "scheduler_mode": "idle_driven",
            "fast_retry_s": 0.25,
            "next_wait_s": None,
            "force_request_pending": False,
            "force_request_count": 0,
            "last_consumed_force_request_count": 0,
            "last_force_requested_at": "",
            "last_batches": [],
            "last_attempted_symbols": [],
            "last_attempted_symbols_total": 0,
            "last_processed_symbols": [],
            "last_processed_symbols_total": 0,
            "last_loaded_bars": 0,
            "last_request_count": 0,
            "last_stop_reason": "",
            "estimated_next_batch_s": 20.0,
            "seconds_until_next_active_5m_due": None,
            "active_target_count": 0,
            "no_data_cooldowns": {},
            "last_no_data_symbols": [],
            "last_no_data_reason": "",
            "inactive_symbols": {},
            "removal_candidates": [],
            "last_hygiene_notified_at_ms": 0,
            "last_hygiene_notified_symbols": [],
        }

    def _copy_watchlist_idle_topup_state(self, source: dict | None = None) -> dict:
        payload = source if source is not None else self._watchlist_idle_topup_state
        copied = {}
        for key, value in (payload or {}).items():
            if isinstance(value, dict):
                copied[key] = dict(value)
            elif isinstance(value, list):
                copied[key] = [dict(item) if isinstance(item, dict) else item for item in value]
            else:
                copied[key] = value
        return copied

    def _set_watchlist_idle_topup_state(self, **updates) -> dict:
        with self._watchlist_idle_topup_lock:
            next_state = self._copy_watchlist_idle_topup_state()
            for key, value in updates.items():
                if isinstance(value, dict):
                    next_state[key] = dict(value)
                elif isinstance(value, list):
                    next_state[key] = [dict(item) if isinstance(item, dict) else item for item in value]
                else:
                    next_state[key] = value
            self._watchlist_idle_topup_state = next_state
            return self._copy_watchlist_idle_topup_state(next_state)

    def _watchlist_idle_topup_enabled(self) -> bool:
        service_mod = _service_mod()
        return self.config.get_bool_for_environment(
            "ibkr_watchlist_idle_topup_enabled",
            service_mod.DATA_ENVIRONMENT,
            True,
        )

    def _watchlist_idle_topup_dynamic_enabled(self) -> bool:
        service_mod = _service_mod()
        return self.config.get_bool_for_environment(
            "ibkr_watchlist_idle_topup_dynamic_enabled",
            service_mod.DATA_ENVIRONMENT,
            True,
        )

    def _watchlist_idle_topup_active_first_enabled(self) -> bool:
        service_mod = _service_mod()
        return self.config.get_bool_for_environment(
            "ibkr_watchlist_idle_topup_active_first_enabled",
            service_mod.DATA_ENVIRONMENT,
            True,
        )

    def _watchlist_idle_topup_loop_interval_sec(self) -> int:
        service_mod = _service_mod()
        if self._watchlist_idle_topup_dynamic_enabled():
            return max(
                1,
                self.config.get_int_for_environment(
                    "ibkr_watchlist_idle_topup_dynamic_loop_interval_sec",
                    service_mod.DATA_ENVIRONMENT,
                    2,
                ),
            )
        return max(
            1,
            self.config.get_int_for_environment(
                "ibkr_watchlist_idle_topup_loop_interval_sec",
                service_mod.DATA_ENVIRONMENT,
                2,
            ),
        )

    def _watchlist_idle_topup_fast_retry_sec(self) -> float:
        service_mod = _service_mod()
        try:
            value = float(
                self.config.get_for_environment(
                    "ibkr_watchlist_idle_topup_fast_retry_sec",
                    service_mod.DATA_ENVIRONMENT,
                    "0.25",
                )
            )
        except Exception:
            value = 0.25
        return max(0.05, min(2.0, value))

    def _watchlist_idle_topup_next_wait_sec(self, state: dict | None = None) -> float:
        payload = state if isinstance(state, dict) else self._watchlist_idle_topup_status()
        loop_wait_s = float(self._watchlist_idle_topup_loop_interval_sec())
        fast_retry_s = self._watchlist_idle_topup_fast_retry_sec()
        status = str((payload or {}).get("status") or "").strip().lower()
        stop_reason = str((payload or {}).get("last_stop_reason") or "").strip().lower()
        skip_reason = str((payload or {}).get("skip_reason") or "").strip().lower()
        reason_text = stop_reason or skip_reason
        completion = (payload or {}).get("completion") if isinstance((payload or {}).get("completion"), dict) else {}
        if not completion:
            try:
                completion = self._watchlist_idle_topup_completion_snapshot()
            except Exception:
                completion = {}
        if any(key in completion for key in ("actionable_stale", "actionable_missing", "actionable_unobserved")):
            remaining = (
                _safe_int(completion.get("actionable_stale"), 0)
                + _safe_int(completion.get("actionable_missing"), 0)
                + _safe_int(completion.get("actionable_unobserved"), 0)
            )
        else:
            remaining = (
                _safe_int(completion.get("stale"), 0)
                + _safe_int(completion.get("missing"), 0)
                + _safe_int(completion.get("unobserved"), 0)
            )
        last_written = _safe_int((payload or {}).get("last_written_bars"), 0)
        admission = (payload or {}).get("last_admission") if isinstance((payload or {}).get("last_admission"), dict) else {}
        blocker_codes = {
            str((item or {}).get("code") or "").strip().lower()
            for item in (admission.get("blockers") or [])
            if isinstance(item, dict)
        }
        short_blockers = {
            "data_writer_busy",
            "official_5m_pending",
            "bar_repair_busy",
        }
        hard_blockers = {
            "disabled",
            "warmup_active",
            "session_unauthenticated",
            "websocket_not_ready",
            "active_5m_not_fresh",
            "resource_governor_denied",
        }

        if self._watchlist_idle_topup_force_catchup_active():
            return fast_retry_s
        if status == "running":
            return fast_retry_s
        if blocker_codes.intersection(short_blockers):
            return fast_retry_s
        if blocker_codes.intersection(hard_blockers):
            return loop_wait_s
        if status == "completed" and (last_written > 0 or reason_text == "max_symbols_per_cycle") and remaining > 0:
            return fast_retry_s
        if status == "skipped" and reason_text.startswith("admission_blocked:"):
            return fast_retry_s
        return loop_wait_s

    def _watchlist_idle_topup_dynamic_max_symbols_per_cycle(self) -> int:
        service_mod = _service_mod()
        return max(
            1,
            min(
                WATCHLIST_IDLE_TOPUP_MAX_SYMBOLS_HARD_CAP,
                self.config.get_int_for_environment(
                    "ibkr_watchlist_idle_topup_dynamic_max_symbols_per_cycle",
                    service_mod.DATA_ENVIRONMENT,
                    160,
                ),
            ),
        )

    def _watchlist_idle_topup_max_estimated_bars_per_cycle(self) -> int:
        service_mod = _service_mod()
        return max(
            0,
            self.config.get_int_for_environment(
                "ibkr_watchlist_idle_topup_max_estimated_bars_per_cycle",
                service_mod.DATA_ENVIRONMENT,
                0,
            ),
        )

    def _watchlist_idle_topup_max_symbols_per_cycle(self) -> int:
        service_mod = _service_mod()
        return max(
            0,
            self.config.get_int_for_environment(
                "ibkr_watchlist_idle_topup_max_symbols_per_cycle",
                service_mod.DATA_ENVIRONMENT,
                0,
            ),
        )

    def _watchlist_idle_topup_batch_size(self) -> int:
        service_mod = _service_mod()
        return max(
            1,
            min(
                WATCHLIST_IDLE_TOPUP_MAX_SYMBOLS_HARD_CAP,
                self.config.get_int_for_environment(
                    "ibkr_watchlist_idle_topup_batch_size",
                    service_mod.DATA_ENVIRONMENT,
                    160,
                ),
            ),
        )

    def _watchlist_idle_topup_history_concurrency(self) -> int:
        getter = getattr(getattr(self, "data_backfill", None), "_max_concurrency", None)
        if callable(getter):
            try:
                return int(getter())
            except Exception:
                pass
        service_mod = _service_mod()
        return max(
            1,
            min(
                10,
                self.config.get_int_for_environment(
                    "ibkr_history_max_concurrency",
                    service_mod.DATA_ENVIRONMENT,
                    8,
                ),
            ),
        )

    def _watchlist_idle_topup_history_request_spacing(self) -> float:
        getter = getattr(getattr(self, "data_backfill", None), "_request_spacing", None)
        if callable(getter):
            try:
                return round(max(0.0, float(getter())), 3)
            except Exception:
                pass
        service_mod = _service_mod()
        try:
            value = float(
                self.config.get_for_environment(
                    "ibkr_history_request_spacing",
                    service_mod.DATA_ENVIRONMENT,
                    "0.05",
                )
            )
        except Exception:
            value = 0.05
        return round(max(0.0, value), 3)

    def _watchlist_idle_topup_mode(
        self,
        active_target_count: int,
        active_due_guard_required: bool = True,
    ) -> str:
        if int(active_target_count or 0) <= 0:
            return "full_load_no_active_targets"
        return (
            "continuous_until_active_due"
            if bool(active_due_guard_required)
            else "full_load_off_active_window"
        )

    def _watchlist_idle_topup_estimated_batch_seconds(self, batch_summaries: list[dict] | None = None) -> float:
        durations = [
            float((item or {}).get("duration_s", 0) or 0)
            for item in (batch_summaries or [])
            if float((item or {}).get("duration_s", 0) or 0) > 0
        ]
        if durations:
            return round(max(5.0, min(45.0, (sum(durations[-3:]) / len(durations[-3:])) * 1.25)), 1)
        with self._watchlist_idle_topup_lock:
            previous = _safe_int(self._watchlist_idle_topup_state.get("estimated_next_batch_s"), 20)
        return float(max(5, min(45, previous or 20)))

    def _watchlist_idle_topup_due_budget_allows_batch(self, admission: dict, estimated_batch_s: float) -> tuple[bool, str]:
        return True, ""

    def _watchlist_idle_topup_force_catchup_active(self) -> bool:
        return (
            time.time() < float(getattr(self, "_watchlist_topup_force_until", 0.0) or 0.0)
            or self._watchlist_idle_topup_force_request_pending()
        )

    def _watchlist_idle_topup_force_request_pending(self) -> bool:
        return int(getattr(self, "_watchlist_topup_request_count", 0) or 0) > int(
            getattr(self, "_watchlist_topup_last_consumed_request_count", 0) or 0
        )

    def _consume_watchlist_idle_topup_force_request(self) -> None:
        self._watchlist_topup_last_consumed_request_count = int(
            getattr(self, "_watchlist_topup_request_count", 0) or 0
        )
        try:
            self._set_watchlist_idle_topup_state(
                force_request_pending=False,
                force_request_count=int(getattr(self, "_watchlist_topup_request_count", 0) or 0),
                last_consumed_force_request_count=int(
                    getattr(self, "_watchlist_topup_last_consumed_request_count", 0) or 0
                ),
            )
        except Exception:
            pass

    def _request_watchlist_idle_topup_now(self, *, ttl_s: float = 15.0) -> None:
        self._watchlist_topup_force_until = max(
            float(getattr(self, "_watchlist_topup_force_until", 0.0) or 0.0),
            time.time() + max(1.0, float(ttl_s or 0.0)),
        )
        self._watchlist_topup_requested_at = time.time()
        self._watchlist_topup_request_count = int(getattr(self, "_watchlist_topup_request_count", 0) or 0) + 1
        try:
            self._set_watchlist_idle_topup_state(
                force_request_pending=True,
                force_request_count=int(getattr(self, "_watchlist_topup_request_count", 0) or 0),
                last_force_requested_at=self._now_iso(),
            )
        except Exception:
            pass
        wakeup = getattr(self, "_watchlist_topup_wakeup", None)
        if wakeup is not None and hasattr(wakeup, "set"):
            wakeup.set()

    def _watchlist_idle_topup_request_period(self) -> str:
        service_mod = _service_mod()
        value = self.config.get_for_environment(
            "ibkr_watchlist_idle_topup_request_period",
            service_mod.DATA_ENVIRONMENT,
            "1d",
        )
        return str(value or "1d").strip() or "1d"

    def _watchlist_idle_topup_stale_ms(self) -> int:
        service_mod = _service_mod()
        stale_minutes = max(
            5,
            self.config.get_int_for_environment(
                "ibkr_watchlist_idle_topup_stale_min",
                service_mod.DATA_ENVIRONMENT,
                20,
            ),
        )
        return stale_minutes * 60 * 1000

    def _watchlist_idle_topup_no_data_cooldown_min(self, market_session: str = "") -> int:
        service_mod = _service_mod()
        key = (
            "ibkr_watchlist_idle_topup_regular_no_data_cooldown_min"
            if str(market_session or "").strip().lower() == "regular"
            else "ibkr_watchlist_idle_topup_no_data_cooldown_min"
        )
        default = 5 if key.endswith("regular_no_data_cooldown_min") else 15
        return max(1, self.config.get_int_for_environment(key, service_mod.DATA_ENVIRONMENT, default))

    def _watchlist_inactive_no_data_count_threshold(self) -> int:
        service_mod = _service_mod()
        return max(
            1,
            self.config.get_int_for_environment(
                "ibkr_watchlist_inactive_no_data_count_threshold",
                service_mod.DATA_ENVIRONMENT,
                3,
            ),
        )

    def _watchlist_inactive_days_threshold(self) -> int:
        service_mod = _service_mod()
        return max(
            1,
            self.config.get_int_for_environment(
                "ibkr_watchlist_inactive_days_threshold",
                service_mod.DATA_ENVIRONMENT,
                3,
            ),
        )

    def _watchlist_hygiene_notify_cooldown_ms(self) -> int:
        service_mod = _service_mod()
        hours = max(
            1,
            self.config.get_int_for_environment(
                "ibkr_watchlist_hygiene_notify_cooldown_hours",
                service_mod.DATA_ENVIRONMENT,
                24,
            ),
        )
        return hours * 60 * 60 * 1000

    def _watchlist_idle_topup_active_trade_symbol_set(self) -> set[str]:
        with self._subscription_lock:
            return set(self._normalize_symbol_list(getattr(self, "_active_trade_symbols", []) or []))

    def _watchlist_idle_topup_symbol_manual_member(self, symbol: str) -> bool:
        normalized = str(symbol or "").strip().upper()
        record = (getattr(self, "_watchlist_records", {}) or {}).get(normalized) or {}
        if "manual_member" not in record:
            return True
        value = record.get("manual_member")
        return str(value).strip().lower() not in {"0", "false", "no", "off"}

    def _watchlist_idle_topup_cooldowns(self) -> dict:
        with self._watchlist_idle_topup_lock:
            return {
                str(symbol or "").strip().upper(): dict(item or {})
                for symbol, item in dict(self._watchlist_idle_topup_state.get("no_data_cooldowns") or {}).items()
                if str(symbol or "").strip()
            }

    def _watchlist_idle_topup_no_data_active(self, symbol: str, *, now_ms: int | None = None) -> bool:
        normalized = str(symbol or "").strip().upper()
        if not normalized:
            return False
        cooldown = self._watchlist_idle_topup_cooldowns().get(normalized) or {}
        return _safe_int(cooldown.get("cooldown_until_ms"), 0) > int(now_ms or time.time() * 1000)

    def _watchlist_idle_topup_clear_no_data_symbols(self, symbols) -> None:
        normalized_symbols = set(self._normalize_symbol_list(symbols or []))
        if not normalized_symbols:
            return
        with self._watchlist_idle_topup_lock:
            cooldowns = dict(self._watchlist_idle_topup_state.get("no_data_cooldowns") or {})
            changed = False
            for symbol in normalized_symbols:
                if symbol in cooldowns:
                    cooldowns.pop(symbol, None)
                    changed = True
            if changed:
                removal_candidates = [
                    dict(item or {})
                    for item in cooldowns.values()
                    if bool((item or {}).get("removal_candidate"))
                ]
                self._watchlist_idle_topup_state = self._copy_watchlist_idle_topup_state(
                    {
                        **self._watchlist_idle_topup_state,
                        "no_data_cooldowns": cooldowns,
                        "inactive_symbols": cooldowns,
                        "removal_candidates": removal_candidates,
                    }
                )

    def _watchlist_idle_topup_latest_trace(self) -> dict:
        status_fn = getattr(getattr(self, "data_backfill", None), "status", None)
        if not callable(status_fn):
            return {}
        try:
            status = status_fn() or {}
        except Exception:
            return {}
        traces = []
        last_trace = status.get("last_trace") if isinstance(status.get("last_trace"), dict) else {}
        if last_trace:
            traces.append(last_trace)
        traces.extend(
            trace for trace in (status.get("recent_traces") or [])
            if isinstance(trace, dict)
        )
        for trace in reversed(traces):
            if str((trace or {}).get("source") or "") == "watchlist_idle_topup":
                return dict(trace or {})
        return {}

    def _watchlist_idle_topup_hmds_no_data_from_trace(self, symbols) -> dict:
        requested = set(self._normalize_symbol_list(symbols or []))
        if not requested:
            return {}
        trace = self._watchlist_idle_topup_latest_trace()
        if not trace:
            return {}
        found: dict[str, dict] = {}
        for item in trace.get("symbol_outcomes") or []:
            symbol = str((item or {}).get("symbol") or "").strip().upper()
            if symbol in requested and bool((item or {}).get("hmds_no_data")):
                found[symbol] = {
                    "symbol": symbol,
                    "last_error": str((item or {}).get("last_error") or "HMDS query returned no data"),
                    "request_count": _safe_int((item or {}).get("request_count"), 0),
                    "rows": _safe_int((item or {}).get("rows"), 0),
                }
        for item in trace.get("request_samples") or []:
            symbol = str((item or {}).get("symbol") or "").strip().upper()
            error = str((item or {}).get("error") or "")
            if symbol in requested and "hmds query returned no data" in error.lower():
                found.setdefault(
                    symbol,
                    {
                        "symbol": symbol,
                        "last_error": error,
                        "request_count": 1,
                        "rows": _safe_int((item or {}).get("rows"), 0),
                    },
                )
        return found

    def _watchlist_idle_topup_record_no_data_symbols(self, no_data_by_symbol: dict, market_session: str) -> None:
        if not no_data_by_symbol:
            return
        now_ms = int(time.time() * 1000)
        session_kind = str(market_session or "").strip().lower() or "unknown"
        reason = "regular_no_data" if session_kind == "regular" else "extended_hours_no_data"
        cooldown_ms = self._watchlist_idle_topup_no_data_cooldown_min(session_kind) * 60 * 1000
        active_trade_symbols = self._watchlist_idle_topup_active_trade_symbol_set()
        market_date = self._watchlist_idle_topup_target_date()
        count_threshold = self._watchlist_inactive_no_data_count_threshold()
        days_threshold = self._watchlist_inactive_days_threshold()
        with self._watchlist_idle_topup_lock:
            cooldowns = {
                str(symbol or "").strip().upper(): dict(item or {})
                for symbol, item in dict(self._watchlist_idle_topup_state.get("no_data_cooldowns") or {}).items()
                if str(symbol or "").strip()
            }
            for symbol, info in no_data_by_symbol.items():
                normalized = str(symbol or "").strip().upper()
                if not normalized or normalized in active_trade_symbols:
                    continue
                previous = dict(cooldowns.get(normalized) or {})
                seen_dates = [
                    str(item or "")
                    for item in (previous.get("seen_dates") or [])
                    if str(item or "")
                ]
                if market_date and market_date not in seen_dates:
                    seen_dates.append(market_date)
                consecutive_count = _safe_int(previous.get("consecutive_count"), 0) + 1
                manual_member = self._watchlist_idle_topup_symbol_manual_member(normalized)
                removal_candidate = consecutive_count >= count_threshold or len(seen_dates) >= days_threshold
                cooldowns[normalized] = {
                    **previous,
                    "symbol": normalized,
                    "reason": reason,
                    "session": session_kind,
                    "first_seen_at_ms": _safe_int(previous.get("first_seen_at_ms"), now_ms) or now_ms,
                    "last_seen_at_ms": now_ms,
                    "cooldown_until_ms": now_ms + cooldown_ms,
                    "consecutive_count": consecutive_count,
                    "seen_dates": seen_dates[-10:],
                    "last_error": str((info or {}).get("last_error") or "HMDS query returned no data"),
                    "manual_member": manual_member,
                    "inactive_candidate": True,
                    "removal_candidate": bool(removal_candidate),
                    "auto_remove_allowed": False,
                }
            removal_candidates = [
                dict(item or {})
                for item in cooldowns.values()
                if bool((item or {}).get("removal_candidate"))
            ]
            self._watchlist_idle_topup_state = self._copy_watchlist_idle_topup_state(
                {
                    **self._watchlist_idle_topup_state,
                    "no_data_cooldowns": cooldowns,
                    "last_no_data_symbols": sorted(no_data_by_symbol.keys()),
                    "last_no_data_reason": reason,
                    "inactive_symbols": cooldowns,
                    "removal_candidates": removal_candidates,
                }
            )
        self._notify_watchlist_hygiene_candidates(removal_candidates)

    def _notify_watchlist_hygiene_candidates(self, candidates: list[dict]) -> None:
        active = [dict(item or {}) for item in (candidates or []) if (item or {}).get("symbol")]
        if not active:
            return
        now_ms = int(time.time() * 1000)
        symbols = sorted({str((item or {}).get("symbol") or "").strip().upper() for item in active if (item or {}).get("symbol")})
        with self._watchlist_idle_topup_lock:
            last_ms = _safe_int(self._watchlist_idle_topup_state.get("last_hygiene_notified_at_ms"), 0)
            last_symbols = set(self._normalize_symbol_list(self._watchlist_idle_topup_state.get("last_hygiene_notified_symbols") or []))
            if now_ms - last_ms < self._watchlist_hygiene_notify_cooldown_ms() and set(symbols).issubset(last_symbols):
                return
            self._watchlist_idle_topup_state = self._copy_watchlist_idle_topup_state(
                {
                    **self._watchlist_idle_topup_state,
                    "last_hygiene_notified_at_ms": now_ms,
                    "last_hygiene_notified_symbols": symbols,
                }
            )
        pb = getattr(self, "pb", None) or getattr(getattr(self, "data_backfill", None), "pb_client", None)
        notifier = getattr(pb, "notify_system_event", None)
        if not callable(notifier):
            return
        service_mod = _service_mod()
        sample = active[:12]
        try:
            notifier(
                "标的池治理建议 / Watchlist hygiene: 扩展时段无数据",
                {
                    "结论 / Conclusion": "部分 watchlist 标的多次返回 HMDS no data，建议检查是否继续保留。",
                    "非系统故障说明 / Not a system fault": "盘前/盘后可能无成交或 IBKR 没有可用历史 bars。",
                    "建议动作 / Next action": "确认后可从 trade watchlist 移除；manual_member=true 的标的只建议人工检查。",
                    "标的 / Symbols": ", ".join(symbols[:20]),
                    "数量 / Count": len(symbols),
                    "样本 / Sample": sample,
                },
                event_type="alert",
                level="warning",
                source="ibkr_compute",
                environment=str(getattr(service_mod, "ENVIRONMENT", "") or "paper").strip().lower() or "paper",
            )
        except Exception as exc:
            service_mod.logger.warning("Watchlist hygiene notification failed: %s", exc)

    def _watchlist_idle_topup_expected_5m_ms(self, *, now_ms: int | None = None) -> int:
        service_mod = _service_mod()
        try:
            delay_seconds = self._official_5m_close_delay_sec()
        except Exception:
            delay_seconds = self.config.get_int_for_environment(
                "ibkr_official_5m_close_delay_sec",
                service_mod.DATA_ENVIRONMENT,
                DEFAULT_CLOSE_DELAY_SECONDS,
            )
        return int(
            latest_expected_extended_5m_ms(
                now_ms=now_ms,
                delay_seconds=max(0, int(delay_seconds or 0)),
            )
            or 0
        )

    def _watchlist_idle_topup_latest_is_stale(
        self,
        latest_ms: int,
        *,
        expected_5m_ms: int,
        now_ms: int,
    ) -> bool:
        latest_ms = _safe_int(latest_ms, 0)
        expected_5m_ms = _safe_int(expected_5m_ms, 0)
        if latest_ms <= 0:
            return True
        if expected_5m_ms > 0:
            return latest_ms < expected_5m_ms
        return (int(now_ms or time.time() * 1000) - latest_ms) >= self._watchlist_idle_topup_stale_ms()

    def _watchlist_active_due_guard_sec(self) -> int:
        service_mod = _service_mod()
        return max(
            0,
            self.config.get_int_for_environment(
                "ibkr_watchlist_active_due_guard_sec",
                service_mod.DATA_ENVIRONMENT,
                180,
            ),
        )

    def _seconds_until_next_active_5m_due(self, now_ts: float | None = None) -> float:
        current_ts = float(now_ts or time.time())
        interval_ms = interval_to_ms("5m")
        now_ms = int(current_ts * 1000)
        try:
            current_bucket_ms = bucket_start_ms(now_ms, "5m")
        except Exception:
            return 0.0
        next_due_ms = current_bucket_ms + interval_ms + (self._official_5m_close_delay_sec() * 1000)
        while next_due_ms <= now_ms:
            next_due_ms += interval_ms
        return round(max(0.0, (next_due_ms - now_ms) / 1000.0), 1)

    def _watchlist_idle_topup_completion_snapshot(self, *, now_ms: int | None = None) -> dict:
        current_ms = int(now_ms or time.time() * 1000)
        expected_5m_ms = self._watchlist_idle_topup_expected_5m_ms(now_ms=current_ms)
        with self._subscription_lock:
            active_symbols = set(self._active_subscription_symbols)
            total = len([symbol for symbol in self._watchlist_symbols if symbol not in active_symbols])
        with self._watchlist_idle_topup_lock:
            observations = {
                symbol: item
                for symbol, item in dict(self._watchlist_idle_observations).items()
                if symbol not in active_symbols
            }
            cooldowns = {
                str(symbol or "").strip().upper(): dict(item or {})
                for symbol, item in dict(self._watchlist_idle_topup_state.get("no_data_cooldowns") or {}).items()
                if str(symbol or "").strip().upper() and str(symbol or "").strip().upper() not in active_symbols
            }
            removal_candidates = [
                dict(item or {})
                for item in (self._watchlist_idle_topup_state.get("removal_candidates") or [])
                if str((item or {}).get("symbol") or "").strip().upper() not in active_symbols
            ]

        fresh = 0
        stale = 0
        missing = 0
        explained_no_data = 0
        explained_no_data_symbols: list[str] = []
        oldest_ms = 0
        oldest_symbol = ""
        for symbol, item in observations.items():
            latest_ms = _safe_int((item or {}).get("latest_ms"), 0)
            cooldown = cooldowns.get(symbol) or {}
            no_data_active = _safe_int(cooldown.get("cooldown_until_ms"), 0) > current_ms
            if latest_ms <= 0:
                if no_data_active:
                    explained_no_data += 1
                    explained_no_data_symbols.append(symbol)
                else:
                    missing += 1
                continue
            if self._watchlist_idle_topup_latest_is_stale(
                latest_ms,
                expected_5m_ms=expected_5m_ms,
                now_ms=current_ms,
            ):
                if no_data_active:
                    explained_no_data += 1
                    explained_no_data_symbols.append(symbol)
                else:
                    stale += 1
            else:
                fresh += 1
            if oldest_ms <= 0 or latest_ms < oldest_ms:
                oldest_ms = latest_ms
                oldest_symbol = symbol

        observed = len(observations)
        unobserved = max(0, total - observed)
        inactive_symbols = sorted(
            symbol for symbol, item in cooldowns.items()
            if bool((item or {}).get("inactive_candidate"))
        )
        removal_symbols = sorted(
            str((item or {}).get("symbol") or "").strip().upper()
            for item in removal_candidates
            if str((item or {}).get("symbol") or "").strip()
        )
        return {
            "total": total,
            "observed": observed,
            "fresh": fresh,
            "stale": stale,
            "missing": missing,
            "unobserved": unobserved,
            "explained_no_data_count": explained_no_data,
            "explained_no_data_symbols_sample": sorted(explained_no_data_symbols)[:12],
            "inactive_candidate_count": len(inactive_symbols),
            "inactive_symbols_sample": inactive_symbols[:12],
            "removal_candidate_count": len(removal_symbols),
            "removal_candidate_symbols_sample": removal_symbols[:12],
            "actionable_stale": stale,
            "actionable_missing": missing,
            "actionable_unobserved": unobserved,
            "progress_pct": round((fresh / total) * 100.0, 2) if total > 0 else 100.0,
            "observed_pct": round((observed / total) * 100.0, 2) if total > 0 else 100.0,
            "oldest_symbol": oldest_symbol,
            "oldest_latest_ms": oldest_ms,
            "oldest_latest_us": format_us_time(oldest_ms) if oldest_ms > 0 else "",
            "expected_latest_5m_ms": expected_5m_ms,
            "expected_latest_5m_us": format_us_time(expected_5m_ms) if expected_5m_ms > 0 else "",
        }

    def _watchlist_idle_topup_status(self) -> dict:
        with self._watchlist_idle_topup_lock:
            state = self._copy_watchlist_idle_topup_state()
        state["completion"] = self._watchlist_idle_topup_completion_snapshot()
        return state

    def _watchlist_idle_topup_compute_inflight(self) -> bool:
        return bool(
            float(self._last_realtime_compute_started_at or 0.0)
            and float(self._last_realtime_compute_started_at or 0.0) > float(self._last_realtime_compute_at or 0.0)
        )

    def _watchlist_idle_topup_data_writer_busy(self) -> tuple[bool, dict]:
        try:
            status = self.data_writer.status()
        except Exception as exc:
            return True, {"error": str(exc), "pending_batch": 0, "inflight_batch": 0}
        pending = _safe_int(status.get("pending_batch"), 0)
        inflight = _safe_int(status.get("inflight_batch"), 0)
        return pending > 0 or inflight > 0, {"pending_batch": pending, "inflight_batch": inflight}

    def _watchlist_idle_topup_bar_repair_busy(self) -> tuple[bool, dict]:
        coordinator = getattr(self, "bar_repair_coordinator", None)
        if coordinator is None or not hasattr(coordinator, "status"):
            return False, {"pending": 0, "inflight": 0}
        try:
            status = coordinator.status()
        except Exception as exc:
            return True, {"error": str(exc), "pending": 0, "inflight": 0}
        pending = _safe_int(status.get("pending"), 0)
        inflight = _safe_int(status.get("inflight"), 0)
        return pending > 0 or inflight > 0, {"pending": pending, "inflight": inflight}

    def _watchlist_idle_topup_admission(self) -> tuple[bool, dict]:
        service_mod = _service_mod()
        blockers = []
        official_5m = self._copy_official_5m_state()
        pending_symbols = self._normalize_symbol_list(official_5m.get("pending_symbols") or [])
        queue_size = int(self._compute_queue.qsize())
        compute_inflight = self._watchlist_idle_topup_compute_inflight()
        writer_busy, writer_status = self._watchlist_idle_topup_data_writer_busy()
        bar_repair_busy, bar_repair_status = self._watchlist_idle_topup_bar_repair_busy()
        resource_governor = self._resource_governor_snapshot()
        resource_admission = (
            (resource_governor.get("admission") or {}).get("watchlist_idle_topup") or {}
        )
        seconds_until_due = self._seconds_until_next_active_5m_due()
        due_guard_sec = self._watchlist_active_due_guard_sec()
        websocket_status = {}
        try:
            websocket_status = self.ws_client.status()
        except Exception:
            websocket_status = {}
        authenticated = bool(getattr(self.session_keeper, "is_authenticated", False))
        websocket_ready = bool(websocket_status.get("connected") or websocket_status.get("ready"))
        active_subscription_count = len(self._active_subscription_symbols)
        active_target_count = len(self._active_trade_symbols)
        market_session = service_mod.build_market_session_snapshot()
        market_session_kind = str(market_session.get("kind") or "").strip().lower()
        active_first_enabled = self._watchlist_idle_topup_active_first_enabled()
        active_due_guard_required = bool(
            active_first_enabled
            and
            market_session_kind in {"regular", "close_transition"}
            and active_target_count > 0
        )
        lag_s = 0.0
        due_bucket_ms = _safe_int(official_5m.get("last_due_bucket_ms"), 0)
        completed_bucket_ms = _safe_int(official_5m.get("last_completed_bucket_ms"), 0)
        if due_bucket_ms > completed_bucket_ms:
            lag_s = round(max(0.0, (due_bucket_ms - completed_bucket_ms) / 1000.0), 1)

        if not self._watchlist_idle_topup_enabled():
            blockers.append({"code": "disabled"})
        if self._is_warmup_active():
            blockers.append({"code": "warmup_active"})
        if not authenticated:
            blockers.append({"code": "session_unauthenticated"})
        if active_due_guard_required and active_subscription_count > 0 and not websocket_ready:
            blockers.append({"code": "websocket_not_ready"})
        if active_due_guard_required and pending_symbols:
            blockers.append({"code": "official_5m_pending", "pending_symbols": pending_symbols})
        post_close_catchup = bool(
            active_due_guard_required
            and not pending_symbols
            and completed_bucket_ms > 0
            and due_bucket_ms > 0
            and completed_bucket_ms >= due_bucket_ms
            and self._watchlist_idle_topup_force_catchup_active()
        )
        if writer_busy:
            blockers.append({"code": "data_writer_busy", **writer_status})
        if active_due_guard_required and bar_repair_busy:
            blockers.append({"code": "bar_repair_busy", **bar_repair_status})
        if active_due_guard_required and (completed_bucket_ms <= 0 or lag_s > 90):
            blockers.append(
                {
                    "code": "active_5m_not_fresh",
                    "lag_s": lag_s,
                    "last_completed_bucket_ms": completed_bucket_ms,
                }
            )
        if not bool(resource_admission.get("admit")):
            blockers.append(
                {
                    "code": "resource_governor_denied",
                    "status": resource_governor.get("status"),
                    "blockers": list(resource_admission.get("blockers") or []),
                }
            )

        snapshot = {
            "admit": not blockers,
            "blockers": blockers,
            "queue_size": queue_size,
            "compute_inflight": compute_inflight,
            "data_writer": writer_status,
            "bar_repair_queue": bar_repair_status,
            "official_5m_pending_symbols": pending_symbols,
            "active_target_count": active_target_count,
            "active_subscription_count": active_subscription_count,
            "authenticated": authenticated,
            "websocket_ready": websocket_ready,
            "seconds_until_next_active_5m_due": seconds_until_due,
            "active_due_guard_sec": due_guard_sec,
            "active_due_guard_required": active_due_guard_required,
            "active_first_enabled": active_first_enabled,
            "market_session": market_session_kind,
            "resource_governor": resource_governor,
            "post_close_catchup": post_close_catchup,
        }
        return not blockers, snapshot

    def _observe_watchlist_idle_symbol(self, symbol: str, latest_ms: int):
        normalized = str(symbol or "").strip().upper()
        if not normalized:
            return
        with self._watchlist_idle_topup_lock:
            self._watchlist_idle_observations[normalized] = {
                "latest_ms": int(latest_ms or 0),
                "observed_at": self._now_iso(),
            }

    def _watchlist_idle_topup_candidates(
        self,
        *,
        exclude_symbols: set[str] | None = None,
        scan_all: bool = False,
        scan_size_override: int | None = None,
    ) -> list[dict]:
        service_mod = _service_mod()
        with self._subscription_lock:
            active_symbols = set(self._active_subscription_symbols)
        excluded = {
            str(symbol or "").strip().upper()
            for symbol in (exclude_symbols or set())
            if str(symbol or "").strip()
        }

        pool = [
            symbol for symbol in self._watchlist_symbols
            if symbol not in active_symbols and symbol not in excluded
        ]
        if not pool:
            return []

        scan_size = max(
            1,
            _safe_int(scan_size_override, 0)
            if scan_size_override is not None
            else self.config.get_int_for_environment(
                    "ibkr_watchlist_idle_topup_candidate_scan_size",
                    service_mod.DATA_ENVIRONMENT,
                    24,
                ),
        )
        start = self._watchlist_idle_topup_cursor % len(pool)
        ordered = pool[start:] + pool[:start]
        scanned = ordered if scan_all else ordered[: min(scan_size, len(ordered))]
        self._watchlist_idle_topup_cursor = (start + len(scanned)) % max(len(pool), 1)
        now_ms = int(time.time() * 1000)
        expected_5m_ms = self._watchlist_idle_topup_expected_5m_ms(now_ms=now_ms)
        latest_map = {}
        latest_map_getter = getattr(self.data_backfill, "get_latest_stored_bar_ms_map", None)
        if callable(latest_map_getter):
            try:
                latest_map = {
                    str(symbol or "").strip().upper(): _safe_int(value, 0)
                    for symbol, value in latest_map_getter(scanned, "5m").items()
                }
            except Exception:
                latest_map = {}
        candidates = []
        for symbol in scanned:
            if symbol in latest_map:
                latest_ms = _safe_int(latest_map.get(symbol), 0)
            else:
                latest_ms = self.data_backfill.get_latest_stored_bar_ms(symbol, "5m")
            self._observe_watchlist_idle_symbol(symbol, latest_ms)
            is_stale = self._watchlist_idle_topup_latest_is_stale(
                latest_ms,
                expected_5m_ms=expected_5m_ms,
                now_ms=now_ms,
            )
            if self._watchlist_idle_topup_no_data_active(symbol, now_ms=now_ms):
                if not is_stale:
                    self._watchlist_idle_topup_clear_no_data_symbols([symbol])
                continue
            if is_stale:
                candidates.append(
                    {
                        "symbol": symbol,
                        "latest_ms": latest_ms,
                        "missing": latest_ms <= 0,
                        "expected_latest_5m_ms": expected_5m_ms,
                        "expected_latest_5m_us": format_us_time(expected_5m_ms) if expected_5m_ms > 0 else "",
                        "stale_by_s": round(max(0, expected_5m_ms - max(0, latest_ms)) / 1000.0, 1)
                        if expected_5m_ms > 0 and latest_ms > 0 else None,
                        "stale_age_s": round(max(0, now_ms - max(0, latest_ms)) / 1000.0, 1)
                        if latest_ms > 0 else None,
                    }
                )

        candidates.sort(key=lambda item: (0 if item.get("missing") else 1, _safe_int(item.get("latest_ms"), 0)))
        return candidates

    def _watchlist_idle_topup_estimated_missing_bars(
        self,
        candidate: dict | None,
        *,
        now_ms: int | None = None,
    ) -> int:
        interval_ms = interval_to_ms("5m")
        current_ms = int(now_ms or time.time() * 1000)
        expected_5m_ms = self._watchlist_idle_topup_expected_5m_ms(now_ms=current_ms)
        latest_ms = _safe_int((candidate or {}).get("latest_ms"), 0)
        if latest_ms <= 0:
            return 150
        comparison_ms = expected_5m_ms if expected_5m_ms > 0 else current_ms
        missing = int(max(1, (comparison_ms - latest_ms + interval_ms - 1) // interval_ms))
        return max(1, min(150, missing))

    def _watchlist_idle_topup_dynamic_candidates(
        self,
        *,
        exclude_symbols: set[str] | None = None,
        scan_all: bool = False,
        max_symbols: int,
        remaining_estimated_bars: int,
    ) -> tuple[list[dict], int, str]:
        max_symbols = max(1, int(max_symbols or 1))
        remaining_estimated_bars = int(remaining_estimated_bars or 0)
        if remaining_estimated_bars <= 0:
            remaining_estimated_bars = 1_000_000_000

        scan_size = max(
            max_symbols,
            self._watchlist_idle_topup_dynamic_max_symbols_per_cycle(),
            self._watchlist_idle_topup_history_concurrency(),
        )
        candidates = self._watchlist_idle_topup_candidates(
            exclude_symbols=exclude_symbols,
            scan_all=scan_all,
            scan_size_override=scan_size,
        )
        selected: list[dict] = []
        selected_estimated_bars = 0
        reason = "no_candidates"
        for item in candidates:
            if len(selected) >= max_symbols:
                reason = "max_symbols_per_cycle"
                break
            estimated_missing = self._watchlist_idle_topup_estimated_missing_bars(item)
            if selected and selected_estimated_bars + estimated_missing > remaining_estimated_bars:
                reason = "estimated_bars_budget"
                break
            if not selected and estimated_missing > remaining_estimated_bars:
                reason = "estimated_bars_budget"
                break
            selected_item = dict(item or {})
            selected_item["estimated_missing_bars"] = estimated_missing
            selected.append(selected_item)
            selected_estimated_bars += estimated_missing
            reason = ""

        if not selected and candidates:
            reason = reason or "estimated_bars_budget"
        return selected, selected_estimated_bars, reason

    def _watchlist_idle_topup_target_date(self) -> str:
        current = str(getattr(self, "_current_market_date", "") or "").strip()
        if current:
            return current
        market_date_fn = getattr(self, "_market_date", None)
        if callable(market_date_fn):
            try:
                current = str(market_date_fn() or "").strip()
            except Exception:
                current = ""
            if current:
                return current
        return datetime.now(_service_mod().ET).strftime("%Y-%m-%d")

    def _watchlist_idle_topup_monitor_symbols(self) -> set[str]:
        symbols = set(self._normalize_symbol_list(getattr(self, "_watchlist_monitor_symbols", []) or []))
        market_ws_symbols = getattr(self, "_market_ws_symbols", None)
        if callable(market_ws_symbols):
            try:
                symbols.update(self._normalize_symbol_list(market_ws_symbols() or []))
            except Exception:
                pass
        return symbols

    def _watchlist_idle_topup_active_signal_symbols(self) -> list[str]:
        with self._subscription_lock:
            signal_symbols = set(self._normalize_symbol_list(getattr(self, "_active_trade_symbols", []) or []))

        pb = getattr(self, "pb", None)
        if pb is None:
            return sorted(signal_symbols)

        service_mod = _service_mod()
        safe_env = str(
            getattr(service_mod, "DATA_ENVIRONMENT", None)
            or getattr(service_mod, "ENVIRONMENT", None)
            or "live"
        ).strip().lower().replace('"', '\\"')
        safe_date = self._watchlist_idle_topup_target_date().replace('"', '\\"')
        try:
            rows = pb.get_all_records(
                "ibkr_targets",
                filter=(
                    f'date = "{safe_date}" && '
                    f'environment = "{safe_env}" && '
                    'status = "active"'
                ),
                sort="-score,-updated",
                max_pages=10,
            )
        except Exception as exc:
            service_mod.logger.warning("Failed to load active target symbols for idle topup signals: %s", exc)
            return sorted(signal_symbols)

        monitor_symbols = self._watchlist_idle_topup_monitor_symbols()
        for row in rows or []:
            symbol = str((row or {}).get("symbol", "") or "").strip().upper()
            if (
                not symbol
                or symbol in monitor_symbols
                or symbol in WATCHLIST_IDLE_TOPUP_SIGNAL_BLOCKED_SYMBOLS
                or str((row or {}).get("status", "") or "").strip().lower() != "active"
            ):
                continue
            if _target_row_is_daily_scan_active(row) or _target_row_is_manual(row):
                signal_symbols.add(symbol)
        return sorted(signal_symbols)

    def _run_watchlist_idle_topup_cycle(self) -> dict:
        service_mod = _service_mod()
        if not hasattr(self, "_watchlist_idle_topup_state"):
            self._watchlist_idle_topup_state = self._initial_watchlist_idle_topup_state()

        enabled = self._watchlist_idle_topup_enabled()
        dynamic_enabled = self._watchlist_idle_topup_dynamic_enabled()
        active_first_enabled = self._watchlist_idle_topup_active_first_enabled()
        loop_interval_sec = self._watchlist_idle_topup_loop_interval_sec()
        configured_max_symbols = self._watchlist_idle_topup_max_symbols_per_cycle()
        if dynamic_enabled:
            max_symbols = (
                max(1, min(WATCHLIST_IDLE_TOPUP_MAX_SYMBOLS_HARD_CAP, configured_max_symbols))
                if configured_max_symbols > 0
                else self._watchlist_idle_topup_dynamic_max_symbols_per_cycle()
            )
            batch_size = max(1, min(WATCHLIST_IDLE_TOPUP_MAX_SYMBOLS_HARD_CAP, max_symbols))
            estimated_bars_budget = self._watchlist_idle_topup_max_estimated_bars_per_cycle()
        else:
            batch_size = self._watchlist_idle_topup_batch_size()
            max_symbols = configured_max_symbols
            estimated_bars_budget = 0
        history_concurrency = self._watchlist_idle_topup_history_concurrency()
        request_spacing_s = self._watchlist_idle_topup_history_request_spacing()
        fast_retry_s = self._watchlist_idle_topup_fast_retry_sec()
        request_period = self._watchlist_idle_topup_request_period()
        self._set_watchlist_idle_topup_state(
            enabled=enabled,
            loop_interval_sec=loop_interval_sec,
            scheduler_mode="idle_driven",
            fast_retry_s=fast_retry_s,
            batch_size=batch_size,
            max_symbols_per_cycle=max_symbols,
            dynamic_enabled=dynamic_enabled,
            active_first_enabled=active_first_enabled,
            estimated_bars_budget=estimated_bars_budget,
            estimated_bars_selected=0,
            history_concurrency=history_concurrency,
            request_spacing_s=request_spacing_s,
            selected_symbol_count=0,
            dynamic_reason="",
            request_period=request_period,
        )
        if not enabled:
            state = self._set_watchlist_idle_topup_state(
                running=False,
                status="skipped",
                skip_reason="disabled",
                last_stop_reason="disabled",
                skipped_count=_safe_int(self._watchlist_idle_topup_state.get("skipped_count"), 0) + 1,
            )
            return state

        admitted, admission = self._watchlist_idle_topup_admission()
        active_target_count = _safe_int(admission.get("active_target_count"), len(getattr(self, "_active_trade_symbols", []) or []))
        active_due_guard_required = bool(admission.get("active_due_guard_required"))
        mode = self._watchlist_idle_topup_mode(active_target_count, active_due_guard_required)
        seconds_until_due = admission.get("seconds_until_next_active_5m_due")
        estimated_next_batch_s = self._watchlist_idle_topup_estimated_batch_seconds([])
        self._set_watchlist_idle_topup_state(
            mode=mode,
            active_target_count=active_target_count,
            seconds_until_next_active_5m_due=seconds_until_due,
            estimated_next_batch_s=estimated_next_batch_s,
            last_admission=admission,
        )
        if not admitted:
            reason = str(((admission.get("blockers") or [{}])[0] or {}).get("code") or "admission_blocked")
            state = self._set_watchlist_idle_topup_state(
                running=False,
                status="skipped",
                skip_reason=reason,
                last_stop_reason=reason,
                last_admission=admission,
                skipped_count=_safe_int(self._watchlist_idle_topup_state.get("skipped_count"), 0) + 1,
            )
            service_mod.logger.debug("Watchlist idle topup skipped: %s", reason)
            return state

        budget_ok, budget_reason = self._watchlist_idle_topup_due_budget_allows_batch(
            admission,
            estimated_next_batch_s,
        )
        if not budget_ok:
            state = self._set_watchlist_idle_topup_state(
                running=False,
                status="skipped",
                skip_reason=budget_reason,
                last_stop_reason=budget_reason,
                last_admission=admission,
                skipped_count=_safe_int(self._watchlist_idle_topup_state.get("skipped_count"), 0) + 1,
            )
            service_mod.logger.debug("Watchlist idle topup paused before active due: %s", budget_reason)
            return state

        self._refresh_watchlist_pool()
        started = time.time()
        started_at = self._now_iso()
        written_total = 0
        processed_symbols: list[str] = []
        attempted_symbols: list[str] = []
        attempted_set: set[str] = set()
        batch_summaries: list[dict] = []
        request_count = 0
        estimated_bars_selected = 0
        stop_reason = ""
        last_error = ""
        no_data_symbols_total: set[str] = set()
        full_load_candidates: list[dict] | None = None
        full_load_candidate_initialized = False

        self._set_watchlist_idle_topup_state(
            running=True,
            status="running",
            skip_reason="",
            last_stop_reason="",
            last_error="",
            last_admission=admission,
            last_started_at=started_at,
            last_batches=[],
            last_attempted_symbols=[],
            last_attempted_symbols_total=0,
            last_processed_symbols=[],
            last_processed_symbols_total=0,
            last_loaded_bars=0,
            last_request_count=0,
            estimated_bars_selected=0,
            selected_symbol_count=0,
            dynamic_reason="",
        )
        if self._watchlist_idle_topup_force_request_pending():
            self._consume_watchlist_idle_topup_force_request()

        try:
            while True:
                if getattr(self, "_running", True) is False:
                    stop_reason = "service_stopping"
                    break

                admitted, admission = self._watchlist_idle_topup_admission()
                active_target_count = _safe_int(admission.get("active_target_count"), len(getattr(self, "_active_trade_symbols", []) or []))
                active_due_guard_required = bool(admission.get("active_due_guard_required"))
                mode = self._watchlist_idle_topup_mode(active_target_count, active_due_guard_required)
                seconds_until_due = admission.get("seconds_until_next_active_5m_due")
                estimated_next_batch_s = self._watchlist_idle_topup_estimated_batch_seconds(batch_summaries)
                if not admitted:
                    blocker = str(((admission.get("blockers") or [{}])[0] or {}).get("code") or "admission_blocked")
                    stop_reason = f"admission_blocked:{blocker}"
                    break

                budget_ok, budget_reason = self._watchlist_idle_topup_due_budget_allows_batch(
                    admission,
                    estimated_next_batch_s,
                )
                if not budget_ok:
                    stop_reason = budget_reason
                    break

                if max_symbols > 0 and len(attempted_set) >= max_symbols:
                    stop_reason = "max_symbols_per_cycle"
                    break

                scan_all = active_target_count <= 0 or not active_due_guard_required
                batch_estimated_bars = 0
                dynamic_reason = ""
                if dynamic_enabled:
                    remaining_symbols = max_symbols - len(attempted_set)
                    if remaining_symbols <= 0:
                        stop_reason = "max_symbols_per_cycle"
                        break
                    remaining_estimated_bars = (
                        estimated_bars_budget - estimated_bars_selected
                        if estimated_bars_budget > 0
                        else 0
                    )
                    if estimated_bars_budget > 0 and remaining_estimated_bars <= 0:
                        stop_reason = "estimated_bars_budget"
                        break
                    batch_candidates, batch_estimated_bars, dynamic_reason = self._watchlist_idle_topup_dynamic_candidates(
                        exclude_symbols=attempted_set,
                        scan_all=scan_all,
                        max_symbols=remaining_symbols,
                        remaining_estimated_bars=remaining_estimated_bars,
                    )
                    if not batch_candidates:
                        stop_reason = (
                            dynamic_reason
                            or ("no_stale_or_missing_symbols" if not processed_symbols else "no_candidates")
                        )
                        break
                    batch_symbols = [
                        str((item or {}).get("symbol") or "").strip().upper()
                        for item in batch_candidates
                        if str((item or {}).get("symbol") or "").strip()
                    ]
                    batch_symbols = [symbol for symbol in batch_symbols if symbol not in attempted_set]
                elif scan_all:
                    if not full_load_candidate_initialized:
                        full_load_candidates = self._watchlist_idle_topup_candidates(
                            exclude_symbols=attempted_set,
                            scan_all=True,
                        )
                        full_load_candidate_initialized = True
                    candidates = [
                        item for item in (full_load_candidates or [])
                        if str((item or {}).get("symbol") or "").strip().upper() not in attempted_set
                    ]
                    if not candidates:
                        stop_reason = "no_stale_or_missing_symbols" if not processed_symbols else "no_candidates"
                        break

                    remaining = max_symbols - len(attempted_set) if max_symbols > 0 else batch_size
                    take_count = min(batch_size, max(1, remaining))
                    batch_candidates = candidates[:take_count]
                    for item in batch_candidates:
                        batch_estimated_bars += self._watchlist_idle_topup_estimated_missing_bars(item)
                    batch_symbols = [
                        str((item or {}).get("symbol") or "").strip().upper()
                        for item in batch_candidates
                        if str((item or {}).get("symbol") or "").strip()
                    ]
                    batch_symbols = [symbol for symbol in batch_symbols if symbol not in attempted_set]
                else:
                    candidates = self._watchlist_idle_topup_candidates(
                        exclude_symbols=attempted_set,
                        scan_all=False,
                    )
                    if not candidates:
                        stop_reason = "no_stale_or_missing_symbols" if not processed_symbols else "no_candidates"
                        break

                    remaining = max_symbols - len(attempted_set) if max_symbols > 0 else batch_size
                    take_count = min(batch_size, max(1, remaining))
                    batch_candidates = candidates[:take_count]
                    for item in batch_candidates:
                        batch_estimated_bars += self._watchlist_idle_topup_estimated_missing_bars(item)
                    batch_symbols = [
                        str((item or {}).get("symbol") or "").strip().upper()
                        for item in batch_candidates
                        if str((item or {}).get("symbol") or "").strip()
                    ]
                    batch_symbols = [symbol for symbol in batch_symbols if symbol not in attempted_set]
                if not batch_symbols:
                    stop_reason = "no_candidates"
                    break

                for symbol in batch_symbols:
                    attempted_set.add(symbol)
                    attempted_symbols.append(symbol)
                estimated_bars_selected += int(batch_estimated_bars or 0)

                batch_started = time.perf_counter()
                raw_conid_map = self.conid_resolver.resolve_bulk(batch_symbols)
                conid_map = {}
                unresolved_symbols = []
                for symbol in batch_symbols:
                    conid = int((raw_conid_map or {}).get(symbol) or 0)
                    if conid > 0:
                        conid_map[symbol] = conid
                    else:
                        unresolved_symbols.append(symbol)
                if unresolved_symbols:
                    service_mod.logger.info(
                        "Watchlist idle topup unresolved conids: %s",
                        ",".join(unresolved_symbols),
                    )

                batch_written = 0
                flush_ok = True
                batch_no_data_symbols: list[str] = []
                batch_explained_no_data_symbols: list[str] = []
                batch_active_no_data_symbols: list[str] = []
                if conid_map:
                    conid_symbols = list(conid_map.keys())
                    symbol_meta = {
                        symbol: dict((getattr(self, "_symbol_meta", {}) or {}).get(symbol, {}) or {})
                        for symbol in conid_symbols
                    }
                    period_overrides = {
                        symbol: {"5m": request_period}
                        for symbol in conid_symbols
                    }
                    latest_before = {}
                    latest_map_getter = getattr(self.data_backfill, "get_latest_stored_bar_ms_map", None)
                    if callable(latest_map_getter):
                        try:
                            latest_before = {
                                str(symbol or "").strip().upper(): _safe_int(value, 0)
                                for symbol, value in latest_map_getter(conid_symbols, "5m").items()
                            }
                        except Exception:
                            latest_before = {}
                    for symbol in conid_symbols:
                        if symbol not in latest_before:
                            latest_before[symbol] = self.data_backfill.get_latest_stored_bar_ms(symbol, "5m")
                    completion_before = {}
                    try:
                        completion_before = self._watchlist_idle_topup_completion_snapshot()
                    except Exception:
                        completion_before = {}
                    market_session_kind = str(admission.get("market_session") or "").strip().lower()
                    trace_context = {
                        "trigger_type": "scheduled_auto_topup",
                        "trigger_reason": "watchlist 5m bars missing/stale",
                        "trigger_reason_cn": "自动回补 watchlist 中缺失或落后的 5m bars",
                        "trigger_explanation_cn": "这是运行时空闲自动拉取，不是人工手动触发；盘前/盘后标的不活跃时可能返回 no data。",
                        "trigger_explanation_en": "This is scheduled idle top-up, not a manual trigger; inactive premarket/afterhours symbols can return no data.",
                        "loop_interval_sec": loop_interval_sec,
                        "fast_retry_s": fast_retry_s,
                        "request_period": request_period,
                        "bar_interval": "5m",
                        "mode": mode,
                        "dynamic_enabled": dynamic_enabled,
                        "active_first_enabled": active_first_enabled,
                        "max_symbols_per_cycle": max_symbols,
                        "history_concurrency": history_concurrency,
                        "request_spacing_s": request_spacing_s,
                        "market_session": market_session_kind,
                        "watchlist_completion_before": completion_before,
                    }
                    try:
                        backfill_kwargs = {
                            "symbol_meta": symbol_meta,
                            "intervals": ["5m"],
                            "repair_symbols": [],
                            "period_overrides": period_overrides,
                            "trace_source": "watchlist_idle_topup",
                            "trace_context": trace_context,
                        }
                        results = self.data_backfill.backfill_all(conid_map, **backfill_kwargs)
                    except TypeError as exc:
                        message = str(exc)
                        if "trace_context" in message:
                            backfill_kwargs.pop("trace_context", None)
                        elif "trace_source" in message:
                            backfill_kwargs.pop("trace_context", None)
                            backfill_kwargs.pop("trace_source", None)
                        else:
                            raise
                        results = self.data_backfill.backfill_all(conid_map, **backfill_kwargs)
                    batch_written = sum(
                        int((per_symbol or {}).get("5m", 0) or 0)
                        for per_symbol in (results or {}).values()
                    )
                    request_count += len(conid_map)
                    if hasattr(self.data_writer, "flush"):
                        flush_ok = bool(self.data_writer.flush())
                    if not flush_ok:
                        last_error = "flush_failed"

                    latest_after = {}
                    if callable(latest_map_getter):
                        try:
                            latest_after = {
                                str(symbol or "").strip().upper(): _safe_int(value, 0)
                                for symbol, value in latest_map_getter(conid_symbols, "5m").items()
                            }
                        except Exception:
                            latest_after = {}
                    progressed_symbols: list[str] = []
                    for symbol in conid_symbols:
                        latest_ms = (
                            _safe_int(latest_after.get(symbol), 0)
                            if symbol in latest_after
                            else self.data_backfill.get_latest_stored_bar_ms(symbol, "5m")
                        )
                        latest_after[symbol] = latest_ms
                        self._observe_watchlist_idle_symbol(symbol, latest_ms)
                        if latest_ms > _safe_int(latest_before.get(symbol), 0) or _safe_int(
                            ((results or {}).get(symbol) or {}).get("5m"),
                            0,
                        ) > 0:
                            progressed_symbols.append(symbol)
                        if symbol not in processed_symbols:
                            processed_symbols.append(symbol)
                    if progressed_symbols:
                        self._watchlist_idle_topup_clear_no_data_symbols(progressed_symbols)
                    trace_no_data = self._watchlist_idle_topup_hmds_no_data_from_trace(conid_symbols)
                    no_progress_symbols = {
                        symbol for symbol in conid_symbols
                        if _safe_int(latest_after.get(symbol), 0) <= _safe_int(latest_before.get(symbol), 0)
                    }
                    no_data_by_symbol = {
                        symbol: info
                        for symbol, info in trace_no_data.items()
                        if symbol in no_progress_symbols
                    }
                    if no_data_by_symbol:
                        active_trade_symbols = self._watchlist_idle_topup_active_trade_symbol_set()
                        batch_no_data_symbols = sorted(no_data_by_symbol.keys())
                        batch_active_no_data_symbols = sorted(
                            symbol for symbol in batch_no_data_symbols if symbol in active_trade_symbols
                        )
                        batch_explained_no_data_symbols = sorted(
                            symbol for symbol in batch_no_data_symbols if symbol not in active_trade_symbols
                        )
                        no_data_symbols_total.update(batch_no_data_symbols)
                        self._watchlist_idle_topup_record_no_data_symbols(no_data_by_symbol, market_session_kind)
                    self._last_backfill_at = time.time()
                    self._last_backfill_symbols = list(conid_symbols)

                    materialize_enabled = self.config.get_bool_for_environment(
                        "ibkr_watchlist_idle_topup_materialize_5m",
                        service_mod.DATA_ENVIRONMENT,
                        True,
                    )
                    rollup_without_new_bars = self.config.get_bool_for_environment(
                        "ibkr_watchlist_idle_topup_rollup_without_new_bars",
                        service_mod.DATA_ENVIRONMENT,
                        True,
                    )
                    should_trigger_compute = bool(
                        conid_map
                        and materialize_enabled
                        and (batch_written or rollup_without_new_bars)
                    )
                    if should_trigger_compute:
                        admitted_after_write, _ = self._watchlist_idle_topup_admission()
                        if admitted_after_write:
                            active_signal_symbols = self._watchlist_idle_topup_active_signal_symbols()
                            compute_symbols = sorted(
                                set(self._normalize_symbol_list(list(conid_map.keys()) + active_signal_symbols))
                            )
                            compute_symbol_set = set(compute_symbols)
                            persist_signal_symbols = [
                                symbol for symbol in active_signal_symbols if symbol in compute_symbol_set
                            ]
                            self._trigger_realtime_compute(
                                source="watchlist_idle_topup",
                                symbols=compute_symbols,
                                persist_signals=bool(persist_signal_symbols),
                                persist_signal_symbols=persist_signal_symbols,
                                intervals=["5m"],
                                rollup_intervals=list(WATCHLIST_IDLE_TOPUP_ROLLUP_INTERVALS),
                            )

                batch_duration_s = round(max(0.0, time.perf_counter() - batch_started), 3)
                written_total += int(batch_written or 0)
                batch_summary = {
                    "index": len(batch_summaries) + 1,
                    "symbols": list(batch_symbols),
                    "resolved_symbols": list(conid_map.keys()),
                    "unresolved_symbols": unresolved_symbols,
                    "written_bars": int(batch_written or 0),
                    "estimated_missing_bars": int(batch_estimated_bars or 0),
                    "dynamic_reason": dynamic_reason,
                    "duration_s": batch_duration_s,
                    "flush_ok": flush_ok,
                    "no_data_symbols": list(batch_no_data_symbols),
                    "explained_no_data_symbols": list(batch_explained_no_data_symbols),
                    "active_no_data_symbols": list(batch_active_no_data_symbols),
                }
                batch_summaries.append(batch_summary)
                estimated_next_batch_s = self._watchlist_idle_topup_estimated_batch_seconds(batch_summaries)
                self._set_watchlist_idle_topup_state(
                    mode=mode,
                    active_target_count=active_target_count,
                    seconds_until_next_active_5m_due=seconds_until_due,
                    estimated_next_batch_s=estimated_next_batch_s,
                    last_admission=admission,
                    last_batches=batch_summaries[-12:],
                    last_attempted_symbols=list(attempted_symbols),
                    last_attempted_symbols_total=len(attempted_symbols),
                    last_processed_symbols=list(processed_symbols),
                    last_processed_symbols_total=len(processed_symbols),
                    last_symbols=list(processed_symbols),
                    last_loaded_bars=written_total,
                    last_written_bars=written_total,
                    last_request_count=request_count,
                    estimated_bars_selected=estimated_bars_selected,
                    selected_symbol_count=len(attempted_symbols),
                    dynamic_reason=dynamic_reason,
                    last_no_data_symbols=sorted(no_data_symbols_total),
                    last_no_data_reason=(
                        "regular_no_data"
                        if no_data_symbols_total and str(admission.get("market_session") or "").strip().lower() == "regular"
                        else ("extended_hours_no_data" if no_data_symbols_total else "")
                    ),
                )

                if max_symbols > 0 and len(attempted_set) >= max_symbols:
                    stop_reason = "max_symbols_per_cycle"
                    break
        except Exception as exc:
            state = self._set_watchlist_idle_topup_state(
                running=False,
                status="error",
                skip_reason="",
                last_stop_reason="error",
                last_error=str(exc),
                error_count=_safe_int(self._watchlist_idle_topup_state.get("error_count"), 0) + 1,
                last_finished_at=self._now_iso(),
                last_duration_s=round(max(0.0, time.time() - started), 3),
                last_batches=batch_summaries[-12:],
                last_attempted_symbols=list(attempted_symbols),
                last_attempted_symbols_total=len(attempted_symbols),
                last_processed_symbols=list(processed_symbols),
                last_processed_symbols_total=len(processed_symbols),
                last_loaded_bars=written_total,
                last_request_count=request_count,
                last_no_data_symbols=sorted(no_data_symbols_total),
                last_no_data_reason=(
                    "regular_no_data"
                    if no_data_symbols_total and str(admission.get("market_session") or "").strip().lower() == "regular"
                    else ("extended_hours_no_data" if no_data_symbols_total else "")
                ),
                estimated_bars_selected=estimated_bars_selected,
                selected_symbol_count=len(attempted_symbols),
                dynamic_reason=stop_reason if dynamic_enabled else "",
            )
            service_mod.logger.warning("Watchlist idle topup failed: %s", exc)
            return state

        if not stop_reason:
            stop_reason = "completed"
        status = "completed" if processed_symbols else "skipped"
        skip_reason = "" if processed_symbols else stop_reason
        if not processed_symbols and attempted_symbols and stop_reason in {"no_candidates", "no_stale_or_missing_symbols"}:
            skip_reason = "no_resolved_symbols"
            stop_reason = "no_resolved_symbols"
        state = self._set_watchlist_idle_topup_state(
            running=False,
            status=status,
            skip_reason=skip_reason,
            last_stop_reason=stop_reason,
            last_admission=admission,
            last_finished_at=self._now_iso(),
            last_duration_s=round(max(0.0, time.time() - started), 3),
            last_symbol=processed_symbols[-1] if processed_symbols else "",
            last_symbols=list(processed_symbols),
            last_attempted_symbols=list(attempted_symbols),
            last_attempted_symbols_total=len(attempted_symbols),
            last_processed_symbols=list(processed_symbols),
            last_processed_symbols_total=len(processed_symbols),
            last_written_bars=written_total,
            last_loaded_bars=written_total,
            last_request_count=request_count,
            last_batches=batch_summaries[-12:],
            last_error=last_error,
            last_no_data_symbols=sorted(no_data_symbols_total),
            last_no_data_reason=(
                "regular_no_data"
                if no_data_symbols_total and str(admission.get("market_session") or "").strip().lower() == "regular"
                else ("extended_hours_no_data" if no_data_symbols_total else "")
            ),
            total_written_bars=_safe_int(self._watchlist_idle_topup_state.get("total_written_bars"), 0) + written_total,
            cycle_count=_safe_int(self._watchlist_idle_topup_state.get("cycle_count"), 0) + 1,
            skipped_count=(
                _safe_int(self._watchlist_idle_topup_state.get("skipped_count"), 0)
                + (0 if processed_symbols else 1)
            ),
            mode=mode,
            active_target_count=active_target_count,
            seconds_until_next_active_5m_due=seconds_until_due,
            estimated_next_batch_s=self._watchlist_idle_topup_estimated_batch_seconds(batch_summaries),
            estimated_bars_selected=estimated_bars_selected,
            selected_symbol_count=len(attempted_symbols),
            dynamic_reason=stop_reason if dynamic_enabled else "",
        )
        service_mod.logger.info(
            "Watchlist idle topup cycle finished: mode=%s dynamic=%s status=%s stop=%s attempted=%d processed=%d written=%d estimated_bars=%d batches=%d next_due_s=%s estimate_s=%.1f",
            mode,
            dynamic_enabled,
            status,
            stop_reason,
            len(attempted_symbols),
            len(processed_symbols),
            written_total,
            estimated_bars_selected,
            len(batch_summaries),
            seconds_until_due,
            float(state.get("estimated_next_batch_s", 0) or 0),
        )
        return state

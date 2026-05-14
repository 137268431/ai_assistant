from __future__ import annotations

from ibkr_compute.market.pocketbase_sqlite import normalize_exchange_value

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

from . import market_universe_support as _market_universe_support


def _service_mod():
    # Keep tests and legacy callers that patch market_universe._service_mod effective.
    import sys

    facade = sys.modules.get(f"{__package__}.market_universe")
    patched = getattr(facade, "_service_mod", None) if facade is not None else None
    if patched is not None:
        return patched()
    return _market_universe_support._service_mod()


class TradingServiceMarketUniverseTargetsMixin:
    def _environment_watchlist_filter(self) -> str:
        service_mod = _service_mod()
        safe_env = str(service_mod.ENVIRONMENT or "live").strip().lower().replace('"', '\\"')
        return f'environment = "{safe_env}" || environment = "global" || environment = ""'

    def _watchlist_record_role(self, row: dict) -> str:
        service_mod = _service_mod()
        return service_mod.normalize_watchlist_symbol_role((row or {}).get("symbol_role"))

    def _watchlist_metadata_fallbacks(self, symbols) -> dict:
        symbol_set = {
            str(symbol or "").strip().upper()
            for symbol in (symbols or [])
            if str(symbol or "").strip()
        }
        if not symbol_set:
            return {}

        fallbacks = {}
        try:
            rows = self.pb.get_all_records("ibkr_fundamentals", max_pages=30)
        except Exception as exc:
            service_mod = _service_mod()
            service_mod.logger.debug("Failed to load watchlist metadata fallbacks: %s", exc)
            return {}

        for row in rows or []:
            symbol = str((row or {}).get("symbol") or "").strip().upper()
            if not symbol or symbol not in symbol_set or symbol in fallbacks:
                continue
            exchange = normalize_exchange_value((row or {}).get("exchange"))
            industry = str((row or {}).get("industry") or (row or {}).get("profile") or "").strip()
            if exchange or industry:
                fallbacks[symbol] = {
                    "exchange": exchange,
                    "industry": industry,
                }
        return fallbacks

    def _refresh_watchlist_pool(self, force: bool = False):
        service_mod = _service_mod()
        refresh_minutes = max(1, self.config.get_int_for_environment("watchlist_interval_min", service_mod.ENVIRONMENT, 5))
        now = time.time()
        if (
            not force
            and self._watchlist_symbols
            and (now - self._last_watchlist_refresh_at) < (refresh_minutes * 60)
        ):
            return

        service_mod.logger.info("Refreshing watchlist pool for env=%s", service_mod.ENVIRONMENT)
        merged = {}
        applied = {}
        priority = {"": 0, "global": 1, str(service_mod.ENVIRONMENT or "live").strip().lower(): 2}

        try:
            rows = self.pb.get_all_records(
                "watchlist",
                filter=self._environment_watchlist_filter(),
                max_pages=20,
            )
            for row in rows:
                symbol = str(row.get("symbol", "")).upper()
                if not symbol:
                    continue
                row_env = str(row.get("environment", "") or "").strip().lower()
                rank = priority.get(row_env, -1)
                if symbol in applied and applied[symbol] > rank:
                    continue
                applied[symbol] = rank
                merged[symbol] = row
        except Exception as exc:
            service_mod.logger.error("Failed to refresh watchlist pool: %s", exc)
            return

        fallback_meta = self._watchlist_metadata_fallbacks(merged.keys())
        symbol_meta = {}
        trade_symbols = []
        monitor_symbols = []
        for symbol, row in merged.items():
            symbol_role = self._watchlist_record_role(row)
            fallback = fallback_meta.get(symbol) or {}
            exchange = normalize_exchange_value(row.get("exchange"), default=str(fallback.get("exchange") or ""))
            industry = str(row.get("industry") or fallback.get("industry") or "")
            symbol_meta[symbol] = {
                "exchange": exchange,
                "industry": industry,
                "symbol_role": symbol_role,
            }
            if symbol_role == service_mod.WATCHLIST_SYMBOL_ROLE_MARKET_MONITOR:
                monitor_symbols.append(symbol)
            else:
                trade_symbols.append(symbol)

        self._watchlist_records = merged
        self._watchlist_symbols = sorted(merged.keys())
        self._watchlist_trade_symbols = sorted(trade_symbols)
        self._watchlist_monitor_symbols = sorted(monitor_symbols)
        self._symbol_meta = symbol_meta
        self._last_watchlist_refresh_at = now
        service_mod.logger.info(
            "Watchlist pool refreshed: %d symbols (%d trade / %d monitor)",
            len(self._watchlist_symbols),
            len(self._watchlist_trade_symbols),
            len(self._watchlist_monitor_symbols),
        )

    def _get_target_subscription_limit(self) -> int:
        service_mod = _service_mod()
        return max(0, self.config.get_int_for_environment("ibkr_target_subscription_limit", service_mod.ENVIRONMENT, 80))

    def _get_total_subscription_limit(self) -> int:
        service_mod = _service_mod()
        return max(0, self.config.get_int_for_environment("ibkr_total_subscription_limit", service_mod.ENVIRONMENT, 80))

    def _get_trade_subscription_budget(self) -> int | None:
        target_limit = self._get_target_subscription_limit()
        total_limit = self._get_total_subscription_limit()
        trade_budget = target_limit if target_limit > 0 else None
        if total_limit > 0:
            remaining_budget = max(0, total_limit - len(self._market_ws_symbols()))
            trade_budget = remaining_budget if trade_budget is None else min(trade_budget, remaining_budget)
        return trade_budget

    def _parse_hhmm(self, raw_value) -> tuple[int, int] | None:
        text = str(raw_value or "").strip()
        if not text:
            return None
        try:
            hour_text, minute_text = text.split(":", 1)
            hour = int(hour_text)
            minute = int(minute_text)
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return hour, minute
        except Exception:
            return None
        return None

    def _today_target_rows(self):
        service_mod = _service_mod()
        today = datetime.now(service_mod.ET).strftime("%Y-%m-%d")
        safe_env = str(service_mod.ENVIRONMENT or "live").strip().lower().replace('"', '\\"')
        rows = self.pb.get_all_records(
            "ibkr_targets",
            filter=(
                f'date = "{today}" && '
                f'environment = "{safe_env}" && '
                '(status = "candidate" || status = "active")'
            ),
            sort="-score,-updated",
            max_pages=10,
        )
        return today, rows

    def _build_target_subscription_plan(self):
        service_mod = _service_mod()
        target_date, rows = self._today_target_rows()
        trade_budget = self._get_trade_subscription_budget()
        monitor_symbols = set(self._market_ws_symbols())
        selected_symbols = []
        selected_meta = {}
        selected_rows = []
        seen = set()

        active_rows = [
            row
            for row in rows
            if str(row.get("status", "") or "").strip().lower() == "active"
            and _target_row_is_daily_scan_active(row)
        ]
        prioritized_rows = list(active_rows)

        for row in prioritized_rows:
            symbol = str(row.get("symbol", "")).upper()
            if not symbol or symbol in seen:
                continue
            if symbol in monitor_symbols:
                service_mod.logger.warning(
                    "Skipping market context symbol %s from active trade target plan",
                    symbol,
                )
                continue
            if trade_budget is not None and len(selected_rows) >= trade_budget:
                break

            watchlist_row = self._watchlist_records.get(symbol) or {}
            selected_symbols.append(symbol)
            selected_rows.append(row)
            selected_meta[symbol] = {
                "exchange": str(
                    row.get("exchange")
                    or watchlist_row.get("exchange")
                    or ""
                ).upper(),
                "industry": str(
                    watchlist_row.get("industry")
                    or ""
                ),
            }
            seen.add(symbol)

        for symbol in self._market_ws_symbols():
            if symbol in seen:
                continue
            selected_symbols.append(symbol)
            selected_meta[symbol] = {
                "exchange": str(
                    self._symbol_meta.get(symbol, {}).get("exchange")
                    or ""
                ).upper(),
                "industry": str(
                    self._symbol_meta.get(symbol, {}).get("industry")
                    or ""
                ),
            }
            seen.add(symbol)

        return target_date, selected_symbols, selected_meta, selected_rows

    def _mark_target_statuses(self, target_date: str, selected_rows):
        service_mod = _service_mod()
        safe_env = str(service_mod.ENVIRONMENT or "live").strip().lower().replace('"', '\\"')
        monitor_symbols = set(self._market_ws_symbols())
        try:
            existing = self.pb.get_all_records(
                "ibkr_targets",
                filter=(
                    f'date = "{target_date}" && '
                    f'environment = "{safe_env}" && '
                    '(status = "candidate" || status = "active")'
                ),
                max_pages=10,
            )
        except Exception as exc:
            service_mod.logger.warning("Failed to load target rows for status sync: %s", exc)
            return

        selected_rank_by_id = {
            str(row.get("id") or ""): rank
            for rank, row in enumerate(selected_rows, start=1)
            if str(row.get("id") or "")
        }
        for row in existing:
            record_id = str(row.get("id") or "")
            if not record_id:
                continue
            symbol = str(row.get("symbol", "") or "").strip().upper()
            if symbol in monitor_symbols:
                current = str(row.get("status", "") or "").strip().lower()
                extra = _safe_extra(row)
                extra.update(
                    {
                        "blocked_from_trading": True,
                        "block_reason": "market_context_symbol",
                        "within_subscription_budget": False,
                        "subscription_rank": 0,
                        "subscription_selected": False,
                    }
                )
                update_data = {"extra": extra}
                if current != "candidate":
                    update_data["status"] = "candidate"
                try:
                    self.pb.update_record("ibkr_targets", record_id, update_data)
                except Exception as exc:
                    service_mod.logger.warning("Failed to block market context target %s: %s", record_id, exc)
                continue
            subscription_rank = selected_rank_by_id.get(record_id, 0)
            subscription_selected = subscription_rank > 0
            extra = _safe_extra(row)
            if str(extra.get("block_reason") or "").strip().lower() == "market_context_symbol":
                extra.pop("blocked_from_trading", None)
                extra.pop("block_reason", None)
            extra.update(
                {
                    "within_subscription_budget": subscription_selected,
                    "subscription_rank": subscription_rank,
                    "subscription_selected": subscription_selected,
                }
            )
            desired = "active"
            current = str(row.get("status", "") or "").strip().lower()
            if current == desired and extra == _safe_extra(row):
                continue
            try:
                update_data = {"extra": extra}
                if current != desired:
                    update_data["status"] = desired
                self.pb.update_record("ibkr_targets", record_id, update_data)
            except Exception as exc:
                service_mod.logger.warning("Failed to update target status %s -> %s: %s", record_id, desired, exc)

    def _scan_schedule_start(self) -> tuple[int, int]:
        service_mod = _service_mod()
        preferred = self._parse_hhmm(
            self.config.get_for_environment("ibkr_daily_scan_time_et", service_mod.ENVIRONMENT, "09:20")
        )
        if preferred:
            return preferred
        raw_schedule = str(
            self.config.get_for_environment("ibkr_scan_schedule", service_mod.ENVIRONMENT, "09:20-10:00") or ""
        ).strip()
        start_text = raw_schedule.split("-", 1)[0].strip() or "09:20"
        scheduled = self._parse_hhmm(start_text)
        if scheduled:
            return scheduled
        return 9, 20

    def _scan_window_open(self) -> bool:
        service_mod = _service_mod()
        hour, minute = self._scan_schedule_start()
        now_et = datetime.now(service_mod.ET)
        return (now_et.hour, now_et.minute) >= (hour, minute)

    def _run_daily_scan_if_due(self, reason: str = "poll") -> dict:
        service_mod = _service_mod()
        self._refresh_watchlist_pool()
        market_date = self._current_market_date or self._market_date()
        state = self._copy_daily_scan_state()
        if str(state.get("market_date") or "") != market_date:
            state = self._set_daily_scan_state(**self._initial_daily_scan_state(market_date))

        if not self._scan_window_open():
            return {"ok": True, "skipped": True, "reason": "scan_window_not_open", "state": state}
        state_status = str(state.get("status") or "").strip().lower()
        if state_status == "retry_wait":
            if self._daily_scan_retry_wait_pending(state):
                return {"ok": True, "skipped": True, "reason": "daily_scan_retry_wait", "state": state}
            state = self._set_daily_scan_state(
                market_date=market_date,
                status="idle",
                reason=reason,
                started_at="",
                finished_at="",
                last_error="",
                next_retry_at="",
            )
            state_status = "idle"

        if state_status in {"pending", "running"} and str(state.get("run_id") or "").strip():
            try:
                from ibkr_compute.api.service_topology import uses_remote_compute_service

                if uses_remote_compute_service():
                    return self._poll_daily_scan_attempt(market_date=market_date, reason=reason, state=state)
            except Exception:
                pass

        if state_status == "failed":
            return {"ok": False, "skipped": True, "reason": "daily_scan_failed", "state": state}

        if state_status == "running":
            et_zone = getattr(service_mod, "ET", None)
            running_age = _daily_scan_running_age_seconds(
                state,
                datetime.now(et_zone) if et_zone is not None else datetime.now(),
            )
            if running_age is None or running_age < DAILY_SCAN_RUNNING_STALE_SECONDS:
                return {"ok": True, "skipped": True, "reason": "scan_running", "state": state}
            service_mod.logger.warning(
                "Daily scan running state is stale; retrying: market_date=%s age=%.1fs",
                market_date,
                running_age,
            )
            state = self._set_daily_scan_state(
                market_date=market_date,
                status="failed",
                reason=str(state.get("reason") or reason or "poll"),
                finished_at=self._now_iso(),
                last_error="stale_running_timeout",
                result={
                    "ok": False,
                    "error": "stale_running_timeout",
                    "previous_started_at": str(state.get("started_at") or ""),
                    "age_seconds": round(running_age, 3),
                },
            )
        if str(state.get("status") or "").strip().lower() == "completed":
            return {"ok": True, "skipped": True, "reason": "scan_already_completed", "state": state}

        warmup_state = self._copy_warmup_state()
        symbols_total = int(warmup_state.get("symbols_total", 0) or 0)
        if symbols_total <= 0:
            return {"ok": True, "skipped": True, "reason": "no_data_symbols", "state": state}
        blocking_pending_symbols = self._non_monitor_pending_symbols(
            warmup_state.get("pending_symbols") or [],
            warmup_state.get("monitor_symbols") or [],
        )
        if blocking_pending_symbols:
            return {
                "ok": True,
                "skipped": True,
                "reason": "data_warmup_incomplete",
                "blocking_symbols": blocking_pending_symbols,
                "state": state,
            }

        if not self._watchlist_trade_symbols:
            completed_state = self._set_daily_scan_state(
                market_date=market_date,
                status="completed",
                reason=reason,
                started_at=self._now_iso(),
                finished_at=self._now_iso(),
                last_error="",
                result={
                    "ok": True,
                    "date": market_date,
                    "scanned": 0,
                    "eligible": 0,
                    "active": 0,
                    "candidates": 0,
                    "removed": 0,
                    "errors": 0,
                    "rejection_summary": {},
                    "rejection_examples": [],
                    "environments": [service_mod.ENVIRONMENT],
                },
            )
            self._notify_daily_scan_recovered(completed_state)
            self._last_target_refresh_at = 0.0
            return {"ok": True, "ran": True, "state": completed_state}

        attempt_count = _safe_int(state.get("attempt_count"), 0) + 1
        run_id = f"daily-scan-{service_mod.ENVIRONMENT}-{market_date}-{uuid.uuid4().hex[:10]}"
        try:
            from ibkr_compute.api.service_topology import uses_remote_compute_service

            if uses_remote_compute_service():
                from ibkr_compute.api.compute_status_client import get_remote_compute_status, get_remote_scan_status, trigger_remote_scan

                diagnostics = {}
                compute_status = get_remote_compute_status(force_refresh=True)
                diagnostics = _compact_daily_scan_diagnostics(compute_status)
                preload = diagnostics.get("compute_startup_preload") if isinstance(diagnostics.get("compute_startup_preload"), dict) else {}
                if preload and bool(preload.get("running")):
                    failure = _classify_daily_scan_failure(
                        "compute_startup_preload_running",
                        result={"ok": False, "error": "compute_startup_preload_running"},
                        diagnostics=diagnostics,
                        retryable_default=True,
                    )
                    return self._schedule_or_fail_daily_scan_retry(
                        market_date=market_date,
                        reason=reason,
                        failure=failure,
                        result={"ok": False, "error": "compute_startup_preload_running"},
                        diagnostics=diagnostics,
                        run_id=run_id,
                    )

                pending_state = self._set_daily_scan_state(
                    market_date=market_date,
                    status="pending",
                    reason=reason,
                    run_id=run_id,
                    attempt_count=attempt_count,
                    started_at=self._now_iso(),
                    finished_at="",
                    last_error="",
                    failure={},
                    diagnostics=diagnostics,
                    result={},
                )
                scan_payload = {
                    "environment": service_mod.ENVIRONMENT,
                    "async": True,
                    "run_id": run_id,
                    "trigger_source": reason,
                }
                result = trigger_remote_scan(scan_payload) or {}
                if result.get("ok") and (result.get("accepted") or result.get("async")):
                    accepted_state = self._set_daily_scan_state(
                        market_date=market_date,
                        status="pending",
                        reason=reason,
                        run_id=str(result.get("run_id") or run_id),
                        attempt_count=attempt_count,
                        started_at=str(pending_state.get("started_at") or self._now_iso()),
                        finished_at="",
                        last_error="",
                        failure={},
                        diagnostics=diagnostics,
                        result=_compact_daily_scan_result_for_state(result),
                    )
                    return {
                        "ok": True,
                        "ran": True,
                        "pending": True,
                        "run_id": accepted_state.get("run_id"),
                        "state": accepted_state,
                        "result": result,
                    }

                if not result.get("ok"):
                    status_payload = get_remote_scan_status(
                        {
                            "environment": service_mod.ENVIRONMENT,
                            "date": market_date,
                            "run_id": run_id,
                        }
                    ) or {}
                    if status_payload.get("ok") and str(status_payload.get("status") or "").lower() not in {"not_found", ""}:
                        poll_state = self._set_daily_scan_state(
                            market_date=market_date,
                            status="pending",
                            reason=reason,
                            run_id=run_id,
                            attempt_count=attempt_count,
                            started_at=str(pending_state.get("started_at") or self._now_iso()),
                            finished_at="",
                            last_error="",
                            diagnostics={**diagnostics, **_compact_daily_scan_diagnostics(status_payload)},
                            result=_compact_daily_scan_result_for_state(status_payload),
                        )
                        return self._poll_daily_scan_attempt(market_date=market_date, reason=reason, state=poll_state)

                    diagnostics = {
                        **diagnostics,
                        **_compact_daily_scan_diagnostics(result),
                    }
                    failure = _classify_daily_scan_failure(
                        str(result.get("error") or result.get("error_code") or "daily_scan_submit_failed"),
                        result=result,
                        diagnostics=diagnostics,
                        retryable_default=bool(result.get("retryable", True)),
                    )
                    return self._schedule_or_fail_daily_scan_retry(
                        market_date=market_date,
                        reason=reason,
                        failure=failure,
                        result=result,
                        diagnostics=diagnostics,
                        run_id=run_id,
                    )

                return self._finalize_daily_scan_result(market_date=market_date, reason=reason, result=result, run_id=run_id)
            else:
                self._set_daily_scan_state(
                    market_date=market_date,
                    status="running",
                    reason=reason,
                    run_id=run_id,
                    attempt_count=attempt_count,
                    started_at=self._now_iso(),
                    finished_at="",
                    last_error="",
                    result={},
                )
                scan_payload = {"environment": service_mod.ENVIRONMENT}
                from ibkr_compute.api import server as compute_server

                result = compute_server._run_internal_scan(scan_payload) or {}
            return self._finalize_daily_scan_result(market_date=market_date, reason=reason, result=result, run_id=run_id)
        except Exception as exc:
            failure = _classify_daily_scan_failure(
                str(exc),
                result={"ok": False, "error": str(exc)},
                retryable_default=True,
            )
            service_mod.logger.error("Daily scan execution failed: %s", exc)
            return self._schedule_or_fail_daily_scan_retry(
                market_date=market_date,
                reason=reason,
                failure=failure,
                result={"ok": False, "error": str(exc)},
                diagnostics={},
                run_id=run_id,
            )

    def _quote_stale_resubscribe_threshold_sec(self) -> int:
        service_mod = _service_mod()
        return max(
            60,
            self.config.get_int_for_environment(
                "ibkr_realtime_quote_stale_resubscribe_sec",
                service_mod.ENVIRONMENT,
                600,
            ),
        )

    def _quote_resubscribe_cooldown_sec(self) -> int:
        service_mod = _service_mod()
        return max(
            30,
            self.config.get_int_for_environment(
                "ibkr_realtime_quote_resubscribe_cooldown_sec",
                service_mod.ENVIRONMENT,
                300,
            ),
        )

    def _resubscribe_realtime_conids(
        self,
        conid_map: dict,
        *,
        symbols=None,
        reason: str = "manual",
        force: bool = False,
    ) -> list[str]:
        service_mod = _service_mod()
        normalized_symbols = self._normalize_symbol_list(symbols or list((conid_map or {}).keys()))
        if not normalized_symbols:
            return []

        now = time.time()
        cooldown_sec = 0 if force else self._quote_resubscribe_cooldown_sec()
        resubscribe_at = getattr(self, "_quote_resubscribe_at", None)
        if not isinstance(resubscribe_at, dict):
            resubscribe_at = {}
            self._quote_resubscribe_at = resubscribe_at

        batch_size = max(
            1,
            self.config.get_int_for_environment("ibkr_ws_resubscribe_batch_size", service_mod.ENVIRONMENT, 8),
        )
        gap_ms = max(
            0,
            self.config.get_int_for_environment("ibkr_ws_resubscribe_gap_ms", service_mod.ENVIRONMENT, 150),
        )
        resubscribed = []
        skipped_cooldown = []
        for symbol in normalized_symbols:
            try:
                conid = int((conid_map or {}).get(symbol) or 0)
            except (TypeError, ValueError):
                conid = 0
            if conid <= 0:
                continue
            previous_at = float(resubscribe_at.get(symbol) or 0.0)
            if not force and previous_at > 0 and (now - previous_at) < cooldown_sec:
                skipped_cooldown.append(symbol)
                continue
            try:
                self.ws_client.resubscribe(conid)
            except Exception:
                service_mod.logger.warning(
                    "Realtime quote resubscribe failed: symbol=%s conid=%s reason=%s",
                    symbol,
                    conid,
                    reason,
                    exc_info=True,
                )
                continue
            resubscribe_at[symbol] = now
            resubscribed.append(symbol)
            if gap_ms > 0 and len(resubscribed) % batch_size == 0:
                time.sleep(gap_ms / 1000.0)

        if resubscribed:
            service_mod.logger.warning(
                "Realtime market data resubscribed: reason=%s force=%s symbols=%s skipped_cooldown=%s",
                reason or "manual",
                force,
                ",".join(resubscribed),
                ",".join(skipped_cooldown),
            )
        return resubscribed

    def _force_resubscribe_active_market_data(self, reason: str = "session_restored", symbols=None) -> list[str]:
        with self._subscription_lock:
            active_map = dict(self._active_subscription_map)
        normalized_symbols = self._normalize_symbol_list(symbols or list(active_map.keys()))
        return self._resubscribe_realtime_conids(
            active_map,
            symbols=normalized_symbols,
            reason=reason or "force",
            force=True,
        )

    def _repair_stale_realtime_quote_subscriptions(
        self,
        conid_map: dict,
        *,
        monitor_symbols=None,
        reason: str = "poll",
    ) -> list[str]:
        threshold_sec = self._quote_stale_resubscribe_threshold_sec()
        symbols = self._normalize_symbol_list(monitor_symbols or list((conid_map or {}).keys()))
        stale_quotes = self.realtime_quote_book.get_stale_quotes(symbols=symbols, max_age_s=threshold_sec)
        stale_symbols = self._normalize_symbol_list([item.get("symbol") for item in stale_quotes])
        if not stale_symbols:
            return []
        now = time.time()
        cooldown_sec = self._quote_resubscribe_cooldown_sec()
        resubscribe_at = getattr(self, "_quote_resubscribe_at", {})
        eligible_symbols = [
            symbol
            for symbol in stale_symbols
            if (now - float((resubscribe_at or {}).get(symbol) or 0.0)) >= cooldown_sec
        ]
        if not eligible_symbols:
            return []
        eligible_set = set(eligible_symbols)
        _service_mod().logger.warning(
            "Realtime quote stale; requesting resubscribe: threshold_s=%s reason=%s stale=%s",
            threshold_sec,
            reason or "poll",
            ",".join(
                f"{str(item.get('symbol') or '').upper()}:{item.get('quote_age_s')}s"
                for item in stale_quotes
                if str(item.get("symbol") or "").strip().upper() in eligible_set
            ),
        )
        return self._resubscribe_realtime_conids(
            conid_map,
            symbols=eligible_symbols,
            reason=f"stale_quote:{reason or 'poll'}",
            force=False,
        )

    def _apply_live_subscriptions(self, target_date: str, conid_map: dict, reason: str = "", trade_symbols: list[str] | None = None):
        service_mod = _service_mod()
        with self._subscription_lock:
            previous_map = dict(self._active_subscription_map)
            previous_target_date = self._active_target_date
            previous_trade_symbols = list(self._active_trade_symbols)
            previous_conids = set(previous_map.values())
            next_conids = set(conid_map.values())
            removed_conids = previous_conids - next_conids
            added_symbols = [
                symbol for symbol, conid in conid_map.items()
                if previous_map.get(symbol) != conid
            ]
            normalized_trade_symbols = sorted(
                symbol for symbol in (trade_symbols or [])
                if symbol in conid_map
            )

            if removed_conids:
                self.bar_aggregator.remove_conids(removed_conids)
                self.realtime_quote_book.remove_conids(removed_conids)
                for conid in sorted(removed_conids):
                    self.ws_client.unsubscribe(conid)

            reverse_map = {cid: sym for sym, cid in conid_map.items()}
            self.bar_aggregator.set_symbol_map(reverse_map)
            self.realtime_quote_book.set_symbol_map(reverse_map)

            for symbol in added_symbols:
                conid = conid_map.get(symbol)
                if conid:
                    self.ws_client.subscribe(conid)

            self._active_subscription_map = dict(conid_map)
            self._active_subscription_symbols = sorted(conid_map.keys())
            self._active_trade_symbols = normalized_trade_symbols
            self._active_target_date = target_date
            self._last_target_refresh_at = time.time()
            subscriptions_changed = (
                previous_target_date != target_date
                or sorted(previous_map.items()) != sorted(conid_map.items())
                or previous_trade_symbols != normalized_trade_symbols
            )

        if added_symbols and reason not in {"startup", "session_restored"}:
            added_map = {symbol: conid_map[symbol] for symbol in added_symbols if symbol in conid_map}
            service_mod.logger.info(
                "Backfilling newly subscribed target symbols: %s",
                ",".join(sorted(added_map.keys())),
            )
            self.data_backfill.backfill_all(added_map, symbol_meta=self._symbol_meta, intervals=["5m"])
            self.data_writer.flush()
        elif added_symbols:
            service_mod.logger.info(
                "Skipping inline backfill during %s; warmup will backfill %d symbols asynchronously",
                reason or "startup",
                len(added_symbols),
            )

        service_mod.logger.info(
            "Applied target subscriptions (%s): active=%d added=%d removed=%d",
            reason or "refresh",
            len(conid_map),
            len(added_symbols),
            len(removed_conids),
        )
        self._repair_stale_realtime_quote_subscriptions(
            conid_map,
            monitor_symbols=self._market_ws_symbols(),
            reason=reason or "refresh",
        )
        if subscriptions_changed or reason in {"startup", "session_restored"}:
            self._schedule_warmup(reason=reason or "subscriptions_changed", force=reason in {"startup", "session_restored"})

    def _refresh_target_subscriptions(self, force: bool = False, reason: str = "loop"):
        service_mod = _service_mod()
        self._reset_for_new_market_day(force=False)
        refresh_seconds = max(15, self.config.get_int_for_environment("ibkr_target_refresh_sec", service_mod.ENVIRONMENT, 60))
        now = time.time()
        if not force and (now - self._last_target_refresh_at) < refresh_seconds:
            return

        self._refresh_watchlist_pool(force=force)
        try:
            target_date, symbols, target_meta, selected_rows = self._build_target_subscription_plan()
        except Exception as exc:
            service_mod.logger.error("Failed to build target subscription plan: %s", exc)
            return

        if not symbols:
            service_mod.logger.info("No target symbols selected for %s (%s)", target_date, reason)
            self._mark_target_statuses(target_date, [])
            self._apply_live_subscriptions(target_date, {}, reason=reason, trade_symbols=[])
            return

        for symbol, meta in target_meta.items():
            base_meta = self._symbol_meta.get(symbol, {})
            self._symbol_meta[symbol] = {
                "exchange": str(meta.get("exchange") or base_meta.get("exchange") or "").upper(),
                "industry": str(meta.get("industry") or base_meta.get("industry") or ""),
            }

        conid_map = self.conid_resolver.resolve_bulk(symbols)
        if not conid_map:
            service_mod.logger.warning("No conids resolved for target plan (%s)", reason)
            return

        trade_symbols = sorted(
            {
                str(row.get("symbol", "")).upper()
                for row in selected_rows
                if str(row.get("symbol", "")).upper() in conid_map
            }
        )
        self._mark_target_statuses(target_date, selected_rows)
        self._apply_live_subscriptions(target_date, conid_map, reason=reason, trade_symbols=trade_symbols)

    def _subscription_refresh_loop(self):
        service_mod = _service_mod()
        service_mod.logger.info("Target subscription loop started")
        while self._running:
            try:
                self.config.refresh()
                self._refresh_runtime_settings()
                self._sync_session_transition()
                self._reset_for_new_market_day(force=False)
                if self.session_keeper.is_authenticated:
                    self._run_daily_scan_if_due(reason="poll")
                    self._refresh_target_subscriptions(reason="poll")
                else:
                    service_mod.logger.info("Skip target refresh while session is unauthenticated")
            except Exception as exc:
                service_mod.logger.error("Target subscription loop error: %s", exc)

            sleep_seconds = max(15, self.config.get_int_for_environment("ibkr_target_refresh_sec", service_mod.ENVIRONMENT, 60))
            for _ in range(sleep_seconds):
                if not self._running:
                    break
                time.sleep(1)

    def _remove_stale_target_rows(self, active_date: str) -> int:
        service_mod = _service_mod()
        safe_env = str(service_mod.ENVIRONMENT or "live").strip().lower().replace('"', '\\"')
        try:
            rows = self.pb.get_all_records(
                "ibkr_targets",
                filter=(
                    f'environment = "{safe_env}" && '
                    '(status = "candidate" || status = "active")'
                ),
                max_pages=20,
            )
        except Exception as exc:
            service_mod.logger.warning("Failed to load stale target rows: %s", exc)
            return 0

        removed = 0
        removed_at = datetime.now(service_mod.ET).isoformat()
        for row in rows:
            row_date = str(row.get("date", "") or "").strip()
            if not row_date or row_date == active_date:
                continue

            record_id = str(row.get("id") or "")
            if not record_id:
                continue

            payload = {"status": "removed"}
            extra = row.get("extra")
            if isinstance(extra, dict):
                next_extra = dict(extra)
                next_extra["removed_reason"] = "market_day_reset"
                next_extra["removed_at"] = removed_at
                next_extra["removed_market_date"] = active_date
                payload["extra"] = next_extra

            try:
                self.pb.update_record("ibkr_targets", record_id, payload)
                removed += 1
            except Exception as exc:
                service_mod.logger.warning(
                    "Failed to remove stale target row %s (%s %s): %s",
                    record_id,
                    row_date,
                    str(row.get("symbol", "")).upper(),
                    exc,
                )

        if removed > 0:
            service_mod.logger.info("Removed %d stale target rows before activating %s", removed, active_date)
        return removed

    def _reset_for_new_market_day(self, force: bool = False):
        service_mod = _service_mod()
        current_date = self._market_date()
        previous_date = self._current_market_date
        if not force and previous_date == current_date:
            return False

        service_mod.logger.info(
            "Market day reset: previous=%s current=%s force=%s",
            previous_date or "n/a",
            current_date,
            force,
        )
        self._current_market_date = current_date
        self._last_daily_reset_at = time.time()

        self.signal_router.daily_reset()
        self.signal_processor.daily_reset()
        self.reverse_handler.daily_reset()
        self.order_lifecycle.daily_reset()
        self.timeframe_builder.reset()
        self.bar_aggregator.reset()
        self.realtime_quote_book.reset()
        self._quote_prev_close_cache = {}
        self._quote_prev_close_cache_date = current_date
        self._signal_wakeup.clear()
        drained = self._drain_compute_queue()
        if drained > 0:
            service_mod.logger.info(
                "Cleared %d queued realtime compute tasks during market day reset",
                drained,
            )

        try:
            from ibkr_compute.api import server as compute_server

            reset_result = compute_server.reset_daily_runtime_state(
                [service_mod.ENVIRONMENT],
                reason="market_day_reset",
            )
            service_mod.logger.info("Compute daily reset result: %s", reset_result)
        except Exception as exc:
            service_mod.logger.warning("Compute daily reset failed: %s", exc)

        self._reset_warmup_state(reason="market_day_reset")
        if previous_date and previous_date != current_date:
            self._set_daily_scan_state(**self._initial_daily_scan_state(current_date))
        else:
            with self._scan_state_lock:
                self._daily_scan_state = self._load_daily_scan_state(current_date)
        self._reset_daily_scan_alert_state(current_date)
        self._remove_stale_target_rows(current_date)
        self._apply_live_subscriptions(current_date, {}, reason="market_day_reset")
        self._active_target_date = ""
        self._last_target_refresh_at = 0.0
        self._last_backfill_at = 0.0
        self._last_backfill_symbols = []
        self._watchlist_idle_topup_cursor = 0
        self._last_watchlist_deep_maintenance_at = 0.0
        with self._watchlist_idle_topup_lock:
            self._watchlist_idle_observations = {}
            self._watchlist_idle_topup_state = self._initial_watchlist_idle_topup_state()
        self._watchlist_topup_force_until = 0.0
        self._watchlist_topup_requested_at = 0.0
        self._watchlist_topup_request_count = 0
        self._watchlist_topup_last_consumed_request_count = 0
        self._last_active_repair_at = 0.0
        self._last_active_repair_symbols = []
        self._last_active_repair_reasons = {}
        self._last_history_repair_at = 0.0
        self._last_history_repair_symbols = []
        self._last_pipeline_repair_at = 0.0
        self._last_pipeline_repair_symbols = []
        self._watchlist_integrity_cursor = 0
        self._last_watchlist_integrity_at = 0.0
        self._last_watchlist_integrity_symbols = []
        self._last_watchlist_integrity_repair_symbols = []
        self._official_5m_state = self._initial_official_5m_state()
        if previous_date and previous_date != current_date:
            self._persist_watchlist_integrity_cursor()
        return True

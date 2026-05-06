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

        symbol_meta = {}
        trade_symbols = []
        monitor_symbols = []
        for symbol, row in merged.items():
            symbol_role = self._watchlist_record_role(row)
            symbol_meta[symbol] = {
                "exchange": str(row.get("exchange", "") or "").upper(),
                "industry": str(row.get("industry", "") or ""),
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
        target_date, rows = self._today_target_rows()
        trade_budget = self._get_trade_subscription_budget()
        selected_symbols = []
        selected_meta = {}
        selected_rows = []
        seen = set()

        active_rows = [
            row
            for row in rows
            if str(row.get("status", "") or "").strip().lower() == "active"
        ]
        prioritized_rows = [
            row for row in active_rows if _target_row_is_manual(row)
        ] + [
            row for row in active_rows if not _target_row_is_manual(row)
        ]

        for row in prioritized_rows:
            symbol = str(row.get("symbol", "")).upper()
            if not symbol or symbol in seen:
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

        selected_ids = {str(row.get("id") or "") for row in selected_rows}
        for row in existing:
            record_id = str(row.get("id") or "")
            if not record_id:
                continue
            if _target_row_is_manual(row):
                continue
            desired = "active" if record_id in selected_ids else "candidate"
            current = str(row.get("status", "") or "").strip().lower()
            if current == desired:
                continue
            try:
                self.pb.update_record("ibkr_targets", record_id, {"status": desired})
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

from __future__ import annotations

import time
from datetime import datetime


def _service_mod():
    from . import trading_service as service_mod

    return service_mod


class TradingServiceMarketUniverseMixin:
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
        return max(0, self.config.get_int_for_environment("ibkr_target_subscription_limit", service_mod.ENVIRONMENT, 60))

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
        limit = self._get_target_subscription_limit()
        selected_symbols = []
        selected_meta = {}
        selected_rows = []
        seen = set()

        for row in rows:
            symbol = str(row.get("symbol", "")).upper()
            if not symbol or symbol in seen:
                continue
            if limit and len(selected_symbols) >= limit:
                break
            score = float(row.get("score", 0) or 0)
            if score <= 0:
                continue

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
        raw_schedule = str(
            self.config.get_for_environment("ibkr_scan_schedule", service_mod.ENVIRONMENT, "7:00-10:00") or ""
        ).strip()
        start_text = raw_schedule.split("-", 1)[0].strip() or "07:00"
        try:
            hour_text, minute_text = start_text.split(":", 1)
            hour = int(hour_text)
            minute = int(minute_text)
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return hour, minute
        except Exception:
            pass
        return 7, 0

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
        if str(state.get("status") or "").strip().lower() == "running":
            return {"ok": True, "skipped": True, "reason": "scan_running", "state": state}
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
                    "candidates": 0,
                    "errors": 0,
                    "environments": [service_mod.ENVIRONMENT],
                },
            )
            self._last_target_refresh_at = 0.0
            return {"ok": True, "ran": True, "state": completed_state}

        self._set_daily_scan_state(
            market_date=market_date,
            status="running",
            reason=reason,
            started_at=self._now_iso(),
            finished_at="",
            last_error="",
            result={},
        )
        try:
            from ibkr_compute.api import server as compute_server

            result = compute_server._run_internal_scan({"environment": service_mod.ENVIRONMENT}) or {}
            completed_state = self._set_daily_scan_state(
                market_date=market_date,
                status="completed" if bool(result.get("ok", True)) else "failed",
                reason=reason,
                finished_at=self._now_iso(),
                last_error="" if bool(result.get("ok", True)) else str(result.get("error") or "daily_scan_failed"),
                result=result,
            )
            if bool(result.get("ok", True)):
                self._last_target_refresh_at = 0.0
            return {"ok": bool(result.get("ok", True)), "ran": True, "state": completed_state, "result": result}
        except Exception as exc:
            failed_state = self._set_daily_scan_state(
                market_date=market_date,
                status="failed",
                reason=reason,
                finished_at=self._now_iso(),
                last_error=str(exc),
                result={},
            )
            service_mod.logger.error("Daily scan execution failed: %s", exc)
            return {"ok": False, "ran": True, "state": failed_state, "error": str(exc)}

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
                service_mod.ENVIRONMENT,
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
                service_mod.ENVIRONMENT,
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
                service_mod.ENVIRONMENT,
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

        batch_size = max(1, self.config.get_int_for_environment("ibkr_watchlist_backfill_batch_size", service_mod.ENVIRONMENT, 12))
        start = self._watchlist_backfill_cursor % len(pool)
        ordered = pool[start:] + pool[:start]
        self._watchlist_backfill_cursor = (start + batch_size) % max(len(pool), 1)
        return ordered[:batch_size]

    def _active_repair_loop(self):
        service_mod = _service_mod()
        service_mod.logger.info("Active target repair loop started")
        while self._running:
            try:
                self.config.refresh()
                self._refresh_runtime_settings()
                self._run_active_repair_cycle()
            except Exception as exc:
                service_mod.logger.error("Active target repair loop error: %s", exc)

            sleep_seconds = max(300, self.config.get_int_for_environment("ibkr_active_repair_interval_min", service_mod.ENVIRONMENT, 5) * 60)
            for _ in range(sleep_seconds):
                if not self._running:
                    break
                time.sleep(1)

    def _run_active_repair_cycle(self):
        service_mod = _service_mod()
        if self._is_warmup_active():
            service_mod.logger.info("Active target repair skipped while startup warmup is active")
            return
        if not self.session_keeper.is_authenticated:
            service_mod.logger.info("Active target repair skipped while session is unauthenticated")
            return

        defer_repairs, defer_snapshot = self._should_defer_background_repairs()
        if defer_repairs:
            service_mod.logger.info(
                "Active target repair downgraded to scan-only: reason=%s queue=%s trade_targets=%s subscriptions=%s websocket=%s authenticated=%s",
                defer_snapshot.get("reason"),
                defer_snapshot.get("queue_size"),
                defer_snapshot.get("active_target_count"),
                defer_snapshot.get("active_subscription_count"),
                defer_snapshot.get("websocket_connected"),
                defer_snapshot.get("authenticated"),
            )

        result = self.scan_bar_integrity(
            list(self._active_subscription_symbols),
            scan_scope="active_target",
            persist=True,
            repair=not defer_repairs,
        )
        summary = result.get("summary") or {}
        attempted_repair_symbols = list(summary.get("attempted_repair_symbols") or [])
        repair_symbols = list(attempted_repair_symbols or summary.get("repair_candidate_symbols") or [])
        if not repair_symbols:
            service_mod.logger.info("Active target repair skipped: no repair needed")
            return
        if defer_repairs and not attempted_repair_symbols:
            service_mod.logger.info("Active target repair deferred: pending=%s", ",".join(repair_symbols))
            return

        self._last_active_repair_at = time.time()
        self._last_active_repair_symbols = repair_symbols
        self._last_active_repair_reasons = {
            symbol: str((summary.get("initial_repair_reasons") or summary.get("repair_reasons") or {}).get(symbol) or "")
            for symbol in repair_symbols
        }
        history_symbols = list(summary.get("history_fetch_symbols") or [])
        if history_symbols:
            self._last_history_repair_at = self._last_active_repair_at
            self._last_history_repair_symbols = history_symbols

    def _watchlist_backfill_loop(self):
        service_mod = _service_mod()
        service_mod.logger.info("Watchlist backfill loop started")
        while self._running:
            try:
                self.config.refresh()
                self._refresh_runtime_settings()
                self._run_watchlist_backfill_cycle()
            except Exception as exc:
                service_mod.logger.error("Watchlist backfill loop error: %s", exc)

            sleep_seconds = max(300, self.config.get_int_for_environment("ibkr_watchlist_backfill_interval_min", service_mod.ENVIRONMENT, 30) * 60)
            for _ in range(sleep_seconds):
                if not self._running:
                    break
                time.sleep(1)

    def _run_watchlist_backfill_cycle(self):
        service_mod = _service_mod()
        if self._is_warmup_active():
            service_mod.logger.info("Watchlist backfill skipped while startup warmup is active")
            return
        defer_repairs, defer_snapshot = self._should_defer_background_repairs()
        if defer_repairs:
            service_mod.logger.info(
                "Watchlist maintenance deferred: reason=%s queue=%s trade_targets=%s subscriptions=%s websocket=%s authenticated=%s",
                defer_snapshot.get("reason"),
                defer_snapshot.get("queue_size"),
                defer_snapshot.get("active_target_count"),
                defer_snapshot.get("active_subscription_count"),
                defer_snapshot.get("websocket_connected"),
                defer_snapshot.get("authenticated"),
            )
            return
        self._refresh_watchlist_pool()

        candidates = self._watchlist_backfill_candidates()
        if not candidates:
            service_mod.logger.info("Watchlist backfill skipped: no non-target symbols in pool")
            return

        stale_minutes = max(5, self.config.get_int_for_environment("ibkr_watchlist_backfill_stale_min", service_mod.ENVIRONMENT, 20))
        now_ms = int(time.time() * 1000)
        stale_ms = stale_minutes * 60 * 1000
        eligible = []
        for symbol in candidates:
            latest_ms = self.data_backfill.get_latest_stored_bar_ms(symbol, "5m")
            if latest_ms <= 0 or (now_ms - latest_ms) >= stale_ms:
                eligible.append(symbol)

        if not eligible:
            service_mod.logger.info("Watchlist backfill skipped: batch is fresh enough")
        else:
            conid_map = self.conid_resolver.resolve_bulk(eligible)
            if not conid_map:
                service_mod.logger.warning("Watchlist backfill skipped: no conids resolved")
            else:
                symbol_meta = {symbol: self._symbol_meta.get(symbol, {}) for symbol in conid_map.keys()}
                service_mod.logger.info("Running incremental watchlist backfill for %d symbols", len(conid_map))
                self.data_backfill.backfill_all(conid_map, symbol_meta=symbol_meta, intervals=["5m"])
                self.data_writer.flush()
                self._last_backfill_at = time.time()
                self._last_backfill_symbols = sorted(conid_map.keys())

        if not self.config.get_bool_for_environment("ibkr_watchlist_integrity_enabled", service_mod.ENVIRONMENT, True):
            return

        integrity_candidates = self._watchlist_integrity_candidates()
        if not integrity_candidates:
            service_mod.logger.info("Watchlist integrity scan skipped: empty candidate batch")
            return

        result = self.scan_bar_integrity(
            integrity_candidates,
            scan_scope="watchlist",
            persist=True,
            repair=True,
        )
        summary = result.get("summary") or {}
        self._last_watchlist_integrity_at = time.time()
        self._last_watchlist_integrity_symbols = list(summary.get("symbols") or integrity_candidates)
        self._last_watchlist_integrity_repair_symbols = list(
            summary.get("attempted_repair_symbols")
            or summary.get("initial_repair_symbols")
            or summary.get("repair_candidate_symbols")
            or []
        )
        self._persist_watchlist_integrity_cursor()

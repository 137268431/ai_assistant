from __future__ import annotations

import json
import time
from datetime import datetime

from ibkr_compute.core.payload_compact import compact_json_payload


def _service_mod():
    from . import trading_service as service_mod

    return service_mod


MANUAL_TARGET_SOURCES = {
    "ibkr_screener",
    "manual_page",
    "manual_page_add",
    "manual_page_edit",
    "manual_page_remove",
    "screener_targets_tab",
}
DAILY_SCAN_RUNNING_STALE_SECONDS = 10 * 60


def _safe_extra(row: dict | None) -> dict:
    payload = (row or {}).get("extra")
    if isinstance(payload, dict):
        return dict(payload)
    if isinstance(payload, str):
        try:
            parsed = json.loads(payload)
        except Exception:
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def _target_row_is_manual(row: dict | None) -> bool:
    extra = _safe_extra(row)
    source = str(extra.get("source") or "").strip().lower()
    if source.startswith("manual_"):
        return True
    return source in MANUAL_TARGET_SOURCES


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _daily_scan_all_snapshotless(result: dict | None) -> bool:
    if not isinstance(result, dict):
        return False
    scanned = _safe_int(result.get("scanned"), 0)
    rejection_summary = result.get("rejection_summary")
    if not isinstance(rejection_summary, dict):
        rejection_summary = {}
    no_snapshot = _safe_int(rejection_summary.get("no_snapshot"), 0)
    if scanned <= 0 and isinstance(result.get("environment_results"), list):
        env_results = [row for row in result.get("environment_results") if isinstance(row, dict)]
        scanned = sum(_safe_int(row.get("scanned"), 0) for row in env_results)
        no_snapshot = 0
        for row in env_results:
            summary = row.get("rejection_summary")
            if isinstance(summary, dict):
                no_snapshot += _safe_int(summary.get("no_snapshot"), 0)
    return scanned > 0 and no_snapshot >= scanned


def _parse_iso_datetime(value) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except Exception:
        return None


def _daily_scan_running_age_seconds(state: dict | None, now_dt: datetime) -> float | None:
    if not isinstance(state, dict):
        return None
    started_at = _parse_iso_datetime(state.get("started_at"))
    if started_at is None:
        return None
    if started_at.tzinfo is None and now_dt.tzinfo is not None:
        started_at = started_at.replace(tzinfo=now_dt.tzinfo)
    elif started_at.tzinfo is not None and now_dt.tzinfo is None:
        started_at = started_at.replace(tzinfo=None)
    if now_dt.tzinfo is not None and started_at.tzinfo is not None:
        started_at = started_at.astimezone(now_dt.tzinfo)
    return max(0.0, (now_dt - started_at).total_seconds())


def _compact_daily_scan_result_for_state(result: dict | None) -> dict:
    if not isinstance(result, dict):
        return {}
    return compact_json_payload(
        result,
        max_list_items=60,
        max_dict_items=160,
        max_string_length=1200,
        max_depth=8,
    )


class TradingServiceMarketUniverseMixin:
    def _initial_daily_scan_state(self, market_date: str = "") -> dict:
        return {
            "market_date": str(market_date or self._market_date()),
            "status": "idle",
            "reason": "",
            "started_at": "",
            "finished_at": "",
            "last_error": "",
            "result": {},
        }

    def _load_daily_scan_state(self, market_date: str) -> dict:
        service_mod = _service_mod()
        target_date = str(market_date or self._market_date())
        try:
            state = self.pb.get_state(
                service_mod.DAILY_SCAN_STATE_KEY,
                service_mod.ENVIRONMENT,
                date=service_mod.DAILY_SCAN_STATE_DATE,
            )
        except Exception:
            state = None
        payload = state.get("data") if isinstance(state, dict) else {}
        if not isinstance(payload, dict):
            return self._initial_daily_scan_state(target_date)
        loaded = {
            **self._initial_daily_scan_state(target_date),
            **payload,
        }
        if str(loaded.get("market_date") or "") != target_date:
            return self._initial_daily_scan_state(target_date)
        return loaded

    def _copy_daily_scan_state(self) -> dict:
        with self._scan_state_lock:
            return dict(self._daily_scan_state or {})

    def _set_daily_scan_state(self, **updates) -> dict:
        service_mod = _service_mod()
        with self._scan_state_lock:
            next_state = dict(self._daily_scan_state or self._initial_daily_scan_state())
            next_state.update(updates)
            next_state["market_date"] = str(next_state.get("market_date") or self._market_date())
            self._daily_scan_state = next_state
            try:
                self.pb.upsert_state(
                    service_mod.DAILY_SCAN_STATE_KEY,
                    service_mod.ENVIRONMENT,
                    next_state,
                    date=service_mod.DAILY_SCAN_STATE_DATE,
                )
            except Exception:
                service_mod.logger.warning("Persist daily scan state failed", exc_info=True)
            return dict(next_state)

    def _reset_daily_scan_alert_state(self, market_date: str = ""):
        self._daily_scan_alert_market_date = str(market_date or "")
        self._daily_scan_alert_error = ""
        self._daily_scan_alert_title = ""
        self._daily_scan_alert_at = 0.0
        self._daily_scan_alert_active = False
        self._daily_scan_failure_count = 0

    def _notify_daily_scan_failed(self, state: dict | None = None):
        service_mod = _service_mod()
        market_date = str((state or {}).get("market_date") or self._current_market_date or self._market_date())
        error_text = str((state or {}).get("last_error") or "daily_scan_failed").strip() or "daily_scan_failed"
        reason = str((state or {}).get("reason") or "").strip() or "poll"
        result = (state or {}).get("result")
        if not isinstance(result, dict):
            result = {}
        if self._daily_scan_alert_market_date != market_date:
            self._reset_daily_scan_alert_state(market_date)
        self._daily_scan_failure_count += 1

        now = time.time()
        should_send = (
            not self._daily_scan_alert_active
            or error_text != self._daily_scan_alert_error
            or self._daily_scan_alert_at <= 0
            or (now - self._daily_scan_alert_at) >= service_mod.DAILY_SCAN_EVENT_ALERT_COOLDOWN_SECONDS
        )
        self._daily_scan_alert_market_date = market_date
        self._daily_scan_alert_error = error_text
        self._daily_scan_alert_title = "IBKR 盘前日筛失败"
        if not should_send:
            return

        detail = {
            "状态结论": "今日目标池自动筛选失败，盘中 active / candidate targets 不会按预期刷新。",
            "检查时间": self._now_et(),
            "交易日": market_date,
            "Runtime阶段": self._runtime_phase_label(),
            "触发原因": reason,
            "错误信息": error_text,
            "失败次数": str(self._daily_scan_failure_count),
            "处理建议": "检查 compute / screener / targets 写入链路，并在修复后手动重跑 /scan。",
        }
        scanned = int(result.get("scanned", 0) or 0)
        active = int(result.get("active", 0) or 0)
        candidates = int(result.get("candidates", 0) or 0)
        errors = int(result.get("errors", 0) or 0)
        if scanned > 0 or active > 0 or candidates > 0 or errors > 0:
            detail["扫描结果"] = (
                f"scanned={scanned} active={active} "
                f"candidate={candidates} errors={errors}"
            )
        runtime_url = self._runtime_page_url()
        if runtime_url:
            detail["运行页"] = runtime_url

        self._emit_system_event("alert", "error", self._daily_scan_alert_title, detail)
        self._daily_scan_alert_active = True
        self._daily_scan_alert_at = now

    def _notify_daily_scan_recovered(self, state: dict | None = None):
        if not self._daily_scan_alert_active:
            return

        market_date = str((state or {}).get("market_date") or self._daily_scan_alert_market_date or self._market_date())
        reason = str((state or {}).get("reason") or "").strip() or "poll"
        result = (state or {}).get("result")
        if not isinstance(result, dict):
            result = {}

        detail = {
            "状态结论": "今日目标池自动筛选已恢复成功，盘中 trade targets 已重新生成。",
            "检查时间": self._now_et(),
            "交易日": market_date,
            "Runtime阶段": self._runtime_phase_label(),
            "恢复来源": reason,
            "上一条错误": self._daily_scan_alert_error or "-",
        }
        scanned = int(result.get("scanned", 0) or 0)
        active = int(result.get("active", 0) or 0)
        candidates = int(result.get("candidates", 0) or 0)
        errors = int(result.get("errors", 0) or 0)
        detail["恢复结果"] = (
            f"scanned={scanned} active={active} "
            f"candidate={candidates} errors={errors}"
        )
        runtime_url = self._runtime_page_url()
        if runtime_url:
            detail["运行页"] = runtime_url

        self._emit_system_event("alert", "info", "IBKR 盘前日筛已恢复", detail)
        self._reset_daily_scan_alert_state(market_date)

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
        if str(state.get("status") or "").strip().lower() == "running":
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
            from ibkr_compute.api.service_topology import uses_remote_compute_service

            scan_payload = {"environment": service_mod.ENVIRONMENT}
            if uses_remote_compute_service():
                from ibkr_compute.api.compute_status_client import trigger_remote_scan

                result = trigger_remote_scan(scan_payload) or {}
            else:
                from ibkr_compute.api import server as compute_server

                result = compute_server._run_internal_scan(scan_payload) or {}
            scan_ok = bool(result.get("ok", True))
            last_error = "" if scan_ok else str(result.get("error") or "daily_scan_failed")
            if scan_ok and _daily_scan_all_snapshotless(result):
                last_error = "all_scanned_symbols_missing_technical_snapshots"
                result = {
                    **result,
                    "ok": False,
                    "error": last_error,
                }
                scan_ok = False
            completed_state = self._set_daily_scan_state(
                market_date=market_date,
                status="completed" if scan_ok else "failed",
                reason=reason,
                finished_at=self._now_iso(),
                last_error=last_error,
                result=_compact_daily_scan_result_for_state(result),
            )
            if scan_ok:
                self._notify_daily_scan_recovered(completed_state)
                self._last_target_refresh_at = 0.0
            else:
                self._notify_daily_scan_failed(completed_state)
            return {"ok": scan_ok, "ran": True, "state": completed_state, "result": result}
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
            self._notify_daily_scan_failed(failed_state)
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

    def _normalize_runtime_symbols(self, symbols) -> list[str]:
        normalized = []
        seen = set()
        for item in symbols or []:
            symbol = str(item or "").strip().upper()
            if symbol and symbol not in seen:
                seen.add(symbol)
                normalized.append(symbol)
        return normalized

    def _select_latest_valid_prime_signals(self, captured_signals, allowed_symbols=None) -> dict:
        allowed = set(self._normalize_runtime_symbols(allowed_symbols))
        grouped: dict[str, list[dict]] = {}
        evaluated: list[dict] = []
        selected: list[dict] = []

        for item in captured_signals or []:
            symbol = str((item or {}).get("symbol") or "").strip().upper()
            if not symbol:
                continue
            if allowed and symbol not in allowed:
                continue
            grouped.setdefault(symbol, []).append(dict(item or {}))

        for symbol, rows in grouped.items():
            rows.sort(key=lambda row: int(row.get("bar_time_ms", 0) or 0), reverse=True)
            for row in rows:
                signal_payload = {
                    "signal_id": str(row.get("signal_id") or ""),
                    "symbol": symbol,
                    "direction": str(row.get("direction") or "").strip(),
                    "entry": float(row.get("entry", 0) or 0),
                    "stop_loss": float(row.get("stop_loss", 0) or 0),
                    "take_profit": float(row.get("take_profit", 0) or 0),
                    "shares": int(row.get("shares", 0) or 0),
                    "signal_time": row.get("us_time") or "",
                }
                valid, reason = self.signal_processor.validate_signal(signal_payload)
                evaluated.append(
                    {
                        "symbol": symbol,
                        "signal_id": str(row.get("signal_id") or ""),
                        "bar_time_ms": int(row.get("bar_time_ms", 0) or 0),
                        "valid": bool(valid),
                        "reason": str(reason or ""),
                    }
                )
                if not valid:
                    continue

                extra = dict(row.get("extra") or {})
                extra.update(
                    {
                        "universe_prime": True,
                        "universe_prime_source": "manual_pool_reconcile",
                        "universe_prime_triggered_at": self._now_iso(),
                    }
                )
                row["extra"] = extra
                selected.append(row)
                break

        return {
            "symbols": sorted(grouped.keys()),
            "selected_signals": selected,
            "evaluated": evaluated,
        }

    def _persist_prime_signals(self, selected_signals) -> dict:
        service_mod = _service_mod()
        persisted = []
        errors = []
        signal_ids = []

        for item in selected_signals or []:
            payload = dict(item or {})
            signal_id = str(payload.get("signal_id") or "").strip()
            if not signal_id:
                continue
            payload["environment"] = service_mod.ENVIRONMENT
            try:
                result = self.pb.upsert_signal(payload)
                persisted.append(
                    {
                        "signal_id": signal_id,
                        "symbol": str(payload.get("symbol") or "").strip().upper(),
                        "action": str(result.get("action") or ""),
                        "status": str(result.get("status") or ""),
                    }
                )
                signal_ids.append(signal_id)
            except Exception as exc:
                errors.append({"signal_id": signal_id, "error": str(exc)})

        if signal_ids:
            self.signal_router.forget_processed(signal_ids)
            self._signal_wakeup.set()

        return {
            "persisted": persisted,
            "errors": errors,
            "signal_ids": signal_ids,
        }

    def _prime_universe_symbols(self, symbols, *, emit_signals: bool = False, source: str = "universe_prime") -> dict:
        service_mod = _service_mod()
        normalized_symbols = self._normalize_runtime_symbols(symbols)
        if not normalized_symbols:
            return {"ok": True, "symbols": [], "skipped": True, "reason": "empty_symbols"}

        backfill_result: dict = {"ok": True, "symbols": [], "resolved": []}
        conid_map = {}
        try:
            conid_map = self.conid_resolver.resolve_bulk(normalized_symbols)
        except Exception as exc:
            backfill_result = {"ok": False, "symbols": normalized_symbols, "error": str(exc)}

        if conid_map:
            try:
                symbol_meta = {symbol: self._symbol_meta.get(symbol, {}) for symbol in conid_map.keys()}
                self.data_backfill.backfill_all(conid_map, symbol_meta=symbol_meta, intervals=["5m"])
                self.data_writer.flush()
                self._last_backfill_at = time.time()
                self._last_backfill_symbols = sorted(conid_map.keys())
                backfill_result = {
                    "ok": True,
                    "symbols": normalized_symbols,
                    "resolved": sorted(conid_map.keys()),
                    "missing": sorted(set(normalized_symbols) - set(conid_map.keys())),
                }
            except Exception as exc:
                backfill_result = {
                    "ok": False,
                    "symbols": normalized_symbols,
                    "resolved": sorted(conid_map.keys()),
                    "error": str(exc),
                }

        compute_result = {}
        selected_signal_result = {"symbols": [], "selected_signals": [], "evaluated": []}
        persisted_signal_result = {"persisted": [], "errors": [], "signal_ids": []}
        interval_prime_started = False
        try:
            from ibkr_compute.api import server as compute_server

            with compute_server.compute_lock:
                compute_result = compute_server._run_internal_compute(
                    {
                        "source": "targeted_recompute",
                        "environments": [service_mod.ENVIRONMENT],
                        "symbols": normalized_symbols,
                        "force_rollup": True,
                        "persist_signals": False,
                        "capture_signals": bool(emit_signals),
                    }
                ) or {}
        except Exception as exc:
            compute_result = {"ok": False, "error": str(exc)}

        if emit_signals:
            selected_signal_result = self._select_latest_valid_prime_signals(
                (compute_result or {}).get("captured_signals") or [],
                allowed_symbols=normalized_symbols,
            )
            persisted_signal_result = self._persist_prime_signals(
                selected_signal_result.get("selected_signals") or []
            )

        try:
            interval_prime_started = self._schedule_interval_prime(
                normalized_symbols,
                source=str(source or "universe_prime"),
            )
        except Exception:
            interval_prime_started = False

        return {
            "ok": bool((compute_result or {}).get("ok", True)) and bool(backfill_result.get("ok", True)),
            "symbols": normalized_symbols,
            "emit_signals": bool(emit_signals),
            "backfill": backfill_result,
            "compute": compute_result,
            "signal_selection": selected_signal_result,
            "signal_persist": persisted_signal_result,
            "interval_prime_started": interval_prime_started,
        }

    def _cleanup_universe_symbols(self, symbols, *, source: str = "universe_cleanup") -> dict:
        service_mod = _service_mod()
        normalized_symbols = self._normalize_runtime_symbols(symbols)
        if not normalized_symbols:
            return {"ok": True, "symbols": [], "skipped": True, "reason": "empty_symbols"}

        removed_conids = []
        with self._subscription_lock:
            removed_conids = sorted(
                {
                    int(conid)
                    for symbol, conid in self._active_subscription_map.items()
                    if symbol in normalized_symbols and int(conid or 0) > 0
                }
            )

        if removed_conids:
            self.bar_aggregator.remove_conids(removed_conids)
            self.realtime_quote_book.remove_conids(removed_conids)
            for conid in removed_conids:
                try:
                    self.ws_client.unsubscribe(conid)
                except Exception:
                    service_mod.logger.warning("Universe cleanup unsubscribe failed: conid=%s", conid, exc_info=True)

        for symbol in normalized_symbols:
            self._quote_prev_close_cache.pop(symbol, None)
        self._last_backfill_symbols = [symbol for symbol in self._last_backfill_symbols if symbol not in normalized_symbols]
        self._last_active_repair_symbols = [symbol for symbol in self._last_active_repair_symbols if symbol not in normalized_symbols]
        self._last_history_repair_symbols = [symbol for symbol in self._last_history_repair_symbols if symbol not in normalized_symbols]
        self._last_pipeline_repair_symbols = [symbol for symbol in self._last_pipeline_repair_symbols if symbol not in normalized_symbols]
        self._last_watchlist_integrity_symbols = [symbol for symbol in self._last_watchlist_integrity_symbols if symbol not in normalized_symbols]
        self._last_watchlist_integrity_repair_symbols = [
            symbol for symbol in self._last_watchlist_integrity_repair_symbols if symbol not in normalized_symbols
        ]

        compute_reset = {}
        try:
            from ibkr_compute.api import server as compute_server

            with compute_server.compute_lock:
                compute_reset = compute_server.reset_compute_state_for_symbols(
                    service_mod.ENVIRONMENT,
                    normalized_symbols,
                )
                compute_server.persist_compute_cursors(service_mod.ENVIRONMENT)
        except Exception as exc:
            compute_reset = {"ok": False, "error": str(exc)}

        sqlite_result = {}
        try:
            from ibkr_compute.market.pocketbase_sqlite import delete_symbol_runtime_data, open_pb_sqlite

            with open_pb_sqlite(readonly=False) as conn:
                sqlite_result = delete_symbol_runtime_data(conn, service_mod.ENVIRONMENT, normalized_symbols)
                conn.commit()
        except Exception as exc:
            sqlite_result = {"ok": False, "error": str(exc), "symbols": normalized_symbols}

        deleted_signal_ids = list((sqlite_result or {}).get("deleted_signal_ids") or [])
        if deleted_signal_ids:
            self.signal_router.forget_processed(deleted_signal_ids)

        return {
            "ok": not bool((sqlite_result or {}).get("error")) and not bool((compute_reset or {}).get("error")),
            "symbols": normalized_symbols,
            "source": str(source or "universe_cleanup"),
            "removed_conids": removed_conids,
            "compute_reset": compute_reset,
            "sqlite": sqlite_result,
        }

    def reconcile_market_universe(
        self,
        *,
        prime_symbols=None,
        cleanup_symbols=None,
        emit_signals: bool = False,
        source: str = "runtime_api",
        reason: str = "manual_reconcile",
    ) -> dict:
        service_mod = _service_mod()
        prime_list = self._normalize_runtime_symbols(prime_symbols)
        cleanup_list = self._normalize_runtime_symbols(cleanup_symbols)

        self.config.refresh()
        self._refresh_runtime_settings()
        self._reset_for_new_market_day(force=False)
        self._refresh_watchlist_pool(force=True)

        target_refresh = {
            "ok": True,
            "skipped": not self.session_keeper.is_authenticated,
            "reason": "session_unauthenticated" if not self.session_keeper.is_authenticated else "",
        }
        if self.session_keeper.is_authenticated:
            try:
                self._refresh_target_subscriptions(force=True, reason=reason or "manual_reconcile")
                target_refresh = {
                    "ok": True,
                    "skipped": False,
                    "active_target_date": self._active_target_date,
                    "active_target_symbols": list(self._active_trade_symbols),
                    "active_subscription_symbols": list(self._active_subscription_symbols),
                }
            except Exception as exc:
                target_refresh = {"ok": False, "skipped": False, "error": str(exc)}

        prime_result = {"ok": True, "symbols": [], "skipped": True, "reason": "empty_symbols"}
        if prime_list:
            prime_result = self._prime_universe_symbols(
                prime_list,
                emit_signals=emit_signals,
                source=source or "runtime_api",
            )

        cleanup_result = {"ok": True, "symbols": [], "skipped": True, "reason": "empty_symbols"}
        if cleanup_list:
            cleanup_result = self._cleanup_universe_symbols(
                cleanup_list,
                source=source or "runtime_api",
            )

        return {
            "ok": bool(target_refresh.get("ok", True)) and bool(prime_result.get("ok", True)) and bool(cleanup_result.get("ok", True)),
            "environment": service_mod.ENVIRONMENT,
            "market_date": str(self._current_market_date or self._market_date()),
            "source": str(source or "runtime_api"),
            "reason": str(reason or "manual_reconcile"),
            "emit_signals": bool(emit_signals),
            "target_refresh": target_refresh,
            "prime": prime_result,
            "cleanup": cleanup_result,
        }

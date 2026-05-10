from __future__ import annotations

from .runtime_support import *
from ibkr_compute.market.pocketbase_sqlite import pb_json_dumps, pb_now_text, pb_record_id


class BacktestPersistenceMixin:
    def _empty_capture_summary(self, collection: str, run_id: str, status: str = "empty") -> dict:
        return {
            "collection": collection,
            "run_id": run_id,
            "attempted_count": 0,
            "saved_count": 0,
            "error_count": 0,
            "status": "disabled" if not self.pb else status,
            "errors": [],
        }

    def _append_capture_error(self, summary: dict, payload: dict, exc: Exception | str, fields: tuple[str, ...]):
        summary["error_count"] += 1
        summary["status"] = "partial"
        if len(summary["errors"]) >= TV_COMPARE_SAMPLE_LIMIT:
            return
        sample = {field: payload.get(field, "") for field in fields}
        if "bar_time_ms" in sample:
            sample["bar_time_ms"] = int(sample.get("bar_time_ms", 0) or 0)
        sample["error"] = str(exc)[:300]
        summary["errors"].append(sample)

    def _prepare_backtest_capture_payload(self, run_id: str, row: dict) -> dict:
        payload = dict(row or {})
        extra = self._parse_object(payload.get("extra"))
        extra["backtest_run_id"] = run_id
        payload["run_id"] = run_id
        payload["extra"] = extra
        return payload

    def _persist_rows_to_sqlite(
        self,
        collection: str,
        payloads: list[dict],
        summary: dict,
        error_fields: tuple[str, ...],
    ) -> bool:
        if not payloads:
            summary["status"] = "ok"
            summary["storage_source"] = "sqlite"
            return True
        table = str(collection or "").strip()
        if not table or not table.replace("_", "").isalnum():
            return False
        db_path = str(runtime_backtest_sqlite_path() or "").strip()
        if not db_path or not os.path.exists(db_path):
            return False

        try:
            with sqlite3.connect(db_path, timeout=30) as conn:
                conn.row_factory = sqlite3.Row
                table_rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
                available_columns = [str(row["name"] or "") for row in table_rows if str(row["name"] or "")]
                if not available_columns:
                    return False
                columns = [
                    column
                    for column in available_columns
                    if column in {"id", "created", "updated"}
                    or any(column in payload and payload.get(column) is not None for payload in payloads)
                ]
                if not columns:
                    return False
                now_text = pb_now_text()
                placeholders = ",".join(["?"] * len(columns))
                column_sql = ",".join(columns)
                values = []
                for payload in payloads:
                    if self._cancel_event.is_set():
                        raise BacktestCancelled()
                    row_values = []
                    for column in columns:
                        if column == "id":
                            value = str(payload.get("id") or pb_record_id())
                        elif column == "created":
                            value = str(payload.get("created") or now_text)
                        elif column == "updated":
                            value = str(payload.get("updated") or now_text)
                        else:
                            value = payload.get(column)
                        if isinstance(value, (dict, list)):
                            value = pb_json_dumps(value)
                        row_values.append(value)
                    values.append(tuple(row_values))
                conn.executemany(f"INSERT INTO {table} ({column_sql}) VALUES ({placeholders})", values)
                conn.commit()
            summary["saved_count"] = len(payloads)
            summary["status"] = "ok"
            summary["storage_source"] = "sqlite"
            return True
        except BacktestCancelled:
            raise
        except Exception as exc:
            summary["storage_source"] = "sqlite_error"
            summary["sqlite_error"] = str(exc)[:300]
            summary["status"] = "pending_pb_fallback"
            return False

    def _sqlite_persist_error_should_skip_pb_fallback(self, summary: dict) -> bool:
        error_text = str(summary.get("sqlite_error") or "").lower()
        return any(token in error_text for token in ("database is locked", "database or disk is full"))

    def _persist_backtest_collection_rows(
        self,
        collection: str,
        run_id: str,
        rows: list[dict],
        error_fields: tuple[str, ...],
    ) -> dict:
        summary = self._empty_capture_summary(collection, run_id)
        summary["attempted_count"] = len(rows)
        if not self.pb or not run_id:
            return summary
        if not rows:
            return summary

        summary["status"] = "ok"
        payloads = []
        for row in rows:
            if self._cancel_event.is_set():
                raise BacktestCancelled()
            payloads.append(self._prepare_backtest_capture_payload(run_id, row))

        if self._persist_rows_to_sqlite(collection, payloads, summary, error_fields):
            return summary
        if self._sqlite_persist_error_should_skip_pb_fallback(summary):
            summary["status"] = "skipped_sqlite_error"
            summary["error_count"] = len(payloads)
            summary["errors"] = [
                {
                    **{field: payload.get(field, "") for field in error_fields},
                    "error": str(summary.get("sqlite_error") or "")[:300],
                }
                for payload in payloads[:TV_COMPARE_SAMPLE_LIMIT]
            ]
            return summary
        summary["status"] = "ok"
        summary["error_count"] = 0
        summary["errors"] = []

        bulk_create = getattr(self.pb, "create_records", None)
        chunk_size = 50
        for index in range(0, len(payloads), chunk_size):
            if self._cancel_event.is_set():
                raise BacktestCancelled()
            chunk = payloads[index : index + chunk_size]
            if callable(bulk_create):
                try:
                    bulk_create(collection, chunk, timeout=60, batch_size=chunk_size)
                    summary["saved_count"] += len(chunk)
                    continue
                except Exception as exc:
                    for payload in chunk:
                        self._append_capture_error(summary, payload, exc, error_fields)
                    continue

            for payload in chunk:
                if self._cancel_event.is_set():
                    raise BacktestCancelled()
                try:
                    self.pb.create_record(collection, payload)
                    summary["saved_count"] += 1
                except Exception as exc:
                    self._append_capture_error(summary, payload, exc, error_fields)

        if summary["error_count"] and summary["saved_count"] == 0:
            summary["status"] = "error"
        return summary

    def _persist_backtest_indicators(self, run_id: str, indicator_rows: list[dict]) -> dict:
        return self._persist_backtest_collection_rows(
            BACKTEST_INDICATOR_COLLECTION,
            run_id,
            indicator_rows,
            ("bar_time_ms", "symbol"),
        )

    def _persist_backtest_signals(self, run_id: str, signal_rows: list[dict]) -> dict:
        return self._persist_backtest_collection_rows(
            BACKTEST_SIGNAL_COLLECTION,
            run_id,
            signal_rows,
            ("bar_time_ms", "symbol", "signal_id"),
        )

    def _persist_backtest_targets(self, run_id: str, target_rows: list[dict]) -> dict:
        return self._persist_backtest_collection_rows(
            BACKTEST_TARGET_COLLECTION,
            run_id,
            target_rows,
            ("date", "symbol"),
        )

    def _persist_backtest_reverse_signals(self, run_id: str, reverse_rows: list[dict]) -> dict:
        return self._persist_backtest_collection_rows(
            BACKTEST_REVERSE_SIGNAL_COLLECTION,
            run_id,
            reverse_rows,
            ("bar_time_ms", "symbol", "action_type", "signal_id"),
        )

    def _create_records_or_raise(self, collection: str, payloads: list[dict], timeout: int = 60, batch_size: int = 50):
        if not payloads:
            return
        bulk_create = getattr(self.pb, "create_records", None)
        if callable(bulk_create):
            bulk_create(collection, payloads, timeout=timeout, batch_size=batch_size)
            return
        for payload in payloads:
            if self._cancel_event.is_set():
                raise BacktestCancelled()
            self.pb.create_record(collection, payload)

    def _execution_profile_or_legacy(
        self,
        execution_profile: dict | None,
        commission_per_share: float,
        slippage_bps: float,
    ) -> dict:
        if execution_profile:
            return dict(execution_profile)
        return build_execution_cost_profile(
            {
                "commission_per_share": commission_per_share,
                "slippage_bps": slippage_bps,
            }
        )

    def _build_execution_trade_extra(
        self,
        position: dict,
        exit_result: dict,
        exit_commission_detail: dict,
        *,
        gross_pnl: float,
        net_pnl: float,
    ) -> dict:
        entry_commission = float(position.get("entry_commission", 0) or 0)
        exit_commission = float(exit_commission_detail.get("commission", 0) or 0)
        return {
            "fee_model": str(position.get("fee_model") or exit_commission_detail.get("fee_model") or DEFAULT_FEE_MODEL),
            "slippage_model": str(position.get("slippage_model") or exit_result.get("slippage_model") or DEFAULT_SLIPPAGE_MODEL),
            "entry_reference_price": round(float(position.get("entry_reference_price", position.get("entry_price", 0)) or 0), 4),
            "exit_reference_price": round(float(exit_result.get("raw_reference_price", 0) or 0), 4),
            "entry_slippage_bps": round(float(position.get("entry_slippage_bps", 0) or 0), 4),
            "exit_slippage_bps": round(float(exit_result.get("slippage_bps_applied", 0) or 0), 4),
            "entry_slippage_cost": round(float(position.get("entry_slippage_cost", 0) or 0), 4),
            "exit_slippage_cost": round(float(exit_result.get("slippage_cost", 0) or 0), 4),
            "estimated_slippage_cost": round(
                float(position.get("entry_slippage_cost", 0) or 0) + float(exit_result.get("slippage_cost", 0) or 0),
                4,
            ),
            "entry_commission": round(entry_commission, 4),
            "exit_commission": round(exit_commission, 4),
            "total_commission": round(entry_commission + exit_commission, 4),
            "entry_commission_detail": position.get("entry_commission_detail") or {},
            "exit_commission_detail": exit_commission_detail,
            "gross_pnl": round(float(gross_pnl or 0), 4),
            "net_pnl": round(float(net_pnl or 0), 4),
            "bar_cap_applied": bool(position.get("entry_bar_cap_applied")) or bool(exit_result.get("bar_cap_applied")),
            "limit_cap_applied": bool(position.get("entry_limit_cap_applied")) or bool(exit_result.get("limit_cap_applied")),
        }

    def _open_position(
        self,
        symbol: str,
        bar: dict,
        signal: dict,
        commission_per_share: float,
        slippage_bps: float,
        raw_fill_price: float | None = None,
        execution_profile: dict | None = None,
    ) -> dict:
        direction = str(signal.get("direction", "") or "")
        shares = max(0, int(signal.get("shares", 0) or 0))
        entry_reference = float(raw_fill_price if raw_fill_price is not None else bar.get("open", 0) or 0)
        raw_extra = signal.get("extra") or {}
        signal_extra = dict(raw_extra) if isinstance(raw_extra, dict) else {}
        entry_order_type = str(signal.get("entry_order_type", signal_extra.get("entry_order_type", "limit")) or "limit")
        entry_limit_price = float(signal.get("entry_price", signal.get("entry", 0)) or 0)
        profile = self._execution_profile_or_legacy(execution_profile, commission_per_share, slippage_bps)
        entry_result = apply_execution_slippage(
            price=entry_reference,
            direction=direction,
            is_entry=True,
            profile=profile,
            bar=bar,
            shares=shares,
            limit_price=entry_limit_price,
            order_type=entry_order_type,
        )
        fill_price = float(entry_result.get("fill_price", 0) or 0)
        entry_commission_detail = calculate_execution_commission(
            shares=shares,
            price=fill_price,
            side=execution_side(direction, True),
            profile=profile,
        )
        risk_r = float(signal.get("risk_r", signal_extra.get("risk_r", 0)) or 0)
        initial_stop_loss = float(
            signal.get("initial_stop_loss", signal_extra.get("initial_stop_loss", signal.get("stop_price", signal.get("stop_loss", 0)))) or 0
        )
        initial_take_profit = float(
            signal.get("initial_take_profit", signal_extra.get("initial_take_profit", signal.get("target_price", signal.get("take_profit", 0)))) or 0
        )
        return {
            "symbol": symbol,
            "direction": direction,
            "signal": str(signal.get("signal", "") or ""),
            "signal_id": str(signal.get("signal_id", "") or ""),
            "reason": str(signal.get("reason", "") or ""),
            "entry_bar_ms": int(bar.get("bar_time_ms", 0) or 0),
            "entry_us_time": str(bar.get("us_time", "") or ""),
            "entry_cn_time": str(bar.get("cn_time", "") or ""),
            "entry_price": round(fill_price, 4),
            "entry_reference_price": round(float(entry_result.get("raw_reference_price", entry_reference) or 0), 4),
            "entry_limit_price": entry_limit_price,
            "target_price": float(signal.get("target_price", signal.get("take_profit", 0)) or 0),
            "stop_price": float(signal.get("stop_price", signal.get("stop_loss", 0)) or 0),
            "original_stop_loss": float(signal.get("stop_price", signal.get("stop_loss", 0)) or 0),
            "initial_stop_loss": initial_stop_loss,
            "initial_take_profit": initial_take_profit,
            "risk_r": risk_r,
            "exit_policy_profile": str(signal_extra.get("exit_policy_profile") or signal.get("exit_policy_profile", "") or ""),
            "exit_policy": str(signal_extra.get("exit_policy") or signal.get("exit_policy", "") or ""),
            "exit_policy_type": str(signal_extra.get("exit_policy_type") or signal.get("exit_policy_type", "") or ""),
            "exit_policy_settings": dict(signal_extra.get("exit_policy_settings") or {}),
            "target_state": dict(signal_extra.get("target_state") or {}),
            "trail_state": dict(signal_extra.get("trail_state") or {}),
            "target_stop_adjust_count": 0,
            "last_target_policy_update": {},
            "last_stop_atr": float((signal.get("extra") or {}).get("atr", 0) or 0),
            "atr_stop_adjust_count": 0,
            "mfe": 0.0,
            "mae": 0.0,
            "shares": shares,
            "bars_held": 0,
            "entry_commission": round(float(entry_commission_detail.get("commission", 0) or 0), 4),
            "entry_commission_detail": entry_commission_detail,
            "entry_slippage_bps": round(float(entry_result.get("slippage_bps_applied", 0) or 0), 4),
            "entry_slippage_cost": round(float(entry_result.get("slippage_cost", 0) or 0), 4),
            "entry_bar_cap_applied": bool(entry_result.get("bar_cap_applied")),
            "entry_limit_cap_applied": bool(entry_result.get("limit_cap_applied")),
            "fee_model": str(profile.get("fee_model") or DEFAULT_FEE_MODEL),
            "slippage_model": str(profile.get("slippage_model") or DEFAULT_SLIPPAGE_MODEL),
            "signal_bar_ms": int(signal.get("signal_bar_ms", 0) or 0),
            "signal_us_time": str(signal.get("signal_us_time", "") or ""),
            "signal_close": float(signal.get("signal_close", 0) or 0),
            "entry_order_type": entry_order_type,
            "setup": str(signal.get("setup", "") or ""),
        }

    def _check_exit(
        self,
        position: dict,
        bar: dict,
        commission_per_share: float,
        slippage_bps: float,
        execution_profile: dict | None = None,
    ) -> Optional[dict]:
        direction = str(position.get("direction", "") or "")
        stop_price = float(position.get("stop_price", 0) or 0)
        target_price = float(position.get("target_price", 0) or 0)
        hard_target = exit_policy_uses_hard_target(position)
        high = float(bar.get("high", 0) or 0)
        low = float(bar.get("low", 0) or 0)
        close = float(bar.get("close", 0) or 0)
        shares = int(position.get("shares", 0) or 0)
        position["bars_held"] = int(position.get("bars_held", 0) or 0) + 1
        entry_price = float(position.get("entry_price", 0) or 0)
        if entry_price > 0:
            if direction == "long":
                favorable = max(0.0, high - entry_price)
                adverse = max(0.0, entry_price - low)
            else:
                favorable = max(0.0, entry_price - low)
                adverse = max(0.0, high - entry_price)
            position["mfe"] = max(float(position.get("mfe", 0) or 0), favorable)
            position["mae"] = max(float(position.get("mae", 0) or 0), adverse)

        exit_reason = ""
        raw_exit_price = 0.0
        if direction == "long":
            if stop_price > 0 and low <= stop_price:
                raw_exit_price = stop_price
                exit_reason = "stop_loss"
            elif hard_target and target_price > 0 and high >= target_price:
                raw_exit_price = target_price
                exit_reason = "take_profit"
        else:
            if stop_price > 0 and high >= stop_price:
                raw_exit_price = stop_price
                exit_reason = "stop_loss"
            elif hard_target and target_price > 0 and low <= target_price:
                raw_exit_price = target_price
                exit_reason = "take_profit"

        if not exit_reason:
            return None

        profile = self._execution_profile_or_legacy(execution_profile, commission_per_share, slippage_bps)
        raw_exit_price = maybe_stop_gap_reference(
            raw_exit_price=raw_exit_price,
            direction=direction,
            exit_reason=exit_reason,
            bar=bar,
            profile=profile,
        )
        exit_result = apply_execution_slippage(
            price=raw_exit_price,
            direction=direction,
            is_entry=False,
            profile=profile,
            bar=bar,
            shares=shares,
        )
        exit_price = float(exit_result.get("fill_price", 0) or 0)
        exit_commission_detail = calculate_execution_commission(
            shares=shares,
            price=exit_price,
            side=execution_side(direction, False),
            profile=profile,
        )
        exit_commission = round(float(exit_commission_detail.get("commission", 0) or 0), 4)
        gross_pnl = self._calc_pnl(direction, float(position["entry_price"]), exit_price, shares)
        pnl = gross_pnl - float(position["entry_commission"]) - exit_commission
        pnl_pct = 0.0
        position_cost = max(0.01, float(position["entry_price"]) * shares)
        pnl_pct = (pnl / position_cost) * 100.0
        execution_extra = self._build_execution_trade_extra(
            position,
            exit_result,
            exit_commission_detail,
            gross_pnl=gross_pnl,
            net_pnl=pnl,
        )
        return {
            "symbol": position["symbol"],
            "direction": direction,
            "signal": position.get("signal", ""),
            "signal_id": position.get("signal_id", ""),
            "reason": position.get("reason", ""),
            "entry_bar_ms": int(position["entry_bar_ms"]),
            "entry_us_time": position["entry_us_time"],
            "entry_cn_time": position["entry_cn_time"],
            "entry_price": round(float(position["entry_price"]), 4),
            "exit_bar_ms": int(bar.get("bar_time_ms", 0) or 0),
            "exit_us_time": str(bar.get("us_time", "") or ""),
            "exit_cn_time": str(bar.get("cn_time", "") or ""),
            "exit_price": round(exit_price, 4),
            "shares": shares,
            "pnl": round(pnl, 4),
            "pnl_pct": round(pnl_pct, 4),
            "bars_held": int(position.get("bars_held", 0) or 0),
            "exit_reason": exit_reason,
            "session_type": str(bar.get("session_type", "") or "regular"),
            "extra": {
                "stop_price": stop_price,
                "target_price": target_price,
                "mfe": round(float(position.get("mfe", 0) or 0), 4),
                "mae": round(float(position.get("mae", 0) or 0), 4),
                "atr_stop_adjust_count": int(position.get("atr_stop_adjust_count", 0) or 0),
                "last_atr_stop_adjust": position.get("last_atr_stop_adjust") or {},
                "target_stop_adjust_count": int(position.get("target_stop_adjust_count", 0) or 0),
                "last_target_policy_update": position.get("last_target_policy_update") or {},
                "exit_policy_profile": position.get("exit_policy_profile", ""),
                "exit_policy": position.get("exit_policy", ""),
                "exit_policy_type": position.get("exit_policy_type", ""),
                "risk_r": float(position.get("risk_r", 0) or 0),
                "initial_stop_loss": float(position.get("initial_stop_loss", 0) or 0),
                "initial_take_profit": float(position.get("initial_take_profit", 0) or 0),
                "exit_policy_settings": position.get("exit_policy_settings") or {},
                "target_state": position.get("target_state") or {},
                "trail_state": position.get("trail_state") or {},
                "signal_bar_ms": int(position.get("signal_bar_ms", 0) or 0),
                "signal_us_time": position.get("signal_us_time", ""),
                "signal_close": float(position.get("signal_close", 0) or 0),
                **execution_extra,
                **build_runtime_timestamps(),
            },
        }

    def _close_position(
        self,
        position: dict,
        bar: dict,
        commission_per_share: float,
        slippage_bps: float,
        reason: str,
        execution_profile: dict | None = None,
    ) -> dict:
        direction = str(position.get("direction", "") or "")
        shares = int(position.get("shares", 0) or 0)
        raw_exit_price = float(bar.get("close", 0) or 0)
        entry_price = float(position.get("entry_price", 0) or 0)
        if entry_price > 0 and raw_exit_price > 0:
            favorable = max(0.0, raw_exit_price - entry_price) if direction == "long" else max(0.0, entry_price - raw_exit_price)
            adverse = max(0.0, entry_price - raw_exit_price) if direction == "long" else max(0.0, raw_exit_price - entry_price)
            position["mfe"] = max(float(position.get("mfe", 0) or 0), favorable)
            position["mae"] = max(float(position.get("mae", 0) or 0), adverse)
        profile = self._execution_profile_or_legacy(execution_profile, commission_per_share, slippage_bps)
        exit_result = apply_execution_slippage(
            price=raw_exit_price,
            direction=direction,
            is_entry=False,
            profile=profile,
            bar=bar,
            shares=shares,
        )
        exit_price = float(exit_result.get("fill_price", 0) or 0)
        exit_commission_detail = calculate_execution_commission(
            shares=shares,
            price=exit_price,
            side=execution_side(direction, False),
            profile=profile,
        )
        exit_commission = round(float(exit_commission_detail.get("commission", 0) or 0), 4)
        gross_pnl = self._calc_pnl(direction, float(position["entry_price"]), exit_price, shares)
        pnl = gross_pnl - float(position["entry_commission"]) - exit_commission
        position_cost = max(0.01, float(position["entry_price"]) * shares)
        pnl_pct = (pnl / position_cost) * 100.0
        execution_extra = self._build_execution_trade_extra(
            position,
            exit_result,
            exit_commission_detail,
            gross_pnl=gross_pnl,
            net_pnl=pnl,
        )
        return {
            "symbol": position["symbol"],
            "direction": direction,
            "signal": position.get("signal", ""),
            "signal_id": position.get("signal_id", ""),
            "reason": position.get("reason", ""),
            "entry_bar_ms": int(position["entry_bar_ms"]),
            "entry_us_time": position["entry_us_time"],
            "entry_cn_time": position["entry_cn_time"],
            "entry_price": round(float(position["entry_price"]), 4),
            "exit_bar_ms": int(bar.get("bar_time_ms", 0) or 0),
            "exit_us_time": str(bar.get("us_time", "") or ""),
            "exit_cn_time": str(bar.get("cn_time", "") or ""),
            "exit_price": round(exit_price, 4),
            "shares": shares,
            "pnl": round(pnl, 4),
            "pnl_pct": round(pnl_pct, 4),
            "bars_held": int(position.get("bars_held", 0) or 0),
            "exit_reason": reason,
            "session_type": str(bar.get("session_type", "") or "regular"),
            "extra": {
                "stop_price": float(position.get("stop_price", 0) or 0),
                "target_price": float(position.get("target_price", 0) or 0),
                "mfe": round(float(position.get("mfe", 0) or 0), 4),
                "mae": round(float(position.get("mae", 0) or 0), 4),
                "atr_stop_adjust_count": int(position.get("atr_stop_adjust_count", 0) or 0),
                "last_atr_stop_adjust": position.get("last_atr_stop_adjust") or {},
                "target_stop_adjust_count": int(position.get("target_stop_adjust_count", 0) or 0),
                "last_target_policy_update": position.get("last_target_policy_update") or {},
                "exit_policy_profile": position.get("exit_policy_profile", ""),
                "exit_policy": position.get("exit_policy", ""),
                "exit_policy_type": position.get("exit_policy_type", ""),
                "risk_r": float(position.get("risk_r", 0) or 0),
                "initial_stop_loss": float(position.get("initial_stop_loss", 0) or 0),
                "initial_take_profit": float(position.get("initial_take_profit", 0) or 0),
                "exit_policy_settings": position.get("exit_policy_settings") or {},
                "target_state": position.get("target_state") or {},
                "trail_state": position.get("trail_state") or {},
                "signal_bar_ms": int(position.get("signal_bar_ms", 0) or 0),
                "signal_us_time": position.get("signal_us_time", ""),
                "signal_close": float(position.get("signal_close", 0) or 0),
                **execution_extra,
                **build_runtime_timestamps(),
            },
        }

    def _apply_slippage(self, price: float, direction: str, is_entry: bool, bps: float) -> float:
        if price <= 0:
            return 0.0
        slip = max(0.0, float(bps)) / 10000.0
        if direction == "long":
            return price * (1.0 + slip) if is_entry else price * (1.0 - slip)
        return price * (1.0 - slip) if is_entry else price * (1.0 + slip)

    def _calc_pnl(self, direction: str, entry_price: float, exit_price: float, shares: int) -> float:
        if direction == "long":
            return (exit_price - entry_price) * shares
        return (entry_price - exit_price) * shares

    def _collapse_daily_equity(self, points: list[dict], initial_capital: float) -> list[dict]:
        if not points:
            return []
        collapsed = []
        by_date = {}
        for point in points:
            by_date[str(point.get("date") or "")] = round(float(point.get("equity", initial_capital) or initial_capital), 2)
        for date_text in sorted(by_date.keys()):
            collapsed.append({"date": date_text, "equity": by_date[date_text]})
        return collapsed

    def _build_benchmark_curve(
        self,
        symbol: str,
        source_environment: str,
        date_from: str,
        date_to: str,
        session_mode: str,
        initial_capital: float,
        *,
        allow_backfill: bool = True,
    ) -> list[dict]:
        benchmark_symbol = str(symbol or "").strip().upper()
        if not benchmark_symbol:
            return []
        bars = self._load_symbol_bars(
            benchmark_symbol,
            source_environment,
            date_from,
            date_to,
            session_mode,
            allow_backfill=allow_backfill,
        )
        if not bars:
            return []
        closes_by_day = {}
        for bar in bars:
            day = str(bar.get("us_time", "") or "")[:10]
            closes_by_day[day] = float(bar.get("close", 0) or 0)
        items = []
        first_close = 0.0
        for day in sorted(closes_by_day.keys()):
            close = closes_by_day[day]
            if close <= 0:
                continue
            if first_close <= 0:
                first_close = close
            equity = initial_capital * (close / first_close)
            items.append({"date": day, "equity": round(equity, 2)})
        return items

    def _compute_metrics(self, initial_capital: float, trades: list[dict], daily_equity: list[dict]) -> dict:
        trade_count = len(trades)
        net_pnl = round(sum(float(item.get("pnl", 0) or 0) for item in trades), 4)
        gross_profit = round(sum(max(0.0, float(item.get("pnl", 0) or 0)) for item in trades), 4)
        gross_loss = round(sum(min(0.0, float(item.get("pnl", 0) or 0)) for item in trades), 4)
        winners = [item for item in trades if float(item.get("pnl", 0) or 0) > 0]
        losers = [item for item in trades if float(item.get("pnl", 0) or 0) < 0]
        win_rate = (len(winners) / trade_count * 100.0) if trade_count else 0.0
        avg_win = (gross_profit / len(winners)) if winners else 0.0
        avg_loss = (gross_loss / len(losers)) if losers else 0.0
        profit_factor = (gross_profit / abs(gross_loss)) if gross_loss < 0 else 0.0
        expectancy = (net_pnl / trade_count) if trade_count else 0.0
        total_return_pct = (net_pnl / initial_capital * 100.0) if initial_capital > 0 else 0.0
        exit_reason_breakdown = {}
        direction_breakdown = {}
        mfe_values = []
        mae_values = []
        for item in trades:
            exit_reason = str(item.get("exit_reason") or "unknown")
            direction = str(item.get("direction") or "unknown")
            exit_reason_breakdown[exit_reason] = int(exit_reason_breakdown.get(exit_reason, 0) or 0) + 1
            direction_breakdown[direction] = int(direction_breakdown.get(direction, 0) or 0) + 1
            extra = self._parse_object(item.get("extra"))
            mfe_values.append(float(extra.get("mfe", 0) or 0))
            mae_values.append(float(extra.get("mae", 0) or 0))

        daily_returns = []
        previous_equity = initial_capital
        for point in daily_equity:
            equity = float(point.get("equity", previous_equity) or previous_equity)
            if previous_equity > 0:
                daily_returns.append((equity - previous_equity) / previous_equity)
            previous_equity = equity

        sharpe = 0.0
        sortino = 0.0
        if len(daily_returns) >= 2:
            mean_return = statistics.fmean(daily_returns)
            std_return = statistics.pstdev(daily_returns)
            if std_return > 0:
                sharpe = mean_return / std_return * math.sqrt(252)
            downside = [min(0.0, value) for value in daily_returns]
            downside_std = statistics.pstdev(downside)
            if downside_std > 0:
                sortino = mean_return / downside_std * math.sqrt(252)

        max_drawdown_pct = 0.0
        equity_peak = initial_capital
        for point in daily_equity:
            equity = float(point.get("equity", initial_capital) or initial_capital)
            equity_peak = max(equity_peak, equity)
            if equity_peak > 0:
                drawdown = (equity - equity_peak) / equity_peak * 100.0
                max_drawdown_pct = min(max_drawdown_pct, drawdown)

        monthly_returns = []
        if daily_equity:
            month_start_equity = None
            month_key = ""
            last_equity = initial_capital
            for point in daily_equity:
                day = str(point.get("date") or "")
                current_key = day[:7]
                equity = float(point.get("equity", initial_capital) or initial_capital)
                if current_key != month_key:
                    if month_key and month_start_equity and month_start_equity > 0:
                        monthly_returns.append(
                            {
                                "month": month_key,
                                "return_pct": round((last_equity - month_start_equity) / month_start_equity * 100.0, 4),
                            }
                        )
                    month_key = current_key
                    month_start_equity = last_equity if last_equity > 0 else initial_capital
                last_equity = equity
            if month_key and month_start_equity and month_start_equity > 0:
                monthly_returns.append(
                    {
                        "month": month_key,
                        "return_pct": round((last_equity - month_start_equity) / month_start_equity * 100.0, 4),
                    }
                )

        return {
            "trade_count": trade_count,
            "net_pnl": round(net_pnl, 4),
            "gross_profit": round(gross_profit, 4),
            "gross_loss": round(gross_loss, 4),
            "total_return_pct": round(total_return_pct, 4),
            "win_rate": round(win_rate, 4),
            "avg_win": round(avg_win, 4),
            "avg_loss": round(avg_loss, 4),
            "profit_factor": round(profit_factor, 4),
            "expectancy": round(expectancy, 4),
            "sharpe": round(sharpe, 4),
            "sortino": round(sortino, 4),
            "max_drawdown_pct": round(abs(max_drawdown_pct), 4),
            "ending_equity": round(initial_capital + net_pnl, 4),
            "daily_equity_count": len(daily_equity),
            "monthly_returns": monthly_returns,
            "exit_reason_breakdown": exit_reason_breakdown,
            "direction_breakdown": direction_breakdown,
            "avg_mfe": round(statistics.fmean(mfe_values), 4) if mfe_values else 0.0,
            "avg_mae": round(statistics.fmean(mae_values), 4) if mae_values else 0.0,
        }

    def _persist_trades(self, run_id: str, trades: list[dict]):
        if not self.pb or not run_id or not trades:
            return
        payloads = []
        for index, trade in enumerate(trades, start=1):
            if self._cancel_event.is_set():
                raise BacktestCancelled()
            payloads.append(
                {
                    "run_id": run_id,
                    "symbol": trade.get("symbol", ""),
                    "direction": trade.get("direction", ""),
                    "signal": trade.get("signal", ""),
                    "signal_id": trade.get("signal_id", ""),
                    "entry_us_time": trade.get("entry_us_time", ""),
                    "exit_us_time": trade.get("exit_us_time", ""),
                    "entry_bar_ms": int(trade.get("entry_bar_ms", 0) or 0),
                    "exit_bar_ms": int(trade.get("exit_bar_ms", 0) or 0),
                    "entry_price": float(trade.get("entry_price", 0) or 0),
                    "exit_price": float(trade.get("exit_price", 0) or 0),
                    "shares": int(trade.get("shares", 0) or 0),
                    "pnl": float(trade.get("pnl", 0) or 0),
                    "pnl_pct": float(trade.get("pnl_pct", 0) or 0),
                    "bars_held": int(trade.get("bars_held", 0) or 0),
                    "exit_reason": trade.get("exit_reason", ""),
                    "session_type": trade.get("session_type", "regular"),
                    "trade_index": index,
                    "extra": trade.get("extra", {}),
                }
            )
        trade_summary = self._empty_capture_summary(TRADE_COLLECTION, run_id)
        trade_summary["attempted_count"] = len(payloads)
        if self._persist_rows_to_sqlite(
            TRADE_COLLECTION,
            payloads,
            trade_summary,
            ("symbol", "signal_id", "entry_bar_ms", "exit_bar_ms"),
        ):
            return
        if self._sqlite_persist_error_should_skip_pb_fallback(trade_summary):
            return
        self._create_records_or_raise(TRADE_COLLECTION, payloads)

    def _build_replay_timeline(self, symbol: str, bars: list[dict], params: dict) -> list[dict]:
        timeline = build_runtime_timeline(
            symbol,
            "5m",
            bars,
            params=params,
            include_signals=True,
        )
        return [
            {
                "bar_time_ms": int(row.get("bar_time_ms", 0) or 0),
                "us_time": row.get("us_time", ""),
                "session_type": row.get("session_type", "regular"),
                "open": round(float(row.get("open", 0) or 0), 4),
                "high": round(float(row.get("high", 0) or 0), 4),
                "low": round(float(row.get("low", 0) or 0), 4),
                "close": round(float(row.get("close", 0) or 0), 4),
                "atr": round(float(row.get("atr", 0) or 0), 4),
                "sd_zone": row.get("sd_zone", ""),
                "sd_trend": row.get("sd_trend", 0),
                "dtp_phase": row.get("dtp_phase", ""),
                "signal": row.get("signal"),
            }
            for row in timeline.get("rows", [])
        ]

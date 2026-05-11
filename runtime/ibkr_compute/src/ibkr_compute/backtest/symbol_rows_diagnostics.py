from __future__ import annotations

from .runtime_support import *


class BacktestSymbolRowsDiagnosticsMixin:
    def _append_backtest_risk_adjustment(self, position: dict | None, event: dict | None, limit: int = 200):
        if not position or not isinstance(event, dict):
            return
        bar_time_ms = int(event.get("bar_time_ms", 0) or 0)
        item = dict(event)
        item["bar_time_ms"] = bar_time_ms
        if bar_time_ms > 0:
            item["us_time"] = str(item.get("us_time") or format_us_time(bar_time_ms))
            item["cn_time"] = str(item.get("cn_time") or format_cn_time(bar_time_ms))
        item["signal_id"] = str(item.get("signal_id") or position.get("signal_id", "") or "")
        item["direction"] = str(item.get("direction") or position.get("direction", "") or "")
        item["event_type"] = str(item.get("event_type") or "risk_adjustment")
        for key in ("old_sl", "new_sl", "old_tp", "new_tp", "current_price", "current_atr", "progress_r", "atr_deviation", "mfe_r"):
            if key in item:
                try:
                    item[key] = round(float(item.get(key) or 0), 4)
                except Exception:
                    item[key] = 0.0
        adjustments = position.setdefault("risk_adjustments", [])
        if not isinstance(adjustments, list):
            adjustments = []
        adjustments.append(item)
        position["risk_adjustments"] = adjustments[-max(1, int(limit or 1)) :]

    def _count_values(self, rows: list[dict], field_name: str) -> dict:
        counts = {}
        for row in rows:
            key = str(row.get(field_name, "") or "").strip()
            if not key:
                continue
            counts[key] = counts.get(key, 0) + 1
        return counts

    def _count_signal_status_reasons(self, rows: list[dict]) -> dict:
        counts = {}
        for row in rows:
            extra = self._parse_object(row.get("extra"))
            reason = str(extra.get("signal_status_reason", "") or "").strip()
            if not reason:
                continue
            counts[reason] = counts.get(reason, 0) + 1
        return counts

    def _build_daily_scan_match_diagnostics(self, target_rows: list[dict], signal_rows: list[dict]) -> dict:
        selected_by_date: dict[str, set[str]] = {}
        selected_pairs = set()
        for row in target_rows or []:
            symbol = str(row.get("symbol", "") or "").strip().upper()
            date_text = str(row.get("date", "") or "")[:10]
            if not symbol or not date_text:
                continue
            selected_by_date.setdefault(date_text, set()).add(symbol)
            selected_pairs.add((date_text, symbol))

        def pct(numerator: int, denominator: int) -> float:
            return round((float(numerator) / float(denominator)) * 100.0, 4) if denominator else 0.0

        def bump(counts: dict, key: str, amount: int = 1):
            if not key:
                return
            counts[key] = int(counts.get(key, 0) or 0) + amount

        def top_counts(counts: dict, limit: int = 12) -> list[dict]:
            return [
                {"key": key, "count": int(value)}
                for key, value in sorted(
                    counts.items(),
                    key=lambda item: (-int(item[1] or 0), str(item[0])),
                )[: max(0, int(limit or 0))]
            ]

        daily_stats = {
            date_text: {
                "date": date_text,
                "selected_count": len(symbols),
                "signal_count": 0,
                "selected_day_signal_count": 0,
                "off_plan_signal_count": 0,
                "not_selected_signal_count": 0,
                "executed_signal_count": 0,
            }
            for date_text, symbols in selected_by_date.items()
        }
        symbol_stats: dict[str, dict] = {}
        selected_pairs_with_signal = set()
        selected_pairs_with_executed = set()
        generated_signal_count = len(signal_rows or [])
        selected_day_signal_count = 0
        selected_day_executed_signal_count = 0
        off_plan_signal_count = 0
        not_selected_signal_count = 0
        executed_signal_count = 0
        not_selected_symbol_counts: dict[str, int] = {}
        not_selected_date_counts: dict[str, int] = {}
        off_plan_symbol_counts: dict[str, int] = {}
        matched_symbol_counts: dict[str, int] = {}
        skipped_not_selected_samples = []

        for row in signal_rows or []:
            symbol = str(row.get("symbol", "") or "").strip().upper()
            date_text = str(row.get("date", "") or row.get("us_time", "") or "")[:10]
            status = str(row.get("status", "") or "").strip().lower()
            extra = self._parse_object(row.get("extra"))
            reason = str(extra.get("signal_status_reason", "") or "").strip()
            if not symbol:
                continue

            symbol_stat = symbol_stats.setdefault(
                symbol,
                {
                    "symbol": symbol,
                    "signal_count": 0,
                    "selected_day_signal_count": 0,
                    "off_plan_signal_count": 0,
                    "not_selected_signal_count": 0,
                    "executed_signal_count": 0,
                },
            )
            symbol_stat["signal_count"] += 1
            if date_text:
                day_stat = daily_stats.setdefault(
                    date_text,
                    {
                        "date": date_text,
                        "selected_count": len(selected_by_date.get(date_text, set())),
                        "signal_count": 0,
                        "selected_day_signal_count": 0,
                        "off_plan_signal_count": 0,
                        "not_selected_signal_count": 0,
                        "executed_signal_count": 0,
                    },
                )
                day_stat["signal_count"] += 1
            else:
                day_stat = None

            selected_pair = bool(date_text and (date_text, symbol) in selected_pairs)
            if selected_pair:
                selected_day_signal_count += 1
                selected_pairs_with_signal.add((date_text, symbol))
                symbol_stat["selected_day_signal_count"] += 1
                bump(matched_symbol_counts, symbol)
                if day_stat is not None:
                    day_stat["selected_day_signal_count"] += 1
            else:
                off_plan_signal_count += 1
                symbol_stat["off_plan_signal_count"] += 1
                bump(off_plan_symbol_counts, symbol)
                if day_stat is not None:
                    day_stat["off_plan_signal_count"] += 1

            if reason == "symbol_not_selected_for_day":
                not_selected_signal_count += 1
                symbol_stat["not_selected_signal_count"] += 1
                bump(not_selected_symbol_counts, symbol)
                bump(not_selected_date_counts, date_text or "unknown")
                if day_stat is not None:
                    day_stat["not_selected_signal_count"] += 1
                if len(skipped_not_selected_samples) < 10:
                    skipped_not_selected_samples.append(
                        {
                            "symbol": symbol,
                            "date": date_text,
                            "us_time": str(row.get("us_time", "") or ""),
                            "direction": str(row.get("direction", "") or ""),
                            "status": status,
                            "reason": reason,
                        }
                    )

            if status == "executed":
                executed_signal_count += 1
                symbol_stat["executed_signal_count"] += 1
                if day_stat is not None:
                    day_stat["executed_signal_count"] += 1
                if selected_pair:
                    selected_day_executed_signal_count += 1
                    selected_pairs_with_executed.add((date_text, symbol))

        daily_summary = []
        for item in sorted(daily_stats.values(), key=lambda value: str(value.get("date", ""))):
            signal_count = int(item.get("signal_count", 0) or 0)
            selected_day_signals = int(item.get("selected_day_signal_count", 0) or 0)
            item["selected_day_signal_rate_pct"] = pct(selected_day_signals, signal_count)
            daily_summary.append(item)

        symbol_summary = list(symbol_stats.values())
        symbol_summary.sort(key=lambda item: (-int(item.get("signal_count", 0) or 0), str(item.get("symbol", ""))))

        return {
            "enabled": bool(selected_pairs),
            "target_date_count": len(selected_by_date),
            "selected_pair_count": len(selected_pairs),
            "selected_symbol_count": len({symbol for _, symbol in selected_pairs}),
            "generated_signal_count": generated_signal_count,
            "selected_day_signal_count": selected_day_signal_count,
            "off_plan_signal_count": off_plan_signal_count,
            "not_selected_signal_count": not_selected_signal_count,
            "executed_signal_count": executed_signal_count,
            "selected_day_executed_signal_count": selected_day_executed_signal_count,
            "selected_pair_with_signal_count": len(selected_pairs_with_signal),
            "selected_pair_with_executed_count": len(selected_pairs_with_executed),
            "selected_day_signal_rate_pct": pct(selected_day_signal_count, generated_signal_count),
            "not_selected_signal_rate_pct": pct(not_selected_signal_count, generated_signal_count),
            "selected_day_execution_rate_pct": pct(selected_day_executed_signal_count, selected_day_signal_count),
            "selected_pair_hit_rate_pct": pct(len(selected_pairs_with_signal), len(selected_pairs)),
            "selected_pair_execution_hit_rate_pct": pct(len(selected_pairs_with_executed), len(selected_pairs)),
            "top_not_selected_symbols": top_counts(not_selected_symbol_counts),
            "top_not_selected_dates": top_counts(not_selected_date_counts),
            "top_off_plan_symbols": top_counts(off_plan_symbol_counts),
            "top_matched_symbols": top_counts(matched_symbol_counts),
            "skipped_not_selected_samples": skipped_not_selected_samples,
            "symbol_summary": symbol_summary[:20],
            "daily_summary": daily_summary,
        }

    def _build_backtest_funnel_metrics(
        self,
        request: dict,
        target_rows: list[dict],
        signal_rows: list[dict],
        trades: list[dict],
    ) -> dict:
        def pct(numerator: int, denominator: int) -> float:
            return round((float(numerator) / float(denominator)) * 100.0, 4) if denominator else 0.0

        def row_date(row: dict, *field_names: str) -> str:
            for field_name in field_names:
                value = row.get(field_name)
                if value not in (None, ""):
                    text = str(value)
                    if len(text) >= 10:
                        return text[:10]
            bar_ms = int(row.get("bar_time_ms", row.get("entry_bar_ms", 0)) or 0)
            return ms_to_et(bar_ms).strftime("%Y-%m-%d") if bar_ms > 0 else ""

        target_pairs_by_date: dict[str, set[str]] = {}
        for row in target_rows or []:
            symbol = str(row.get("symbol", "") or "").strip().upper()
            date_text = row_date(row, "date", "us_time")
            if not symbol or not date_text:
                continue
            target_pairs_by_date.setdefault(date_text, set()).add(symbol)

        target_enabled = bool(target_pairs_by_date) and str(request.get("symbol_source") or "") == "daily_scan_replay"
        signal_counts_by_date: dict[str, int] = {}
        executed_signal_counts_by_date: dict[str, int] = {}
        signal_pairs_by_date: dict[str, set[str]] = {}
        for row in signal_rows or []:
            symbol = str(row.get("symbol", "") or "").strip().upper()
            date_text = row_date(row, "date", "us_time")
            if not symbol or not date_text:
                continue
            if target_enabled and symbol not in target_pairs_by_date.get(date_text, set()):
                continue
            signal_pairs_by_date.setdefault(date_text, set()).add(symbol)
            signal_counts_by_date[date_text] = int(signal_counts_by_date.get(date_text, 0) or 0) + 1
            if str(row.get("status", "") or "").strip().lower() == "executed":
                executed_signal_counts_by_date[date_text] = int(executed_signal_counts_by_date.get(date_text, 0) or 0) + 1

        open_counts_by_date: dict[str, int] = {}
        opened_pairs_by_date: dict[str, set[str]] = {}
        for row in trades or []:
            date_text = row_date(row, "entry_us_time")
            if not date_text:
                continue
            open_counts_by_date[date_text] = int(open_counts_by_date.get(date_text, 0) or 0) + 1
            symbol = str(row.get("symbol", "") or "").strip().upper()
            if symbol:
                opened_pairs_by_date.setdefault(date_text, set()).add(symbol)

        all_dates = sorted(
            set(target_pairs_by_date.keys())
            | set(signal_counts_by_date.keys())
            | set(executed_signal_counts_by_date.keys())
            | set(open_counts_by_date.keys())
        )
        daily_funnel = []
        for date_text in all_dates:
            target_count = len(target_pairs_by_date.get(date_text, set())) if target_enabled else 0
            signal_count = int(signal_counts_by_date.get(date_text, 0) or 0)
            target_signal_count = (
                len(target_pairs_by_date.get(date_text, set()) & signal_pairs_by_date.get(date_text, set()))
                if target_enabled
                else 0
            )
            executed_signal_count = int(executed_signal_counts_by_date.get(date_text, 0) or 0)
            open_count = int(open_counts_by_date.get(date_text, 0) or 0)
            target_entry_count = (
                len(target_pairs_by_date.get(date_text, set()) & opened_pairs_by_date.get(date_text, set()))
                if target_enabled
                else 0
            )
            daily_funnel.append(
                {
                    "date": date_text,
                    "target_enabled": target_enabled,
                    "target_count": target_count,
                    "signal_count": signal_count,
                    "target_signal_count": target_signal_count,
                    "executed_signal_count": executed_signal_count,
                    "target_entry_count": target_entry_count,
                    "open_count": open_count,
                    "target_to_signal_rate": pct(target_signal_count, target_count) if target_enabled else 0.0,
                    "target_to_entry_rate": pct(target_entry_count, target_count) if target_enabled else 0.0,
                    "signal_to_entry_rate": pct(executed_signal_count, signal_count),
                }
            )

        target_count = sum(len(symbols) for symbols in target_pairs_by_date.values()) if target_enabled else 0
        target_signal_count = (
            sum(
                len(target_pairs_by_date.get(date_text, set()) & signal_pairs_by_date.get(date_text, set()))
                for date_text in target_pairs_by_date.keys()
            )
            if target_enabled
            else 0
        )
        target_entry_count = (
            sum(
                len(target_pairs_by_date.get(date_text, set()) & opened_pairs_by_date.get(date_text, set()))
                for date_text in target_pairs_by_date.keys()
            )
            if target_enabled
            else 0
        )
        signal_count = sum(int(value or 0) for value in signal_counts_by_date.values())
        executed_signal_count = sum(int(value or 0) for value in executed_signal_counts_by_date.values())
        open_count = sum(int(value or 0) for value in open_counts_by_date.values())
        return {
            "daily_open_counts": [
                {"date": date_text, "open_count": int(open_counts_by_date.get(date_text, 0) or 0)}
                for date_text in sorted(open_counts_by_date.keys())
            ],
            "daily_funnel": daily_funnel,
            "target_funnel_enabled": target_enabled,
            "funnel_signal_count": signal_count,
            "funnel_executed_signal_count": executed_signal_count,
            "target_signal_count": target_signal_count,
            "target_to_signal_rate": pct(target_signal_count, target_count) if target_enabled else 0.0,
            "target_entry_count": target_entry_count,
            "open_count": open_count,
            "target_to_entry_rate": pct(target_entry_count, target_count) if target_enabled else 0.0,
            "signal_to_entry_rate": pct(executed_signal_count, signal_count),
        }

    def _build_backtest_reverse_samples(self, rows: list[dict], limit: int = 8) -> list[dict]:
        samples = []
        for row in rows[: max(0, int(limit or 0))]:
            samples.append(
                {
                    "symbol": str(row.get("symbol", "") or ""),
                    "direction": str(row.get("direction", "") or ""),
                    "action_type": str(row.get("action_type", "") or ""),
                    "score": round(float(row.get("score", 0) or 0), 4),
                    "strength": str(row.get("strength", "") or ""),
                    "status": str(row.get("status", "") or ""),
                    "target_state": str(row.get("target_state", "") or ""),
                    "us_time": str(row.get("us_time", "") or ""),
                    "triggered_signals": list(row.get("triggered_signals") or []),
                    "signal_id": str(row.get("signal_id", "") or ""),
                }
            )
        return samples

    def _build_backtest_audit_trail(
        self,
        request: dict,
        target_rows: list[dict],
        signal_rows: list[dict],
        trades: list[dict],
        reverse_rows: list[dict],
    ) -> dict:
        events: list[dict] = []
        timeline_limit = 800
        focus_limit = 240
        flow_limit = 240

        def coerce_float(value: Any, default: float = 0.0) -> float:
            try:
                return round(float(value), 4)
            except Exception:
                return default

        def coerce_int(value: Any, default: int = 0) -> int:
            try:
                return int(value or 0)
            except Exception:
                return default

        def clean_details(details: dict | None) -> dict:
            compact = {}
            for key, value in dict(details or {}).items():
                if value in (None, "", [], {}):
                    continue
                if isinstance(value, float):
                    compact[key] = round(value, 4)
                elif isinstance(value, (str, int, bool, list, dict)):
                    compact[key] = value
            return compact

        def event_date(event: dict) -> str:
            for field_name in ("date", "us_time"):
                value = str(event.get(field_name, "") or "")
                if len(value) >= 10:
                    return value[:10]
            bar_ms = int(event.get("bar_time_ms", 0) or 0)
            return ms_to_et(bar_ms).strftime("%Y-%m-%d") if bar_ms > 0 else ""

        def add_event(event: dict):
            bar_ms = int(event.get("bar_time_ms", 0) or 0)
            if bar_ms > 0:
                event["bar_time_ms"] = bar_ms
                event["us_time"] = str(event.get("us_time") or format_us_time(bar_ms))
                event["cn_time"] = str(event.get("cn_time") or format_cn_time(bar_ms))
            event["symbol"] = str(event.get("symbol", "") or "").strip().upper()
            event["date"] = event_date(event)
            event["direction"] = str(event.get("direction", "") or "").strip().lower()
            event["signal_id"] = str(event.get("signal_id", "") or "")
            event["reason"] = str(event.get("reason", "") or "")
            event["details"] = clean_details(event.get("details") if isinstance(event.get("details"), dict) else {})
            events.append(event)

        for row in target_rows or []:
            extra = self._parse_object(row.get("extra"))
            add_event(
                {
                    "event_type": "target_selected",
                    "stage": "target",
                    "status": str(row.get("status", "") or "selected"),
                    "symbol": row.get("symbol"),
                    "direction": str(row.get("direction_bias", "") or "").strip().lower(),
                    "bar_time_ms": int(row.get("bar_time_ms", extra.get("cutoff_ms", 0)) or 0),
                    "us_time": str(row.get("us_time", "") or extra.get("cutoff_us_time", "") or ""),
                    "cn_time": str(row.get("cn_time", "") or extra.get("cutoff_cn_time", "") or ""),
                    "date": str(row.get("date", "") or "")[:10],
                    "reason": str(row.get("scan_reason", "") or extra.get("scan_reason", "") or "target_selected"),
                    "details": {
                        "rank": coerce_int(row.get("rank"), 0),
                        "score": coerce_float(row.get("score"), 0.0),
                        "sd_admitted_at_ms": coerce_int(extra.get("sd_admitted_at_ms"), 0),
                        "sd_admitted_us_time": str(extra.get("sd_admitted_us_time", "") or ""),
                    },
                }
            )

        def add_signal_history_event(row: dict, history_event: dict, fallback_status: str):
            extra = self._parse_object(row.get("extra"))
            status = str(history_event.get("status") or fallback_status or row.get("status", "") or "").strip().lower()
            reason = str(history_event.get("reason") or extra.get("signal_status_reason") or status or "").strip()
            event_type = {
                "generated": "signal_generated",
                "pending": "signal_pending",
                "executed": "entry_filled",
                "skipped": "signal_skipped",
                "dropped": "signal_dropped",
            }.get(status, f"signal_{status or 'event'}")
            bar_ms = coerce_int(history_event.get("bar_time_ms"), coerce_int(row.get("bar_time_ms"), 0))
            details = {
                "entry": coerce_float(row.get("entry"), 0.0),
                "stop_loss": coerce_float(row.get("stop_loss"), 0.0),
                "take_profit": coerce_float(row.get("take_profit"), 0.0),
                "rr": str(row.get("rr", "") or ""),
                "shares": coerce_int(row.get("shares"), 0),
                "status_reason": reason,
                "confirm_ready_bar_ms": coerce_int(history_event.get("confirm_ready_bar_ms"), 0),
                "entry_limit_price": coerce_float(history_event.get("entry_limit_price"), 0.0),
                "portfolio_open_exposure": coerce_float(history_event.get("portfolio_open_exposure"), 0.0),
                "portfolio_reserved_exposure": coerce_float(history_event.get("portfolio_reserved_exposure"), 0.0),
            }
            add_event(
                {
                    "event_type": event_type,
                    "stage": "signal" if status in {"generated", "pending", "skipped", "dropped"} else "execution",
                    "status": status,
                    "symbol": row.get("symbol"),
                    "direction": row.get("direction"),
                    "signal_id": row.get("signal_id"),
                    "signal": row.get("signal"),
                    "bar_time_ms": bar_ms,
                    "us_time": str(history_event.get("us_time") or row.get("us_time", "") or ""),
                    "cn_time": str(history_event.get("cn_time") or row.get("cn_time", "") or ""),
                    "date": str(row.get("date") or row.get("us_time", "") or "")[:10],
                    "reason": reason,
                    "entry_price": coerce_float(history_event.get("entry_price"), 0.0),
                    "stop_loss": coerce_float(row.get("stop_loss"), 0.0),
                    "take_profit": coerce_float(row.get("take_profit"), 0.0),
                    "details": details,
                }
            )

        for row in signal_rows or []:
            extra = self._parse_object(row.get("extra"))
            history = extra.get("status_history")
            if isinstance(history, list) and history:
                for item in history:
                    if isinstance(item, dict):
                        add_signal_history_event(row, item, str(row.get("status", "") or ""))
            else:
                add_signal_history_event(row, {"status": "generated"}, "generated")
                final_status = str(row.get("status", "") or "").strip().lower()
                if final_status and final_status != "generated":
                    add_signal_history_event(row, {"status": final_status}, final_status)

        for trade in trades or []:
            extra = self._parse_object(trade.get("extra"))
            trade_index = coerce_int(trade.get("trade_index"), 0)
            add_event(
                {
                    "event_type": "trade_opened",
                    "stage": "execution",
                    "status": "opened",
                    "symbol": trade.get("symbol"),
                    "direction": trade.get("direction"),
                    "signal_id": trade.get("signal_id"),
                    "signal": trade.get("signal"),
                    "trade_index": trade_index,
                    "bar_time_ms": coerce_int(trade.get("entry_bar_ms"), 0),
                    "us_time": str(trade.get("entry_us_time", "") or ""),
                    "cn_time": str(trade.get("entry_cn_time", "") or ""),
                    "entry_price": coerce_float(trade.get("entry_price"), 0.0),
                    "shares": coerce_int(trade.get("shares"), 0),
                    "reason": str(trade.get("reason", "") or "entry_filled"),
                    "details": {
                        "signal_bar_ms": coerce_int(extra.get("signal_bar_ms"), 0),
                        "signal_us_time": str(extra.get("signal_us_time", "") or ""),
                        "entry_reference_price": coerce_float(extra.get("entry_reference_price"), 0.0),
                        "entry_limit_price": coerce_float(extra.get("entry_limit_price"), 0.0),
                        "entry_slippage_bps": coerce_float(extra.get("entry_slippage_bps"), 0.0),
                    },
                }
            )
            for adjustment in list(extra.get("risk_adjustments") or []):
                if not isinstance(adjustment, dict):
                    continue
                event_type = str(adjustment.get("event_type") or "risk_adjustment")
                add_event(
                    {
                        "event_type": event_type,
                        "stage": "risk",
                        "status": "adjusted",
                        "symbol": trade.get("symbol"),
                        "direction": trade.get("direction"),
                        "signal_id": trade.get("signal_id") or adjustment.get("signal_id"),
                        "signal": trade.get("signal"),
                        "trade_index": trade_index,
                        "bar_time_ms": coerce_int(adjustment.get("bar_time_ms"), 0),
                        "us_time": str(adjustment.get("us_time", "") or ""),
                        "cn_time": str(adjustment.get("cn_time", "") or ""),
                        "old_sl": coerce_float(adjustment.get("old_sl"), 0.0),
                        "new_sl": coerce_float(adjustment.get("new_sl"), 0.0),
                        "old_tp": coerce_float(adjustment.get("old_tp"), 0.0),
                        "new_tp": coerce_float(adjustment.get("new_tp"), 0.0),
                        "reason": str(adjustment.get("reason", "") or event_type),
                        "details": adjustment,
                    }
                )
            exit_reason = str(trade.get("exit_reason", "") or "closed")
            add_event(
                {
                    "event_type": "trade_closed",
                    "stage": "exit",
                    "status": exit_reason,
                    "symbol": trade.get("symbol"),
                    "direction": trade.get("direction"),
                    "signal_id": trade.get("signal_id"),
                    "signal": trade.get("signal"),
                    "trade_index": trade_index,
                    "bar_time_ms": coerce_int(trade.get("exit_bar_ms"), 0),
                    "us_time": str(trade.get("exit_us_time", "") or ""),
                    "cn_time": str(trade.get("exit_cn_time", "") or ""),
                    "entry_price": coerce_float(trade.get("entry_price"), 0.0),
                    "exit_price": coerce_float(trade.get("exit_price"), 0.0),
                    "stop_loss": coerce_float(extra.get("stop_price"), 0.0),
                    "take_profit": coerce_float(extra.get("target_price"), 0.0),
                    "pnl": coerce_float(trade.get("pnl"), 0.0),
                    "pnl_pct": coerce_float(trade.get("pnl_pct"), 0.0),
                    "shares": coerce_int(trade.get("shares"), 0),
                    "reason": exit_reason,
                    "details": {
                        "bars_held": coerce_int(trade.get("bars_held"), 0),
                        "initial_stop_loss": coerce_float(extra.get("initial_stop_loss"), 0.0),
                        "initial_take_profit": coerce_float(extra.get("initial_take_profit"), 0.0),
                        "mfe": coerce_float(extra.get("mfe"), 0.0),
                        "mae": coerce_float(extra.get("mae"), 0.0),
                        "total_commission": coerce_float(extra.get("total_commission"), 0.0),
                        "estimated_slippage_cost": coerce_float(extra.get("estimated_slippage_cost"), 0.0),
                    },
                }
            )

        for row in reverse_rows or []:
            extra = self._parse_object(row.get("extra"))
            add_event(
                {
                    "event_type": "reverse_action",
                    "stage": "special",
                    "status": str(row.get("action_type", "") or ""),
                    "symbol": row.get("symbol"),
                    "direction": row.get("direction"),
                    "signal_id": row.get("signal_id") or extra.get("signal_id"),
                    "bar_time_ms": coerce_int(row.get("bar_time_ms"), 0),
                    "us_time": str(row.get("us_time", "") or ""),
                    "cn_time": str(row.get("cn_time", "") or ""),
                    "reason": str(row.get("reason", "") or ""),
                    "old_sl": coerce_float(extra.get("old_sl"), 0.0),
                    "new_sl": coerce_float(extra.get("new_sl"), 0.0),
                    "old_tp": coerce_float(extra.get("old_tp"), 0.0),
                    "new_tp": coerce_float(extra.get("new_tp"), 0.0),
                    "details": {
                        "reverse_kind": str(row.get("reverse_kind", "") or ""),
                        "source": str(row.get("source", "") or ""),
                        "target_state": str(row.get("target_state", "") or ""),
                        "strength": str(row.get("strength", "") or ""),
                        "score": coerce_float(row.get("score"), 0.0),
                        "triggered_signals": list(row.get("triggered_signals") or []),
                        "origin_signal_id": str(row.get("origin_signal_id", "") or extra.get("origin_signal_id", "") or ""),
                        "new_direction": str(extra.get("new_direction", "") or ""),
                    },
                }
            )

        priority = {
            "target": 10,
            "signal": 20,
            "execution": 30,
            "risk": 40,
            "special": 50,
            "exit": 60,
        }
        events.sort(
            key=lambda item: (
                int(item.get("bar_time_ms", 0) or 0) or 9999999999999,
                str(item.get("symbol", "") or ""),
                priority.get(str(item.get("stage", "") or ""), 99),
                str(item.get("event_type", "") or ""),
            )
        )

        daily: dict[str, dict] = {}
        flows: dict[tuple[str, str], dict] = {}
        for event in events:
            date_text = str(event.get("date", "") or "")
            symbol = str(event.get("symbol", "") or "")
            if not date_text:
                continue
            day = daily.setdefault(
                date_text,
                {
                    "date": date_text,
                    "target_symbols": set(),
                    "signal_symbols": set(),
                    "executed_symbols": set(),
                    "trade_symbols": set(),
                    "target_count": 0,
                    "signal_count": 0,
                    "executed_signal_count": 0,
                    "trade_count": 0,
                    "take_profit_count": 0,
                    "stop_loss_count": 0,
                    "risk_adjustment_count": 0,
                    "special_event_count": 0,
                },
            )
            event_type = str(event.get("event_type", "") or "")
            status = str(event.get("status", "") or "")
            stage = str(event.get("stage", "") or "")
            if event_type == "target_selected":
                day["target_count"] += 1
                if symbol:
                    day["target_symbols"].add(symbol)
            if event_type == "signal_generated":
                day["signal_count"] += 1
                if symbol:
                    day["signal_symbols"].add(symbol)
            if event_type == "entry_filled":
                day["executed_signal_count"] += 1
                if symbol:
                    day["executed_symbols"].add(symbol)
            if event_type == "trade_closed":
                day["trade_count"] += 1
                if symbol:
                    day["trade_symbols"].add(symbol)
                if status == "take_profit":
                    day["take_profit_count"] += 1
                if status == "stop_loss":
                    day["stop_loss_count"] += 1
            if stage == "risk":
                day["risk_adjustment_count"] += 1
            if stage == "special":
                day["special_event_count"] += 1
            if symbol:
                flow = flows.setdefault(
                    (date_text, symbol),
                    {
                        "date": date_text,
                        "symbol": symbol,
                        "event_count": 0,
                        "targeted": False,
                        "signal_count": 0,
                        "executed_signal_count": 0,
                        "trade_count": 0,
                        "risk_adjustment_count": 0,
                        "special_event_count": 0,
                        "events": [],
                    },
                )
                flow["event_count"] += 1
                flow["targeted"] = bool(flow["targeted"] or event_type == "target_selected")
                if event_type == "signal_generated":
                    flow["signal_count"] += 1
                if event_type == "entry_filled":
                    flow["executed_signal_count"] += 1
                if event_type == "trade_closed":
                    flow["trade_count"] += 1
                if stage == "risk":
                    flow["risk_adjustment_count"] += 1
                if stage == "special":
                    flow["special_event_count"] += 1
                if len(flow["events"]) < 24:
                    flow["events"].append(event)

        daily_summary = []
        for day in sorted(daily.values(), key=lambda item: str(item.get("date", ""))):
            for key in ("target_symbols", "signal_symbols", "executed_symbols", "trade_symbols"):
                day[key] = sorted(day[key])
            daily_summary.append(day)

        requested_focus_date = str(request.get("date_to") or "")[:10]
        available_dates = [str(item.get("date") or "") for item in daily_summary if item.get("date")]
        focus_date = requested_focus_date if requested_focus_date in available_dates else (available_dates[-1] if available_dates else requested_focus_date)
        focus_events = [event for event in events if str(event.get("date", "") or "") == focus_date]
        focus_day = next((item for item in daily_summary if item.get("date") == focus_date), None) or {"date": focus_date}
        focus_symbols = list(focus_day.get("target_symbols") or []) or sorted(
            {
                str(event.get("symbol") or "")
                for event in focus_events
                if str(event.get("symbol") or "")
            }
        )
        symbol_day_flows = sorted(
            flows.values(),
            key=lambda item: (
                str(item.get("date", "") or ""),
                0 if item.get("targeted") else 1,
                str(item.get("symbol", "") or ""),
            ),
        )[:flow_limit]
        event_type_counts = self._count_values(events, "event_type")
        stage_counts = self._count_values(events, "stage")

        return {
            "enabled": True,
            "date_from": str(request.get("date_from") or ""),
            "date_to": str(request.get("date_to") or ""),
            "focus_date": focus_date,
            "focus_symbols": focus_symbols,
            "event_count": len(events),
            "event_type_counts": event_type_counts,
            "stage_counts": stage_counts,
            "timeline_limit": timeline_limit,
            "timeline_truncated": len(events) > timeline_limit,
            "timeline": events[:timeline_limit],
            "focus_day": {
                **focus_day,
                "timeline": focus_events[:focus_limit],
                "timeline_truncated": len(focus_events) > focus_limit,
            },
            "daily_summary": daily_summary,
            "symbol_day_flows": symbol_day_flows,
            "symbol_day_flow_truncated": len(flows) > flow_limit,
        }

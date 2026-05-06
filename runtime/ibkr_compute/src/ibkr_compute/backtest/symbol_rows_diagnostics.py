from __future__ import annotations

from .runtime_support import *


class BacktestSymbolRowsDiagnosticsMixin:
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

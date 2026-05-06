from __future__ import annotations

from .runtime_support import *


class BacktestTvParityMixin:
    def _normalize_tv_indicator_row(self, row: dict) -> dict:
        extra = self._parse_object(row.get("extra"))
        return {
            "symbol": str(row.get("symbol", "") or "").upper(),
            "interval": str(row.get("interval", "") or ""),
            "bar_time_ms": int(row.get("bar_time_ms", 0) or 0),
            "us_time": str(row.get("us_time", "") or ""),
            "cn_time": str(row.get("cn_time", "") or ""),
            "extra": extra,
        }

    def _normalize_tv_signal_row(self, row: dict) -> dict:
        extra = self._parse_object(row.get("extra"))
        return {
            "symbol": str(row.get("symbol", "") or "").upper(),
            "signal_id": str(row.get("signal_id", "") or ""),
            "signal": str(row.get("signal", "") or ""),
            "direction": str(row.get("direction", "") or ""),
            "entry": float(row.get("entry", 0) or 0),
            "stop_loss": float(row.get("stop_loss", 0) or 0),
            "take_profit": float(row.get("take_profit", 0) or 0),
            "rr": self._parse_rr_value(
                row.get("rr"),
                entry=row.get("entry"),
                stop_loss=row.get("stop_loss"),
                take_profit=row.get("take_profit"),
            ),
            "shares": int(row.get("shares", 0) or 0),
            "interval": str(row.get("interval", "") or ""),
            "bar_time_ms": int(row.get("bar_time_ms", 0) or 0),
            "us_time": str(row.get("us_time", "") or ""),
            "cn_time": str(row.get("cn_time", "") or ""),
            "reason": str(row.get("reason", "") or ""),
            "extra": extra,
        }

    def _load_tv_reference(
        self,
        symbol: str,
        source_environment: str,
        date_from: str,
        date_to: str,
        include_signals: bool = True,
    ) -> dict:
        reference = {"indicator_map": {}, "signal_map": {}, "signal_bar_map": {}, "error": ""}
        if not self.pb:
            reference["error"] = "pb_client_unavailable"
            return reference
        try:
            start_ms, end_ms = self._date_to_ms_range(date_from, date_to)
            chart_tf = interval_to_chart_tf("5m")
            indicator_rows = self.pb.get_all_records(
                TV_INDICATOR_COLLECTION,
                filter=(
                    f'symbol = "{symbol}" && interval = "{chart_tf}" && environment = "{source_environment}" '
                    f"&& bar_time_ms >= {start_ms} && bar_time_ms <= {end_ms}"
                ),
                sort="bar_time_ms",
                max_pages=DEFAULT_MAX_PAGES,
            )
            for row in indicator_rows:
                normalized = self._normalize_tv_indicator_row(row)
                if normalized["bar_time_ms"] > 0:
                    reference["indicator_map"][normalized["bar_time_ms"]] = normalized
            if include_signals:
                signal_rows = self.pb.get_all_records(
                    TV_SIGNAL_COLLECTION,
                    filter=(
                        f'symbol = "{symbol}" && interval = "{chart_tf}" && environment = "{source_environment}" '
                        f"&& bar_time_ms >= {start_ms} && bar_time_ms <= {end_ms}"
                    ),
                    sort="bar_time_ms",
                    max_pages=400,
                )
                for row in signal_rows:
                    normalized = self._normalize_tv_signal_row(row)
                    signal_id = normalized["signal_id"]
                    if signal_id:
                        reference["signal_map"][signal_id] = normalized
                    bar_ms = normalized["bar_time_ms"]
                    if bar_ms > 0:
                        reference["signal_bar_map"].setdefault(bar_ms, []).append(normalized)
        except Exception as exc:
            reference["error"] = str(exc)[:300]
        return reference

    def _init_symbol_tv_parity(self, symbol: str, reference: dict, compare_tv_signals: bool = True) -> dict:
        return {
            "symbol": symbol,
            "reference_error": str((reference or {}).get("error") or ""),
            "signal_compare_enabled": bool(compare_tv_signals),
            "indicators": {
                "generated_count": 0,
                "tv_count": len((reference or {}).get("indicator_map", {})),
                "matched_count": 0,
                "mismatch_count": 0,
                "missing_in_tv_count": 0,
                "missing_in_backtest_count": 0,
                "field_mismatch_count": 0,
                "mismatch_samples": [],
                "missing_in_tv_samples": [],
                "missing_in_backtest_samples": [],
                "_seen_keys": set(),
            },
            "signals": {
                "generated_count": 0,
                "tv_count": len((reference or {}).get("signal_map", {})) if compare_tv_signals else 0,
                "matched_count": 0,
                "mismatch_count": 0,
                "missing_in_tv_count": 0,
                "missing_in_backtest_count": 0,
                "field_mismatch_count": 0,
                "mismatch_samples": [],
                "missing_in_tv_samples": [],
                "missing_in_backtest_samples": [],
                "_seen_keys": set(),
            },
            "_tv_indicator_map": dict((reference or {}).get("indicator_map", {})),
            "_tv_signal_map": dict((reference or {}).get("signal_map", {})),
            "_tv_signal_bar_map": dict((reference or {}).get("signal_bar_map", {})),
        }

    def _append_tv_sample(self, bucket: list, payload: dict):
        if len(bucket) < TV_COMPARE_SAMPLE_LIMIT:
            bucket.append(payload)

    def _normalize_compare_time(self, value: Any) -> str:
        text = str(value or "").strip()
        return text[:16] if len(text) >= 16 else text

    def _coerce_numeric(self, value: Any) -> float | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        text = str(value or "").strip()
        if not text:
            return None
        try:
            return float(text)
        except Exception:
            return None

    def _parse_rr_value(self, raw_value: Any, entry: Any = 0, stop_loss: Any = 0, take_profit: Any = 0) -> float | None:
        numeric = self._coerce_numeric(raw_value)
        if numeric is not None:
            return numeric

        text = str(raw_value or "").strip().replace("：", ":")
        if text:
            parts = text.split(":")
            if len(parts) == 2:
                left = self._coerce_numeric(parts[0])
                right = self._coerce_numeric(parts[1])
                if left is not None and right not in (None, 0):
                    return left / right

        entry_price = self._coerce_numeric(entry)
        stop_price = self._coerce_numeric(stop_loss)
        take_price = self._coerce_numeric(take_profit)
        if entry_price is None or stop_price is None or take_price is None:
            return None
        risk = abs(entry_price - stop_price)
        reward = abs(take_price - entry_price)
        if risk <= 0:
            return None
        return reward / risk

    def _compare_values(self, field: str, left: Any, right: Any) -> tuple[bool, Any, Any]:
        if left in (None, "") and right in (None, ""):
            return True, left, right
        if field in {"us_time", "cn_time"}:
            left_norm = self._normalize_compare_time(left)
            right_norm = self._normalize_compare_time(right)
            return left_norm == right_norm, left_norm, right_norm
        if field == "rr":
            left_norm = self._parse_rr_value(left)
            right_norm = self._parse_rr_value(right)
            if left_norm is None and right_norm is None:
                return True, left_norm, right_norm
            return round(float(left_norm or 0), 4) == round(float(right_norm or 0), 4), left_norm, right_norm
        if isinstance(left, bool) or isinstance(right, bool):
            left_norm = bool(left)
            right_norm = bool(right)
            return left_norm == right_norm, left_norm, right_norm

        left_num = self._coerce_numeric(left)
        right_num = self._coerce_numeric(right)
        if left_num is not None and right_num is not None:
            if field in {"bar_time_ms", "shares", "volume"}:
                left_norm = int(round(left_num))
                right_norm = int(round(right_num))
            else:
                precision = 2 if field in {"entry", "stop_loss", "take_profit", "close", "atr_pct", "day_change_pct", "prev_close_change_pct", "change_7d", "sl_dist_pct"} else 4
                left_norm = round(left_num, precision)
                right_norm = round(right_num, precision)
            return left_norm == right_norm, left_norm, right_norm

        left_text = str(left or "").strip()
        right_text = str(right or "").strip()
        return left_text == right_text, left_text, right_text

    def _compare_payload_fields(self, generated: dict, reference: dict, core_fields: tuple[str, ...], extra_fields: tuple[str, ...]) -> list[dict]:
        mismatches = []
        generated_extra = self._parse_object(generated.get("extra"))
        reference_extra = self._parse_object(reference.get("extra"))

        for field in core_fields:
            matched, left_norm, right_norm = self._compare_values(field, generated.get(field), reference.get(field))
            if not matched:
                mismatches.append({"field": field, "generated": left_norm, "tv": right_norm})

        for field in extra_fields:
            matched, left_norm, right_norm = self._compare_values(field, generated_extra.get(field), reference_extra.get(field))
            if not matched:
                mismatches.append({"field": f"extra.{field}", "generated": left_norm, "tv": right_norm})
        return mismatches

    def _compare_generated_indicator(self, report: dict, generated: dict) -> dict:
        section = report["indicators"]
        key = int(generated.get("bar_time_ms", 0) or 0)
        section["generated_count"] += 1
        section["_seen_keys"].add(key)

        reference = report["_tv_indicator_map"].get(key)
        if not reference:
            section["missing_in_tv_count"] += 1
            self._append_tv_sample(
                section["missing_in_tv_samples"],
                {
                    "symbol": report["symbol"],
                    "bar_time_ms": key,
                    "us_time": generated.get("us_time", ""),
                },
            )
            return {"status": "missing_in_tv", "field_count": 0, "mismatches": []}

        mismatches = self._compare_payload_fields(
            generated,
            reference,
            ("bar_time_ms", "us_time", "cn_time", "interval"),
            TV_INDICATOR_EXTRA_FIELDS,
        )
        if mismatches:
            section["mismatch_count"] += 1
            section["field_mismatch_count"] += len(mismatches)
            self._append_tv_sample(
                section["mismatch_samples"],
                {
                    "symbol": report["symbol"],
                    "bar_time_ms": key,
                    "us_time": generated.get("us_time", ""),
                    "fields": mismatches[:10],
                },
            )
            return {"status": "mismatch", "field_count": len(mismatches), "mismatches": mismatches[:10]}

        section["matched_count"] += 1
        return {"status": "matched", "field_count": 0, "mismatches": []}

    def _compare_generated_signal(self, report: dict, generated: dict):
        if not bool(report.get("signal_compare_enabled", True)):
            return
        section = report["signals"]
        signal_id = str(generated.get("signal_id", "") or "")
        bar_time_ms = int(generated.get("bar_time_ms", 0) or 0)
        section["generated_count"] += 1
        if signal_id:
            section["_seen_keys"].add(signal_id)

        reference = report["_tv_signal_map"].get(signal_id)
        if not reference:
            section["missing_in_tv_count"] += 1
            self._append_tv_sample(
                section["missing_in_tv_samples"],
                {
                    "symbol": report["symbol"],
                    "signal_id": signal_id,
                    "bar_time_ms": bar_time_ms,
                    "us_time": generated.get("us_time", ""),
                    "tv_candidates_at_bar": [item.get("signal_id", "") for item in report["_tv_signal_bar_map"].get(bar_time_ms, [])][:5],
                },
            )
            return

        mismatches = self._compare_payload_fields(
            generated,
            reference,
            TV_SIGNAL_CORE_FIELDS,
            TV_SIGNAL_EXTRA_FIELDS,
        )
        if mismatches:
            section["mismatch_count"] += 1
            section["field_mismatch_count"] += len(mismatches)
            self._append_tv_sample(
                section["mismatch_samples"],
                {
                    "symbol": report["symbol"],
                    "signal_id": signal_id,
                    "bar_time_ms": bar_time_ms,
                    "fields": mismatches[:10],
                },
            )
            return

        section["matched_count"] += 1

    def _finalize_symbol_tv_parity(self, report: dict):
        for key, reference in report["_tv_indicator_map"].items():
            if key in report["indicators"]["_seen_keys"]:
                continue
            report["indicators"]["missing_in_backtest_count"] += 1
            self._append_tv_sample(
                report["indicators"]["missing_in_backtest_samples"],
                {
                    "symbol": report["symbol"],
                    "bar_time_ms": key,
                    "us_time": reference.get("us_time", ""),
                },
            )

        if bool(report.get("signal_compare_enabled", True)):
            for key, reference in report["_tv_signal_map"].items():
                if key in report["signals"]["_seen_keys"]:
                    continue
                report["signals"]["missing_in_backtest_count"] += 1
                self._append_tv_sample(
                    report["signals"]["missing_in_backtest_samples"],
                    {
                        "symbol": report["symbol"],
                        "signal_id": key,
                        "bar_time_ms": reference.get("bar_time_ms", 0),
                        "us_time": reference.get("us_time", ""),
                    },
                )

        for section_name in ("indicators", "signals"):
            section = report[section_name]
            denominator = max(section["generated_count"], section["tv_count"], 1)
            section["match_rate"] = round((section["matched_count"] / denominator) * 100.0, 2) if denominator else 100.0
            section.pop("_seen_keys", None)

        if not bool(report.get("signal_compare_enabled", True)):
            report["signals"].update(
                {
                    "generated_count": 0,
                    "tv_count": 0,
                    "matched_count": 0,
                    "mismatch_count": 0,
                    "missing_in_tv_count": 0,
                    "missing_in_backtest_count": 0,
                    "field_mismatch_count": 0,
                    "mismatch_samples": [],
                    "missing_in_tv_samples": [],
                    "missing_in_backtest_samples": [],
                    "match_rate": 0.0,
                    "status": "disabled",
                }
            )

        if report["reference_error"]:
            report["status"] = "error"
        elif report["indicators"]["tv_count"] == 0 and (not bool(report.get("signal_compare_enabled", True)) or report["signals"]["tv_count"] == 0):
            report["status"] = "no_reference"
        elif bool(report.get("signal_compare_enabled", True)) and (
            report["signals"]["mismatch_count"]
            or report["signals"]["missing_in_tv_count"]
            or report["signals"]["missing_in_backtest_count"]
        ):
            report["status"] = "fail"
        elif report["indicators"]["mismatch_count"] or report["indicators"]["missing_in_tv_count"] or report["indicators"]["missing_in_backtest_count"]:
            report["status"] = "warn"
        else:
            report["status"] = "pass"

        report.pop("_tv_indicator_map", None)
        report.pop("_tv_signal_map", None)
        report.pop("_tv_signal_bar_map", None)

    def _finalize_tv_parity_report(self, request: dict, symbol_reports: list[dict]) -> dict:
        if not self._should_compare_with_tv(request):
            return {
                "enabled": False,
                "status": "disabled",
                "summary": {"status": "disabled", "enabled": False},
                "symbols": [],
            }

        reports = [item for item in symbol_reports if item]
        reports.sort(key=lambda item: str(item.get("symbol", "")))

        indicator_totals = {
            "generated_count": 0,
            "tv_count": 0,
            "matched_count": 0,
            "mismatch_count": 0,
            "missing_in_tv_count": 0,
            "missing_in_backtest_count": 0,
            "field_mismatch_count": 0,
        }
        signal_totals = {
            "generated_count": 0,
            "tv_count": 0,
            "matched_count": 0,
            "mismatch_count": 0,
            "missing_in_tv_count": 0,
            "missing_in_backtest_count": 0,
            "field_mismatch_count": 0,
        }
        compare_tv_signals = self._should_compare_tv_signals(request)

        for report in reports:
            for key in indicator_totals:
                indicator_totals[key] += int(report.get("indicators", {}).get(key, 0) or 0)
                signal_totals[key] += int(report.get("signals", {}).get(key, 0) or 0)

        indicator_denominator = max(indicator_totals["generated_count"], indicator_totals["tv_count"], 1)
        signal_denominator = max(signal_totals["generated_count"], signal_totals["tv_count"], 1)
        indicator_match_rate = round((indicator_totals["matched_count"] / indicator_denominator) * 100.0, 2) if indicator_denominator else 100.0
        signal_match_rate = round((signal_totals["matched_count"] / signal_denominator) * 100.0, 2) if signal_denominator else 100.0
        symbols_with_reference = len([item for item in reports if item.get("status") not in {"no_reference", "error"}])

        if not reports or (indicator_totals["tv_count"] == 0 and (not compare_tv_signals or signal_totals["tv_count"] == 0)):
            status = "no_reference"
        elif any(item.get("status") == "error" for item in reports):
            status = "error"
        elif compare_tv_signals and (
            signal_totals["mismatch_count"]
            or signal_totals["missing_in_tv_count"]
            or signal_totals["missing_in_backtest_count"]
        ):
            status = "fail"
        elif indicator_totals["mismatch_count"] or indicator_totals["missing_in_tv_count"] or indicator_totals["missing_in_backtest_count"]:
            status = "warn"
        else:
            status = "pass"

        summary = {
            "enabled": True,
            "status": status,
            "symbol_count": len(reports),
            "symbols_with_reference": symbols_with_reference,
            "signal_compare_enabled": compare_tv_signals,
            "indicator_match_rate": indicator_match_rate,
            "signal_match_rate": signal_match_rate,
            "indicator_generated_count": indicator_totals["generated_count"],
            "indicator_tv_count": indicator_totals["tv_count"],
            "indicator_matched_count": indicator_totals["matched_count"],
            "indicator_mismatch_count": indicator_totals["mismatch_count"],
            "indicator_missing_in_tv_count": indicator_totals["missing_in_tv_count"],
            "indicator_missing_in_backtest_count": indicator_totals["missing_in_backtest_count"],
            "signal_generated_count": signal_totals["generated_count"],
            "signal_tv_count": signal_totals["tv_count"],
            "signal_matched_count": signal_totals["matched_count"],
            "signal_mismatch_count": signal_totals["mismatch_count"],
            "signal_missing_in_tv_count": signal_totals["missing_in_tv_count"],
            "signal_missing_in_backtest_count": signal_totals["missing_in_backtest_count"],
        }
        return {
            "enabled": True,
            "status": status,
            "signal_compare_enabled": compare_tv_signals,
            "source_environment": request.get("source_environment") or "",
            "session_mode": request.get("session_mode") or "",
            "interval": interval_to_chart_tf("5m"),
            "symbols": reports,
            "summary": summary,
        }

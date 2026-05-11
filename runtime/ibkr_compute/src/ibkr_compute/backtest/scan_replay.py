from __future__ import annotations

import hashlib

from ibkr_compute.universe.dynamic_admission import evaluate_dynamic_admission, normalize_admission_bool

from .runtime_support import *
from .watchlist_universe import merge_trade_watchlist_rows, request_excluded_symbols


DAILY_SELECTION_CACHE_ALGORITHM_VERSION = "daily_scan_replay_live_sd_v4"


class BacktestScanReplayMixin:
    def _parse_pb_datetime(self, raw_value: Any) -> Optional[datetime]:
        text = str(raw_value or "").strip()
        if not text:
            return None
        normalized = text.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except Exception:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(ET)

    def _load_scan_universe(self, request: dict, as_of_date: str = "") -> list[dict]:
        excluded = request_excluded_symbols(request)
        requested_symbols = list(request.get("symbols") or [])
        if requested_symbols:
            return [
                {"symbol": symbol, "exchange": "", "environment": request.get("source_environment", "live")}
                for symbol in requested_symbols
                if str(symbol or "").strip().upper() not in excluded
            ]

        rows = self.pb.get_all_records(
            "watchlist",
            sort="symbol",
            max_pages=50,
        )
        return merge_trade_watchlist_rows(rows, request, as_of_date=as_of_date)

    def _load_trading_dates(self, request: dict) -> list[str]:
        start_ms, end_ms = self._date_to_ms_range(request["date_from"], request["date_to"])
        benchmark_symbol = str(request.get("benchmark_symbol") or "").strip().upper() or "SPY"
        dates = []
        seen = set()
        for interval, max_pages in (("1d", 24), ("5m", 240)):
            rows = self.pb.get_all_records(
                "ibkr_bars",
                filter=(
                    f'symbol = "{benchmark_symbol}" && interval = "{interval}" && environment = "{request["source_environment"]}" '
                    f"&& bar_time_ms >= {start_ms} && bar_time_ms <= {end_ms}"
                ),
                sort="bar_time_ms",
                max_pages=max_pages,
            )
            for row in rows:
                bar_ms = int(row.get("bar_time_ms", 0) or 0)
                if bar_ms <= 0:
                    continue
                date_text = ms_to_et(bar_ms).strftime("%Y-%m-%d")
                if date_text in seen:
                    continue
                seen.add(date_text)
                dates.append(date_text)
        if dates:
            dates.sort()
            return dates

        start = datetime.strptime(request["date_from"], "%Y-%m-%d").replace(tzinfo=ET)
        end = datetime.strptime(request["date_to"], "%Y-%m-%d").replace(tzinfo=ET)
        fallback = []
        current = start
        while current <= end:
            if current.weekday() < 5:
                fallback.append(current.strftime("%Y-%m-%d"))
            current += timedelta(days=1)
        return fallback

    def _build_scan_cutoff_ms(self, date_text: str, cutoff_time: str) -> int:
        hour_text, minute_text = str(cutoff_time or DEFAULT_SCAN_CUTOFF_TIME).split(":", 1)
        cutoff_dt = datetime.strptime(date_text, "%Y-%m-%d").replace(
            tzinfo=ET,
            hour=int(hour_text),
            minute=int(minute_text),
            second=0,
            microsecond=0,
        )
        return int(cutoff_dt.timestamp() * 1000)

    def _build_trade_date_time_ms(self, date_text: str, time_text: str, default_time: str) -> int:
        return self._build_scan_cutoff_ms(date_text, request_utils.normalize_hhmm(time_text, default_time))

    def _historical_sd_window_max_bars(self, request: dict) -> int:
        params = dict((request.get("params") or {}).get("strategy_params") or DEFAULT_PARAMS)
        try:
            return max(0, int(params.get("signal_window_max_bars", DEFAULT_PARAMS.get("signal_window_max_bars", 12)) or 0))
        except Exception:
            return int(DEFAULT_PARAMS.get("signal_window_max_bars", 12) or 12)

    def _effective_indicator_warmup_bars(self, request: dict, key: str = "warmup_bars") -> int:
        params = dict((request.get("params") or {}).get("strategy_params") or DEFAULT_PARAMS)
        requested = int(request.get(key, request.get("warmup_bars", BACKTEST_WARMUP_BARS)) or BACKTEST_WARMUP_BARS)
        signal_window_extra = max(20, self._historical_sd_window_max_bars(request) + 8)
        return max(
            BACKTEST_WARMUP_BARS,
            requested,
            int(indicator_ready_bar_count(params) or 0) + signal_window_extra,
        )

    def _daily_selection_cache_mode(self, request: dict) -> str:
        mode = str(request.get("daily_selection_cache_mode") or "use_or_build").strip().lower()
        return mode if mode in {"use_or_build", "read_only", "bypass"} else "use_or_build"

    def _daily_selection_cache_enabled(self, request: dict) -> bool:
        if not self.pb or self._daily_selection_cache_mode(request) == "bypass":
            return False
        default_enabled = bool(
            request.get("symbol_source") == "daily_scan_replay"
            and request.get("execution_model") == "portfolio_stream"
            and request.get("daily_selected_only")
        )
        return request_utils.normalize_bool(request.get("daily_selection_cache_enabled"), default_enabled)

    def _daily_selection_sd_mode(self, request: dict) -> str:
        mode = str(request.get("daily_selection_sd_mode") or "hard").strip().lower()
        return mode if mode in {"hard", "rank", "off"} else "hard"

    def _normalize_daily_selection_cache_payload(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {str(key): self._normalize_daily_selection_cache_payload(value[key]) for key in sorted(value.keys())}
        if isinstance(value, (list, tuple, set)):
            return [self._normalize_daily_selection_cache_payload(item) for item in list(value)]
        if isinstance(value, float):
            return round(value, 10)
        return value

    def _hash_daily_selection_cache_payload(self, payload: Any, prefix: str = "") -> str:
        normalized = self._normalize_daily_selection_cache_payload(payload)
        encoded = json.dumps(normalized, sort_keys=True, separators=(",", ":"), default=str)
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        return f"{prefix}{digest[:32]}" if prefix else digest

    def _build_daily_selection_cache_base_fingerprint(self, request: dict, scan_settings: dict) -> dict:
        strategy_params = dict((request.get("params") or {}).get("strategy_params") or DEFAULT_PARAMS)
        strategy_payload = {
            "strategy_tag": str(request.get("strategy_tag") or (request.get("params") or {}).get("strategy_tag") or ""),
            "strategy_params": strategy_params,
        }
        scan_settings_payload = dict(scan_settings or {})
        request_payload = {
            "algorithm_version": DAILY_SELECTION_CACHE_ALGORITHM_VERSION,
            "source_environment": str(request.get("source_environment") or "live"),
            "symbol_source": str(request.get("symbol_source") or ""),
            "symbols": sorted({str(symbol or "").strip().upper() for symbol in list(request.get("symbols") or []) if str(symbol or "").strip()}),
            "exclude_symbols": sorted(request_excluded_symbols(request)),
            "exclude_market_monitors": bool(request.get("exclude_market_monitors", True)),
            "session_mode": str(request.get("session_mode") or "extended"),
            "scan_session_mode": str(request.get("scan_session_mode") or "extended"),
            "premarket_cutoff_time": str(request.get("premarket_cutoff_time") or DEFAULT_SCAN_CUTOFF_TIME),
            "warmup_bars": int(request.get("warmup_bars", BACKTEST_WARMUP_BARS) or BACKTEST_WARMUP_BARS),
            "effective_warmup_bars": self._effective_indicator_warmup_bars(request, "warmup_bars"),
            "scan_warmup_bars": int(request.get("scan_warmup_bars", BACKTEST_WARMUP_BARS) or BACKTEST_WARMUP_BARS),
            "effective_scan_warmup_bars": self._effective_indicator_warmup_bars(request, "scan_warmup_bars"),
            "max_symbols": int(request.get("max_symbols", DEFAULT_MAX_SYMBOLS) or DEFAULT_MAX_SYMBOLS),
            "daily_selected_only": bool(request.get("daily_selected_only")),
            "daily_selection_require_sd_trigger": bool(request.get("daily_selection_require_sd_trigger")),
            "daily_selection_reuse_live_admission": bool(request.get("daily_selection_reuse_live_admission")),
            "daily_selection_sd_mode": self._daily_selection_sd_mode(request),
            "daily_selection_candidate_limit": int(request.get("daily_selection_candidate_limit", 0) or 0),
            "trade_window_start_time": str(request.get("trade_window_start_time") or DEFAULT_PORTFOLIO_TRADE_WINDOW_START),
            "trade_window_end_time": str(request.get("trade_window_end_time") or DEFAULT_PORTFOLIO_TRADE_WINDOW_END),
            "scan_intervals": list(SCAN_INTERVALS),
            "strategy": strategy_payload,
            "scan_settings": scan_settings_payload,
        }
        strategy_hash = self._hash_daily_selection_cache_payload(strategy_payload)
        scan_settings_hash = self._hash_daily_selection_cache_payload(scan_settings_payload)
        request_hash = self._hash_daily_selection_cache_payload(request_payload)
        return {
            "algorithm_version": DAILY_SELECTION_CACHE_ALGORITHM_VERSION,
            "cache_key": self._hash_daily_selection_cache_payload(request_payload, prefix="ds_"),
            "request_hash": request_hash,
            "scan_settings_hash": scan_settings_hash,
            "strategy_hash": strategy_hash,
            "fingerprint_extra": request_payload,
        }

    def _build_daily_selection_universe_hash(self, universe_rows: list[dict]) -> str:
        universe_payload = []
        for row in universe_rows or []:
            symbol = str((row or {}).get("symbol", "") or "").strip().upper()
            if not symbol:
                continue
            universe_payload.append(
                {
                    "symbol": symbol,
                    "exchange": str((row or {}).get("exchange", "") or "").strip().upper(),
                    "environment": str((row or {}).get("environment", "") or "").strip().lower(),
                    "symbol_role": str((row or {}).get("symbol_role", "") or "").strip().lower(),
                    "manual_member": bool((row or {}).get("manual_member", False)),
                }
            )
        universe_payload.sort(key=lambda item: (item["symbol"], item["environment"], item["exchange"]))
        return self._hash_daily_selection_cache_payload(universe_payload)

    def _build_daily_selection_input_fingerprint(
        self,
        trade_date: str,
        universe_rows: list[dict],
        request: dict,
    ) -> dict:
        symbols = sorted(
            {
                str((row or {}).get("symbol", "") or "").strip().upper()
                for row in universe_rows or []
                if str((row or {}).get("symbol", "") or "").strip()
            }
        )
        if not symbols:
            return {"usable": True, "hash": self._hash_daily_selection_cache_payload({"symbols": []}), "reason": "empty_universe", "row_count": 0}
        db_path = str(runtime_backtest_sqlite_path() or "").strip()
        if not db_path or not os.path.exists(db_path):
            return {"usable": False, "hash": "", "reason": "sqlite_unavailable", "row_count": 0}
        session_mode = str(request.get("scan_session_mode") or request.get("session_mode") or "extended").strip().lower() or "extended"
        try:
            with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10) as conn:
                conn.row_factory = sqlite3.Row
                coverage_rows = load_daily_coverage_rows(
                    conn,
                    symbols=symbols,
                    environment=str(request.get("source_environment") or "live"),
                    date_from=trade_date,
                    date_to=trade_date,
                    interval="5m",
                    session_mode=session_mode,
                )
        except Exception as exc:
            return {"usable": False, "hash": "", "reason": f"coverage_query_failed:{str(exc)[:120]}", "row_count": 0}

        return self._build_daily_selection_input_fingerprint_from_coverage(
            trade_date,
            symbols,
            coverage_rows,
            request,
            session_mode,
        )

    def _build_daily_selection_input_fingerprint_from_coverage(
        self,
        trade_date: str,
        symbols: list[str],
        coverage_rows: list[dict],
        request: dict,
        session_mode: str,
    ) -> dict:
        by_symbol = {str(row.get("symbol") or "").strip().upper(): dict(row) for row in coverage_rows or []}
        missing_symbols = [symbol for symbol in symbols if symbol not in by_symbol]
        if missing_symbols:
            fallback = self._build_daily_selection_bars_input_fingerprint(trade_date, symbols, request)
            fallback.update(
                {
                    "coverage_reason": "missing_daily_coverage",
                    "coverage_row_count": len(coverage_rows or []),
                    "missing_symbols": missing_symbols[:20],
                    "missing_symbol_count": len(missing_symbols),
                }
            )
            if bool(fallback.get("usable")):
                fallback["reason"] = "bars_aggregate_fallback"
            return fallback

        bad_rows = []
        hash_rows = []
        for symbol in symbols:
            row = dict(by_symbol.get(symbol) or {})
            status = str(row.get("status") or "").strip().lower()
            needs_repair = bool(row.get("needs_repair"))
            hard_gate = bool(row.get("hard_gate"))
            missing_count = int(row.get("missing_count", 0) or 0)
            gap_count = int(row.get("gap_count", 0) or 0)
            duplicate_count = int(row.get("duplicate_count", 0) or 0)
            bad_ohlc_count = int(row.get("bad_ohlc_count", 0) or 0)
            if (
                status not in DAILY_COVERAGE_OK_STATUSES
                or needs_repair
                or hard_gate
                or missing_count > 0
                or gap_count > 0
                or duplicate_count > 0
                or bad_ohlc_count > 0
            ):
                bad_rows.append({"symbol": symbol, "status": status, "needs_repair": needs_repair, "hard_gate": hard_gate})
            hash_rows.append(
                {
                    "symbol": symbol,
                    "status": status,
                    "expected_count": int(row.get("expected_count", 0) or 0),
                    "actual_count": int(row.get("actual_count", 0) or 0),
                    "missing_count": missing_count,
                    "gap_count": gap_count,
                    "duplicate_count": duplicate_count,
                    "bad_ohlc_count": bad_ohlc_count,
                    "expected_start_ms": int(row.get("expected_start_ms", 0) or 0),
                    "expected_end_ms": int(row.get("expected_end_ms", 0) or 0),
                    "first_bar_ms": int(row.get("first_bar_ms", 0) or 0),
                    "last_bar_ms": int(row.get("last_bar_ms", 0) or 0),
                    "expected_mask_hex": str(row.get("expected_mask_hex") or ""),
                    "actual_mask_hex": str(row.get("actual_mask_hex") or ""),
                    "missing_mask_hex": str(row.get("missing_mask_hex") or ""),
                    "last_repair_at": str(row.get("last_repair_at") or ""),
                }
            )
        if bad_rows:
            return {
                "usable": False,
                "hash": "",
                "reason": "daily_coverage_not_clean",
                "row_count": len(coverage_rows or []),
                "bad_rows": bad_rows[:20],
                "bad_row_count": len(bad_rows),
            }
        return {
            "usable": True,
            "hash": self._hash_daily_selection_cache_payload(
                {
                    "trade_date": trade_date,
                    "environment": str(request.get("source_environment") or "live"),
                    "session_mode": session_mode,
                    "coverage": hash_rows,
                }
            ),
            "reason": "coverage_clean",
            "row_count": len(hash_rows),
        }

    def _preload_daily_selection_input_fingerprints(self, request: dict, trading_dates: list[str]) -> tuple[dict[str, dict], dict]:
        started_at = time.time()
        meta = {
            "source": "skipped",
            "requested_days": len(trading_dates or []),
            "fingerprint_days": 0,
            "duration_s": 0.0,
            "error": "",
        }
        requested_symbols = sorted(
            {
                str(symbol or "").strip().upper()
                for symbol in list(request.get("symbols") or [])
                if str(symbol or "").strip().upper() not in request_excluded_symbols(request)
            }
        )
        dates = [str(date or "")[:10] for date in trading_dates or [] if str(date or "")[:10]]
        if not requested_symbols or not dates:
            meta["duration_s"] = round(time.time() - started_at, 3)
            return {}, meta
        db_path = str(runtime_backtest_sqlite_path() or "").strip()
        if not db_path or not os.path.exists(db_path):
            meta.update({"source": "unavailable", "duration_s": round(time.time() - started_at, 3), "error": "sqlite_unavailable"})
            return {}, meta

        session_mode = str(request.get("scan_session_mode") or request.get("session_mode") or "extended").strip().lower() or "extended"
        try:
            with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=20) as conn:
                conn.row_factory = sqlite3.Row
                coverage_rows = load_daily_coverage_rows(
                    conn,
                    symbols=requested_symbols,
                    environment=str(request.get("source_environment") or "live"),
                    date_from=min(dates),
                    date_to=max(dates),
                    interval="5m",
                    session_mode=session_mode,
                )
        except Exception as exc:
            meta.update(
                {
                    "source": "error",
                    "duration_s": round(time.time() - started_at, 3),
                    "error": f"coverage_query_failed:{str(exc)[:120]}",
                }
            )
            return {}, meta

        rows_by_date: dict[str, list[dict]] = {}
        for row in coverage_rows or []:
            market_date = str(row.get("market_date") or "")[:10]
            if market_date:
                rows_by_date.setdefault(market_date, []).append(dict(row))
        fingerprints = {
            trade_date: self._build_daily_selection_input_fingerprint_from_coverage(
                trade_date,
                requested_symbols,
                rows_by_date.get(trade_date) or [],
                request,
                session_mode,
            )
            for trade_date in dates
        }
        meta.update(
            {
                "source": "sqlite",
                "fingerprint_days": len(fingerprints),
                "coverage_rows": len(coverage_rows or []),
                "duration_s": round(time.time() - started_at, 3),
            }
        )
        return fingerprints, meta

    def _build_daily_selection_bars_input_fingerprint(
        self,
        trade_date: str,
        symbols: list[str],
        request: dict,
    ) -> dict:
        normalized_symbols = sorted({str(symbol or "").strip().upper() for symbol in symbols or [] if str(symbol or "").strip()})
        if not normalized_symbols:
            return {"usable": True, "hash": self._hash_daily_selection_cache_payload({"symbols": []}), "reason": "empty_universe", "row_count": 0}
        db_path = str(runtime_backtest_sqlite_path() or "").strip()
        if not db_path or not os.path.exists(db_path):
            return {"usable": False, "hash": "", "reason": "sqlite_unavailable", "row_count": 0}
        cutoff_ms = self._build_scan_cutoff_ms(trade_date, request.get("premarket_cutoff_time") or DEFAULT_SCAN_CUTOFF_TIME)
        day_start_ms = int(datetime.strptime(trade_date, "%Y-%m-%d").replace(tzinfo=ET).timestamp() * 1000)
        day_end_ms = int(
            datetime.strptime(trade_date, "%Y-%m-%d")
            .replace(tzinfo=ET, hour=23, minute=59, second=59, microsecond=999000)
            .timestamp()
            * 1000
        )
        warmup_limit = max(
            self._effective_indicator_warmup_bars(request, "scan_warmup_bars"),
            self._effective_indicator_warmup_bars(request, "warmup_bars"),
            40,
        )
        symbol_placeholders = ",".join(["?"] * len(normalized_symbols))
        environment = str(request.get("source_environment") or "live").strip().lower() or "live"
        grouped_rows = []
        try:
            with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=20) as conn:
                conn.row_factory = sqlite3.Row
                for interval in SCAN_INTERVALS:
                    normalized_interval = normalize_interval(interval)
                    interval_ms = interval_to_ms(normalized_interval)
                    lookback_start_ms = max(0, cutoff_ms - interval_ms * (warmup_limit + 30))
                    start_ms = min(lookback_start_ms, day_start_ms)
                    end_ms = day_end_ms if normalized_interval == "5m" else cutoff_ms
                    rows = conn.execute(
                        f"""
                        SELECT
                          symbol,
                          interval,
                          COUNT(*) AS bar_count,
                          MIN(bar_time_ms) AS first_bar_ms,
                          MAX(bar_time_ms) AS last_bar_ms,
                          ROUND(SUM(open), 6) AS sum_open,
                          ROUND(SUM(high), 6) AS sum_high,
                          ROUND(SUM(low), 6) AS sum_low,
                          ROUND(SUM(close), 6) AS sum_close,
                          ROUND(SUM(volume), 6) AS sum_volume
                        FROM ibkr_bars
                        WHERE environment = ?
                          AND interval = ?
                          AND symbol IN ({symbol_placeholders})
                          AND bar_time_ms >= ?
                          AND bar_time_ms <= ?
                        GROUP BY symbol, interval
                        ORDER BY symbol ASC, interval ASC
                        """,
                        (environment, normalized_interval, *normalized_symbols, int(start_ms), int(end_ms)),
                    ).fetchall()
                    for row in rows or []:
                        grouped_rows.append(
                            {
                                "symbol": str(row["symbol"] or "").strip().upper(),
                                "interval": str(row["interval"] or "").strip().lower(),
                                "bar_count": int(row["bar_count"] or 0),
                                "first_bar_ms": int(row["first_bar_ms"] or 0),
                                "last_bar_ms": int(row["last_bar_ms"] or 0),
                                "sum_open": float(row["sum_open"] or 0),
                                "sum_high": float(row["sum_high"] or 0),
                                "sum_low": float(row["sum_low"] or 0),
                                "sum_close": float(row["sum_close"] or 0),
                                "sum_volume": float(row["sum_volume"] or 0),
                            }
                        )
        except Exception as exc:
            return {"usable": False, "hash": "", "reason": f"bars_aggregate_query_failed:{str(exc)[:120]}", "row_count": 0}

        if not grouped_rows:
            return {"usable": False, "hash": "", "reason": "bars_aggregate_empty", "row_count": 0}
        payload = {
            "trade_date": trade_date,
            "environment": environment,
            "session_mode": str(request.get("scan_session_mode") or request.get("session_mode") or "extended"),
            "cutoff_ms": cutoff_ms,
            "day_start_ms": day_start_ms,
            "day_end_ms": day_end_ms,
            "warmup_limit": warmup_limit,
            "symbols": normalized_symbols,
            "rows": grouped_rows,
        }
        return {
            "usable": True,
            "hash": self._hash_daily_selection_cache_payload(payload),
            "reason": "bars_aggregate",
            "row_count": len(grouped_rows),
            "source": "ibkr_bars_aggregate",
        }

    def _pb_filter_escape(self, value: Any) -> str:
        escaper = getattr(self.pb, "_escape_filter_string", None)
        if callable(escaper):
            return str(escaper(value))
        return str(value or "").replace("\\", "\\\\").replace('"', '\\"')

    def _parse_daily_selection_cache_json(self, value: Any) -> Any:
        if isinstance(value, (dict, list)):
            return value
        if isinstance(value, str) and value.strip():
            try:
                return json.loads(value)
            except Exception:
                return None
        return None

    def _load_daily_selection_cache_record(self, cache_key: str, trade_date: str) -> dict | None:
        if not self.pb or not cache_key or not trade_date:
            return None
        filter_expr = (
            f'cache_key = "{self._pb_filter_escape(cache_key)}" '
            f'&& market_date = "{self._pb_filter_escape(trade_date)}"'
        )
        try:
            getter = getattr(self.pb, "get_first_record", None)
            if callable(getter):
                row = getter(BACKTEST_DAILY_SELECTION_CACHE_COLLECTION, filter=filter_expr)
                return dict(row) if row else None
            rows = self.pb.get_records(BACKTEST_DAILY_SELECTION_CACHE_COLLECTION, filter=filter_expr, per_page=1, page=1)
            return dict(rows[0]) if rows else None
        except Exception:
            return None

    def _load_daily_selection_cache_records(self, cache_key: str, trade_dates: list[str]) -> tuple[dict[str, dict], dict]:
        started_at = time.time()
        dates = []
        seen = set()
        for trade_date in trade_dates or []:
            date_text = str(trade_date or "")[:10]
            if not date_text or date_text in seen:
                continue
            seen.add(date_text)
            dates.append(date_text)

        meta = {
            "source": "skipped",
            "requested": len(dates),
            "found": 0,
            "duration_s": 0.0,
            "error": "",
            "fallback_reason": "",
        }
        if not cache_key or not dates:
            meta["duration_s"] = round(time.time() - started_at, 3)
            return {}, meta

        db_path = str(runtime_backtest_sqlite_path() or "").strip()
        sqlite_error = ""
        if db_path and os.path.exists(db_path):
            try:
                records: dict[str, dict] = {}
                with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10) as conn:
                    conn.row_factory = sqlite3.Row
                    for offset in range(0, len(dates), 800):
                        chunk = dates[offset : offset + 800]
                        placeholders = ",".join(["?"] * len(chunk))
                        rows = conn.execute(
                            f"""
                            SELECT
                              id,
                              cache_key,
                              market_date,
                              source_environment,
                              algorithm_version,
                              request_hash,
                              universe_hash,
                              input_data_hash,
                              scan_settings_hash,
                              strategy_hash,
                              status,
                              selected_count,
                              target_rows,
                              selection_plan,
                              daily_summary,
                              fingerprint_extra,
                              error,
                              last_built_at,
                              created,
                              updated
                            FROM {BACKTEST_DAILY_SELECTION_CACHE_COLLECTION}
                            WHERE cache_key = ?
                              AND market_date IN ({placeholders})
                            """,
                            (cache_key, *chunk),
                        ).fetchall()
                        for row in rows or []:
                            record = dict(row)
                            market_date = str(record.get("market_date") or "")[:10]
                            if market_date:
                                records[market_date] = record
                meta.update(
                    {
                        "source": "sqlite",
                        "found": len(records),
                        "duration_s": round(time.time() - started_at, 3),
                    }
                )
                if records or isinstance(self.pb, PBClient) or not self.pb:
                    return records, meta
                sqlite_error = "sqlite_empty_for_non_pb_client"
            except Exception as exc:
                sqlite_error = str(exc)[:240]

        records = {}
        if self.pb:
            for trade_date in dates:
                record = self._load_daily_selection_cache_record(cache_key, trade_date)
                if record:
                    records[trade_date] = record
            meta.update(
                {
                    "source": "pb",
                    "found": len(records),
                    "duration_s": round(time.time() - started_at, 3),
                    "fallback_reason": sqlite_error,
                }
            )
            return records, meta

        meta.update(
            {
                "source": "unavailable",
                "duration_s": round(time.time() - started_at, 3),
                "error": sqlite_error or "sqlite_unavailable",
            }
        )
        return {}, meta

    def _daily_selection_cache_record_is_valid(self, record: dict | None, fingerprints: dict) -> tuple[bool, str]:
        if not record:
            return False, "miss"
        if bool(fingerprints.get("force_rebuild")):
            return False, "force_rebuild"
        if str(record.get("status") or "").strip().lower() != "valid":
            return False, "stale_status"
        fields = ["cache_key", "request_hash", "universe_hash", "input_data_hash", "scan_settings_hash", "strategy_hash"]
        if bool(fingerprints.get("ignore_input_data_hash")):
            fields.remove("input_data_hash")
        for field in fields:
            expected = str(fingerprints.get(field) or "")
            actual = str(record.get(field) or "")
            if expected and actual != expected:
                return False, f"{field}_mismatch"
        target_rows = self._parse_daily_selection_cache_json(record.get("target_rows"))
        if not isinstance(target_rows, list):
            return False, "target_rows_invalid"
        selection_plan = self._parse_daily_selection_cache_json(record.get("selection_plan"))
        if not isinstance(selection_plan, dict):
            return False, "selection_plan_invalid"
        selected_count = int(record.get("selected_count", 0) or 0)
        if selected_count > 0 and not target_rows:
            return False, "target_rows_empty"
        return True, "hit"

    def _prepare_cached_daily_selection(
        self,
        record: dict,
        trade_date: str,
        cache_key: str,
    ) -> tuple[list[dict], list[str], dict]:
        raw_target_rows = self._parse_daily_selection_cache_json(record.get("target_rows"))
        target_rows = list(raw_target_rows or []) if isinstance(raw_target_rows, list) else []
        prepared_rows = []
        symbols = []
        for row in target_rows:
            if not isinstance(row, dict):
                continue
            item = dict(row)
            symbol = str(item.get("symbol", "") or "").strip().upper()
            if symbol and symbol not in symbols:
                symbols.append(symbol)
            item["symbol"] = symbol
            item["date"] = str(item.get("date") or trade_date)[:10]
            extra = self._parse_object(item.get("extra"))
            extra.update(
                {
                    "daily_selection_cache_hit": True,
                    "daily_selection_cache_key": cache_key,
                    "daily_selection_cache_record_id": str(record.get("id") or ""),
                }
            )
            item["extra"] = extra
            prepared_rows.append(item)
        raw_plan = self._parse_daily_selection_cache_json(record.get("selection_plan"))
        if isinstance(raw_plan, dict):
            for symbol in list(raw_plan.get(trade_date) or []):
                normalized = str(symbol or "").strip().upper()
                if normalized and normalized not in symbols:
                    symbols.append(normalized)
        raw_summary = self._parse_daily_selection_cache_json(record.get("daily_summary"))
        daily_summary = dict(raw_summary or {}) if isinstance(raw_summary, dict) else {"date": trade_date}
        daily_summary.update(
            {
                "date": trade_date,
                "cache_hit": True,
                "cache_status": "hit",
                "cache_key": cache_key,
                "cache_record_id": str(record.get("id") or ""),
            }
        )
        return prepared_rows, symbols, daily_summary

    def _upsert_daily_selection_cache_record(self, payload: dict) -> dict:
        if not self.pb or not payload:
            return {"ok": False, "error": "pb_unavailable", "created": 0, "updated": 0}
        try:
            upserter = getattr(self.pb, "upsert_backtest_daily_selection_cache_items", None)
            if callable(upserter):
                return dict(upserter([payload]) or {})
            batch_upsert = getattr(self.pb, "_batch_upsert_records", None)
            if callable(batch_upsert):
                return dict(batch_upsert(BACKTEST_DAILY_SELECTION_CACHE_COLLECTION, [payload], ["cache_key", "market_date"], timeout=30) or {})
            existing = self._load_daily_selection_cache_record(str(payload.get("cache_key") or ""), str(payload.get("market_date") or ""))
            if existing and existing.get("id"):
                self.pb.update_record(BACKTEST_DAILY_SELECTION_CACHE_COLLECTION, existing["id"], payload)
                return {"ok": True, "created": 0, "updated": 1}
            self.pb.create_record(BACKTEST_DAILY_SELECTION_CACHE_COLLECTION, payload)
            return {"ok": True, "created": 1, "updated": 0}
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:300], "created": 0, "updated": 0}

    def _empty_daily_selection_cache_summary(self, request: dict, fingerprint: dict | None = None) -> dict:
        mode = self._daily_selection_cache_mode(request)
        enabled = self._daily_selection_cache_enabled(request)
        return {
            "enabled": bool(enabled),
            "mode": mode,
            "force_rebuild": bool(request.get("daily_selection_cache_force_rebuild")),
            "collection": BACKTEST_DAILY_SELECTION_CACHE_COLLECTION,
            "cache_key": str((fingerprint or {}).get("cache_key") or ""),
            "algorithm_version": DAILY_SELECTION_CACHE_ALGORITHM_VERSION,
            "hit_days": 0,
            "miss_days": 0,
            "stale_days": 0,
            "rebuilt_days": 0,
            "written_days": 0,
            "write_error_days": 0,
            "input_unusable_days": 0,
            "preload_source": "disabled" if not enabled else "",
            "preload_requested_days": 0,
            "preload_found_days": 0,
            "preload_duration_s": 0.0,
            "preload_error": "",
            "preload_fallback_reason": "",
            "input_fingerprint_preload_source": "disabled" if not enabled else "",
            "input_fingerprint_preload_days": 0,
            "input_fingerprint_preload_duration_s": 0.0,
            "input_fingerprint_preload_error": "",
            "cached_input_hash_days": 0,
            "reason_counts": {},
            "duration_s": 0.0,
        }

    def _increment_daily_selection_cache_reason(self, summary: dict, reason: str) -> None:
        reasons = summary.setdefault("reason_counts", {})
        key = str(reason or "unknown").strip() or "unknown"
        reasons[key] = int(reasons.get(key, 0) or 0) + 1


    def _build_historical_sd_admission(
        self,
        symbol: str,
        trade_date: str,
        request: dict,
        candidate: dict | None = None,
    ) -> dict:
        session_mode = str(request.get("session_mode") or request.get("scan_session_mode") or "extended").strip().lower() or "extended"
        if session_mode not in SESSION_MODE_VALUES:
            session_mode = "extended"
        bars = self._load_symbol_bars(
            symbol,
            request["source_environment"],
            trade_date,
            trade_date,
            session_mode,
            allow_backfill=False,
        )
        if not bars:
            return {
                "passed": False,
                "reason": "sd_no_day_bars",
                "sd_scanned_bars": 0,
                "sd_admitted_at_ms": 0,
                "sd_window_status": "no_bars",
            }

        params = dict((request.get("params") or {}).get("strategy_params") or DEFAULT_PARAMS)
        signal_window_max_bars = self._historical_sd_window_max_bars(request)
        engine = runtime_indicator_engine()(symbol, "5m", params=params)
        signal_gen = runtime_signal_generator()(symbol, "5m", params=params)
        warmup_limit = self._effective_indicator_warmup_bars(request, "warmup_bars")
        warmup_bars = self._load_symbol_warmup_bars(
            symbol,
            request["source_environment"],
            int(bars[0].get("bar_time_ms", 0) or 0),
            session_mode,
            limit=warmup_limit,
        )
        previous_day = self._bootstrap_backtest_state(engine, signal_gen, warmup_bars) if warmup_bars else ""
        cutoff_ms = int(((candidate or {}).get("extra") or {}).get("scan_cutoff_ms", 0) or 0)
        if cutoff_ms <= 0:
            cutoff_ms = self._build_scan_cutoff_ms(trade_date, request.get("premarket_cutoff_time") or DEFAULT_SCAN_CUTOFF_TIME)
        admission_start_ms = max(
            cutoff_ms,
            self._build_trade_date_time_ms(
                trade_date,
                request.get("daily_selection_admission_start_time") or request.get("trade_window_start_time"),
                DEFAULT_PORTFOLIO_TRADE_WINDOW_START,
            ),
        )
        admission_end_ms = self._build_trade_date_time_ms(
            trade_date,
            request.get("daily_selection_admission_end_time") or request.get("trade_window_end_time"),
            DEFAULT_PORTFOLIO_TRADE_WINDOW_END,
        )
        last_item: dict = {}
        scanned_bars = 0
        ready_bars = 0
        for bar in bars:
            if self._cancel_event.is_set():
                raise BacktestCancelled()
            bar_ms = int(bar.get("bar_time_ms", 0) or 0)
            if bar_ms <= 0:
                continue
            current_day = str(bar.get("us_time", "") or "")[:10] or ms_to_et(bar_ms).strftime("%Y-%m-%d")
            if previous_day and current_day != previous_day:
                signal_gen.daily_reset()
            previous_day = current_day
            snapshot = engine.update(bar)
            scanned_bars += 1
            if not snapshot or not engine.is_ready():
                continue
            ready_bars += 1
            signal_gen.update(snapshot)
            if bar_ms < admission_start_ms or bar_ms > admission_end_ms:
                continue
            trace = signal_gen.get_trace_snapshot()
            admission_item = build_active_window_admission_item(
                trace,
                signal_window_max_bars=signal_window_max_bars,
            )
            last_item = admission_item
            if bool(admission_item.get("admitted")):
                return {
                    "passed": True,
                    "reason": "sd_window_admitted",
                    "sd_scanned_bars": scanned_bars,
                    "sd_ready_bars": ready_bars,
                    "sd_admitted_at_ms": bar_ms,
                    "sd_admitted_us_time": str(bar.get("us_time", "") or format_us_time(bar_ms)),
                    "sd_admitted_cn_time": str(bar.get("cn_time", "") or format_cn_time(bar_ms)),
                    "sd_window_status": str(admission_item.get("window_status") or ""),
                    "sd_trace_stage": str(admission_item.get("trace_stage") or ""),
                    "sd_upper_valid": bool(admission_item.get("sd_upper_valid")),
                    "sd_lower_valid": bool(admission_item.get("sd_lower_valid")),
                    "sd_upper_active": bool(admission_item.get("sd_upper_active")),
                    "sd_lower_active": bool(admission_item.get("sd_lower_active")),
                    "sd_upper_age_bars": int(admission_item.get("sd_upper_age_bars", 0) or 0),
                    "sd_lower_age_bars": int(admission_item.get("sd_lower_age_bars", 0) or 0),
                    "bars_remaining": int(admission_item.get("bars_remaining", 0) or 0),
                    "component_progress": float(admission_item.get("component_progress", 0) or 0),
                    "components": admission_item.get("components") if isinstance(admission_item.get("components"), dict) else {},
                    "admission_start_ms": admission_start_ms,
                    "admission_end_ms": admission_end_ms,
                    "shared_admission_helper": "ibkr_compute.core.active_window_admission.is_active_window_admitted",
                }

        return {
            "passed": False,
            "reason": str(last_item.get("window_status") or "sd_no_valid_window"),
            "sd_scanned_bars": scanned_bars,
            "sd_ready_bars": ready_bars,
            "sd_admitted_at_ms": 0,
            "sd_window_status": str(last_item.get("window_status") or "no_window"),
            "sd_trace_stage": str(last_item.get("trace_stage") or ""),
            "sd_upper_valid": bool(last_item.get("sd_upper_valid")),
            "sd_lower_valid": bool(last_item.get("sd_lower_valid")),
            "sd_upper_active": bool(last_item.get("sd_upper_active")),
            "sd_lower_active": bool(last_item.get("sd_lower_active")),
            "sd_upper_age_bars": int(last_item.get("sd_upper_age_bars", 0) or 0),
            "sd_lower_age_bars": int(last_item.get("sd_lower_age_bars", 0) or 0),
            "bars_remaining": int(last_item.get("bars_remaining", 0) or 0),
            "component_progress": float(last_item.get("component_progress", 0) or 0),
            "admission_start_ms": admission_start_ms,
            "admission_end_ms": admission_end_ms,
            "shared_admission_helper": "ibkr_compute.core.active_window_admission.is_active_window_admitted",
        }

    def _apply_historical_sd_admission_to_candidates(
        self,
        trade_date: str,
        day_candidates: list[dict],
        request: dict,
        progress_context: dict | None = None,
    ) -> tuple[list[dict], dict]:
        sd_mode = self._daily_selection_sd_mode(request)
        if sd_mode == "off" or (
            not bool(request.get("daily_selection_require_sd_trigger"))
            and not bool(request.get("daily_selection_reuse_live_admission"))
        ):
            return day_candidates, {
                "enabled": False,
                "mode": sd_mode,
                "sd_scanned_count": 0,
                "sd_admitted_count": 0,
                "sd_rejected_count": 0,
                "sd_scan_budget_deferred_count": 0,
            }

        candidate_limit = int(request.get("daily_selection_candidate_limit", 0) or 0)
        if candidate_limit <= 0:
            candidate_limit = max(int(request.get("max_symbols", DEFAULT_MAX_SYMBOLS) or DEFAULT_MAX_SYMBOLS) * 5, 20)
        scan_candidates = list(day_candidates[:candidate_limit])
        deferred = max(0, len(day_candidates) - len(scan_candidates))
        admitted: list[dict] = []
        rejected: list[dict] = []
        rejected_count = 0
        rejection_summary: dict[str, int] = {}
        total = max(1, len(scan_candidates))
        for index, candidate in enumerate(scan_candidates, start=1):
            symbol = str(candidate.get("symbol", "") or "").strip().upper()
            if not symbol:
                continue
            if index == 1 or index % 10 == 0 or index == total:
                self._set_progress_context(
                    "running",
                    "sd_admission_replay",
                    f"replaying SD admission {trade_date} {index}/{total}",
                    12,
                    progress_context,
                )
            admission = self._build_historical_sd_admission(symbol, trade_date, request, candidate)
            enriched = dict(candidate)
            extra = dict(enriched.get("extra") or {})
            extra.update(
                {
                    "sd_selection_enabled": True,
                    "sd_selection_passed": bool(admission.get("passed")),
                    "sd_selection_reason": str(admission.get("reason") or ""),
                    **admission,
                }
            )
            enriched["extra"] = extra
            if bool(admission.get("passed")):
                admitted.append(enriched)
            else:
                rejected.append(enriched)
                rejected_count += 1
                reason = str(admission.get("reason") or "sd_rejected").strip() or "sd_rejected"
                rejection_summary[reason] = int(rejection_summary.get(reason, 0) or 0) + 1

        if sd_mode == "rank":
            ranked_candidates = admitted + rejected + list(day_candidates[candidate_limit:])
        else:
            ranked_candidates = admitted

        return ranked_candidates, {
            "enabled": True,
            "mode": sd_mode,
            "sd_scanned_count": len(scan_candidates),
            "sd_admitted_count": len(admitted),
            "sd_rejected_count": rejected_count,
            "sd_scan_budget_deferred_count": deferred,
            "sd_rejection_summary": rejection_summary,
            "shared_admission_helper": "ibkr_compute.core.active_window_admission.is_active_window_admitted",
        }

    def _load_interval_bars_before(
        self,
        symbol: str,
        source_environment: str,
        interval: str,
        end_bar_time_ms: int,
        session_mode: str,
        limit: int,
    ) -> list[dict]:
        normalized_interval = normalize_interval(interval)
        if end_bar_time_ms <= 0 or limit <= 0:
            return []
        rows = self._load_bar_rows_from_sqlite(
            symbol,
            source_environment,
            interval=normalized_interval,
            end_ms=end_bar_time_ms,
            descending=True,
            limit=max(limit + 20, limit),
        )
        if not rows and self.pb:
            rows = self.pb.get_all_records(
                "ibkr_bars",
                filter=(
                    f'symbol = "{symbol}" && interval = "{normalized_interval}" && environment = "{source_environment}" '
                    f"&& bar_time_ms <= {end_bar_time_ms}"
                ),
                sort="-bar_time_ms",
                max_pages=max(4, min(20, math.ceil(limit / 200) + 2)),
            )
        normalized = []
        seen = set()
        for row in rows:
            bar_ms = int(row.get("bar_time_ms", 0) or 0)
            if bar_ms <= 0 or bar_ms in seen:
                continue
            seen.add(bar_ms)
            session_type = str(row.get("session_type", "") or classify_session(bar_time_ms=bar_ms))
            if session_mode == "regular" and normalized_interval != "1d" and session_type != "regular":
                continue
            normalized.append(
                {
                    "symbol": symbol,
                    "exchange": str(row.get("exchange", "") or "").upper(),
                    "interval": normalized_interval,
                    "open": float(row.get("open", 0) or 0),
                    "high": float(row.get("high", 0) or 0),
                    "low": float(row.get("low", 0) or 0),
                    "close": float(row.get("close", 0) or 0),
                    "volume": float(row.get("volume", 0) or 0),
                    "session_type": session_type,
                    "us_time": str(row.get("us_time", "") or format_us_time(bar_ms)),
                    "cn_time": str(row.get("cn_time", "") or format_cn_time(bar_ms)),
                    "bar_time_ms": bar_ms,
                }
            )
            if len(normalized) >= limit:
                break
        normalized.sort(key=lambda item: int(item["bar_time_ms"]))
        return normalized

    def _build_historical_scan_metric_row(
        self,
        symbol: str,
        trade_date: str,
        request: dict,
        cutoff_ms: int,
        engines: dict | None = None,
    ) -> dict:
        source_environment = str(request.get("source_environment") or "live").strip().lower() or "live"
        day_start_ms = int(datetime.strptime(trade_date, "%Y-%m-%d").replace(tzinfo=ET).timestamp() * 1000)
        intraday_rows = self._load_bar_rows_from_sqlite(
            symbol,
            source_environment,
            interval="5m",
            start_ms=day_start_ms,
            end_ms=cutoff_ms,
            descending=False,
        )
        if not intraday_rows and not os.path.exists(str(runtime_backtest_sqlite_path() or "")):
            intraday_rows = [
                row
                for row in self._load_interval_bars_before(
                    symbol,
                    source_environment,
                    "5m",
                    cutoff_ms,
                    "extended",
                    240,
                )
                if ms_to_et(int(row.get("bar_time_ms", 0) or 0)).strftime("%Y-%m-%d") == trade_date
            ]

        premarket_volume = 0.0
        latest_close = 0.0
        exchange = ""
        for row in intraday_rows:
            bar_ms = int(row.get("bar_time_ms", 0) or 0)
            if bar_ms <= 0 or bar_ms > cutoff_ms:
                continue
            session_type = str(row.get("session_type", "") or classify_session(bar_time_ms=bar_ms)).strip().lower()
            if session_type == "premarket":
                premarket_volume += float(row.get("volume", 0) or 0)
            latest_close = float(row.get("close", 0) or latest_close or 0)
            if not exchange:
                exchange = str(row.get("exchange", "") or "").strip().upper()

        daily_rows = self._load_bar_rows_from_sqlite(
            symbol,
            source_environment,
            interval="1d",
            end_ms=max(0, day_start_ms - 1),
            descending=True,
            limit=16,
        )
        daily_rows = [
            row
            for row in daily_rows
            if ms_to_et(int(row.get("bar_time_ms", 0) or 0)).strftime("%Y-%m-%d") < trade_date
        ]
        daily_rows.sort(key=lambda item: int(item.get("bar_time_ms", 0) or 0))
        if not daily_rows:
            lookback_start_ms = max(0, day_start_ms - interval_to_ms("1d") * 20)
            prior_5m = self._load_bar_rows_from_sqlite(
                symbol,
                source_environment,
                interval="5m",
                start_ms=lookback_start_ms,
                end_ms=max(0, day_start_ms - 1),
                descending=False,
            )
            grouped: dict[str, dict] = {}
            for row in prior_5m:
                bar_ms = int(row.get("bar_time_ms", 0) or 0)
                if bar_ms <= 0:
                    continue
                date_key = ms_to_et(bar_ms).strftime("%Y-%m-%d")
                bucket = grouped.setdefault(date_key, {"bar_time_ms": bar_ms, "close": 0.0, "volume": 0.0})
                bucket["bar_time_ms"] = bar_ms
                bucket["close"] = float(row.get("close", 0) or bucket.get("close", 0) or 0)
                bucket["volume"] = float(bucket.get("volume", 0) or 0) + float(row.get("volume", 0) or 0)
            daily_rows = [grouped[key] for key in sorted(grouped.keys())][-16:]

        last_10_daily = daily_rows[-10:]
        avg_10d_volume = (
            sum(float(row.get("volume", 0) or 0) for row in last_10_daily) / len(last_10_daily)
            if last_10_daily
            else 0.0
        )
        prev_close = float(daily_rows[-1].get("close", 0) or 0) if daily_rows else 0.0
        if latest_close <= 0:
            latest_close = prev_close
        day_change_pct = ((latest_close - prev_close) / prev_close * 100.0) if prev_close > 0 else 0.0

        atr_pct = 0.0
        engine = (engines or {}).get((source_environment, symbol, "5m"))
        if engine is not None and engine.is_ready():
            snapshot = engine.get_snapshot() or {}
            atr_pct = abs(float(snapshot.get("atr_pct", 0) or 0))
            if atr_pct <= 0 and latest_close > 0:
                atr_pct = abs(float(snapshot.get("atr", 0) or 0)) / latest_close * 100.0
        else:
            atr_pct = self._load_historical_scan_atr_pct(symbol, source_environment, cutoff_ms, latest_close)

        return {
            "symbol": symbol,
            "exchange": exchange,
            "price": round(float(latest_close or 0), 4),
            "avg_10d_volume": round(float(avg_10d_volume or 0), 2),
            "premarket_volume": round(float(premarket_volume or 0), 2),
            "atr_pct": round(float(atr_pct or 0), 4),
            "day_change_pct": round(float(day_change_pct or 0), 4),
            "latest_bar_time_ms": int(intraday_rows[-1].get("bar_time_ms", 0) or 0) if intraday_rows else 0,
        }

    def _load_historical_scan_atr_pct(
        self,
        symbol: str,
        source_environment: str,
        cutoff_ms: int,
        latest_close: float = 0.0,
    ) -> float:
        db_path = str(runtime_backtest_sqlite_path() or "").strip()
        if not db_path or not os.path.exists(db_path) or cutoff_ms <= 0:
            return 0.0
        try:
            with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    """
                    SELECT extra
                    FROM ibkr_indicators
                    WHERE symbol = ?
                      AND interval = ?
                      AND environment = ?
                      AND bar_time_ms <= ?
                    ORDER BY bar_time_ms DESC
                    LIMIT 1
                    """,
                    (symbol, interval_to_chart_tf("5m"), source_environment, int(cutoff_ms)),
                ).fetchone()
        except Exception:
            return 0.0
        if not row:
            return 0.0
        extra = self._parse_object(row["extra"])
        atr_pct = abs(float(extra.get("atr_pct", 0) or 0))
        if atr_pct <= 0 and latest_close > 0:
            atr_pct = abs(float(extra.get("atr", 0) or 0)) / latest_close * 100.0
        return round(float(atr_pct or 0), 4)

    def _build_historical_scan_metric_rejections(
        self,
        symbol: str,
        metric_row: dict,
        settings: dict,
        *,
        allow_unknown_atr: bool = False,
    ) -> list[dict]:
        if normalize_admission_bool((settings or {}).get("dynamic_admission_enabled"), False):
            admission = evaluate_dynamic_admission(symbol, metrics=metric_row, settings=settings)
            if bool(admission.get("quality_gate_passed")):
                return []
            thresholds = admission.get("dynamic_thresholds") or {}
            examples = []
            for gate in admission.get("failed_gates") or []:
                bucket = str((gate or {}).get("bucket") or "")
                if allow_unknown_atr and bucket == "atr_pct_below_threshold":
                    try:
                        if float((gate or {}).get("actual") or 0) <= 0:
                            continue
                    except Exception:
                        pass
                examples.append(
                    {
                        "bucket": bucket,
                        "symbol": str((gate or {}).get("symbol") or symbol),
                        "actual": (gate or {}).get("actual"),
                        "threshold": (gate or {}).get("threshold"),
                        "note": (gate or {}).get("note") or "Dynamic admission prefilter failed",
                        "severity": (gate or {}).get("severity", ""),
                        "metric": (gate or {}).get("metric", ""),
                        "dynamic_admission": True,
                        "threshold_profile": thresholds.get("threshold_profile", ""),
                    }
                )
            return examples

        examples = []
        checks = (
            ("avg_10d_volume", "min_avg_10d_volume", "avg_10d_volume_below_threshold", "10 日均量不足"),
            ("premarket_volume", "min_premarket_volume", "premarket_volume_below_threshold", "盘前量不足"),
            ("day_change_pct", "min_abs_day_change_pct", "day_change_below_threshold", "日内涨跌幅不足"),
        )
        for metric_key, threshold_key, bucket, note in checks:
            actual = abs(float(metric_row.get(metric_key, 0) or 0)) if metric_key == "day_change_pct" else float(metric_row.get(metric_key, 0) or 0)
            threshold = float(settings.get(threshold_key, 0) or 0)
            if actual < threshold:
                examples.append(
                    {
                        "bucket": bucket,
                        "symbol": symbol,
                        "actual": round(actual, 4),
                        "threshold": round(threshold, 4),
                        "note": note,
                    }
                )
        atr_pct = abs(float(metric_row.get("atr_pct", 0) or 0))
        atr_threshold = float(settings.get("min_atr_pct", 0) or 0)
        if atr_pct < atr_threshold and not (allow_unknown_atr and atr_pct <= 0):
            examples.append(
                {
                    "bucket": "atr_pct_below_threshold",
                    "symbol": symbol,
                    "actual": round(atr_pct, 4),
                    "threshold": round(atr_threshold, 4),
                    "note": "ATR 不足",
                }
            )
        return examples

    def _apply_historical_scan_setting_overrides(self, settings: dict, request: dict | None = None) -> dict:
        merged = dict(settings or {})
        applied: dict[str, float] = {}
        for request_key, settings_key in (
            ("daily_scan_min_avg_10d_volume", "min_avg_10d_volume"),
            ("daily_scan_min_premarket_volume", "min_premarket_volume"),
            ("daily_scan_min_atr_pct", "min_atr_pct"),
            ("daily_scan_min_abs_day_change_pct", "min_abs_day_change_pct"),
        ):
            raw_value = (request or {}).get(request_key)
            if raw_value in (None, ""):
                continue
            try:
                value = max(0.0, float(raw_value))
            except Exception:
                continue
            if value <= 0:
                continue
            merged[settings_key] = value
            applied[settings_key] = value
        if applied:
            merged["override_source"] = "backtest_request"
            merged["overrides"] = applied
        return merged

    def _load_historical_scan_settings(self, source_environment: str | dict) -> dict:
        request = source_environment if isinstance(source_environment, dict) else {}
        environment = str((request or {}).get("source_environment") or source_environment or "live")
        try:
            settings = _load_scan_settings(environment)
        except Exception:
            settings = {
                "scan_time_et": DEFAULT_SCAN_CUTOFF_TIME,
                "min_avg_10d_volume": 100000,
                "min_premarket_volume": 5000,
                "min_atr_pct": 0.15,
                "min_abs_day_change_pct": 1.0,
                "monitor_count": 0,
                "target_subscription_limit": 80,
                "total_subscription_limit": 80,
                "trade_subscription_budget": 80,
            }
        return self._apply_historical_scan_setting_overrides(settings, request)

    def _load_historical_scan_fundamentals(self, symbol: str) -> dict:
        normalized_symbol = str(symbol or "").strip().upper()
        if not normalized_symbol:
            return {}
        cache = getattr(self, "_historical_scan_fundamentals_cache", None)
        if not isinstance(cache, dict):
            cache = {}
            setattr(self, "_historical_scan_fundamentals_cache", cache)
        if normalized_symbol not in cache:
            try:
                scanner = DailyScanner(self.pb, {})
                loaded = scanner._load_fundamentals_by_symbol([normalized_symbol])
            except Exception:
                loaded = {}
            cache[normalized_symbol] = dict((loaded or {}).get(normalized_symbol) or {})
        return dict(cache.get(normalized_symbol) or {})

    def _attach_historical_scan_fundamentals(self, symbol: str, metric_row: dict) -> dict:
        row = dict(metric_row or {})
        fundamentals = self._load_historical_scan_fundamentals(symbol)
        if fundamentals:
            row["fundamentals"] = dict(fundamentals)
            row["symbol_fundamentals"] = dict(fundamentals)
        return row

    def _build_historical_scan_engines(self, symbol: str, request: dict, cutoff_ms: int) -> tuple[dict, dict]:
        params = dict((request.get("params") or {}).get("strategy_params") or DEFAULT_PARAMS)
        environment = request["source_environment"]
        session_mode = request.get("scan_session_mode") or "extended"
        lookback_limit = self._effective_indicator_warmup_bars(request, "scan_warmup_bars")
        repair_summary = self._ensure_scan_history_available(symbol, request, cutoff_ms)
        engines = {}
        details = {
            "ready_timeframes": [],
            "bars_loaded": {},
            "last_bar_time_ms_by_interval": {},
            "repair_summary": repair_summary,
        }
        for interval in SCAN_INTERVALS:
            bars = self._load_interval_bars_before(
                symbol,
                environment,
                interval,
                cutoff_ms,
                session_mode,
                lookback_limit,
            )
            details["bars_loaded"][interval] = len(bars)
            if not bars:
                continue
            engine = runtime_indicator_engine()(symbol, interval, params=params)
            last_snapshot = None
            for bar in bars:
                last_snapshot = engine.update(bar)
            if not last_snapshot or not engine.is_ready():
                continue
            engines[(environment, symbol, interval)] = engine
            details["ready_timeframes"].append(interval)
            details["last_bar_time_ms_by_interval"][interval] = int(bars[-1].get("bar_time_ms", 0) or 0)
        details["ready_timeframes"].sort(key=lambda item: interval_to_ms(item))
        return engines, details

    def _evaluate_historical_scan_symbol(
        self,
        symbol: str,
        trade_date: str,
        request: dict,
        settings: dict | None = None,
    ) -> dict | None:
        cutoff_ms = self._build_scan_cutoff_ms(trade_date, request.get("premarket_cutoff_time") or DEFAULT_SCAN_CUTOFF_TIME)
        metric_row = self._attach_historical_scan_fundamentals(
            symbol,
            self._build_historical_scan_metric_row(symbol, trade_date, request, cutoff_ms, None),
        )
        scan_settings = settings or self._load_historical_scan_settings(request)
        prefilter_rejections = self._build_historical_scan_metric_rejections(
            symbol,
            metric_row,
            scan_settings,
            allow_unknown_atr=True,
        )
        if prefilter_rejections:
            return {
                "symbol": symbol,
                "score": 0,
                "technical_score": 0,
                "direction_bias": "neutral",
                "reason": "historical_metric_prefilter",
                "quality_gate_passed": False,
                "rejection_examples": prefilter_rejections,
                "extra": {
                    "environment": request["source_environment"],
                    "timeframes_ready": [],
                    "long_votes": 0,
                    "short_votes": 0,
                    "scan_cutoff_time": request.get("premarket_cutoff_time") or DEFAULT_SCAN_CUTOFF_TIME,
                    "scan_cutoff_ms": cutoff_ms,
                    "metric_row": metric_row,
                    "prefiltered_before_indicators": True,
                },
            }
        engines, details = self._build_historical_scan_engines(symbol, request, cutoff_ms)
        if not engines:
            return None
        metric_row = self._attach_historical_scan_fundamentals(
            symbol,
            self._build_historical_scan_metric_row(symbol, trade_date, request, cutoff_ms, engines),
        )
        scanner = DailyScanner(self.pb, engines)
        result = scanner.evaluate_symbol(
            symbol,
            trade_date,
            request["source_environment"],
            metrics=metric_row,
            settings=scan_settings,
        )
        if not result:
            return None
        enriched = dict(result)
        enriched_extra = dict(result.get("extra") or {})
        enriched_extra.update(
            {
                "scan_cutoff_time": request.get("premarket_cutoff_time") or DEFAULT_SCAN_CUTOFF_TIME,
                "scan_cutoff_ms": cutoff_ms,
                "scan_session_mode": request.get("scan_session_mode") or "extended",
                "scan_warmup_bars": int(request.get("scan_warmup_bars", BACKTEST_WARMUP_BARS) or BACKTEST_WARMUP_BARS),
                "effective_scan_warmup_bars": self._effective_indicator_warmup_bars(request, "scan_warmup_bars"),
                "ready_timeframes": list(details.get("ready_timeframes") or []),
                "bars_loaded": details.get("bars_loaded") or {},
                "last_bar_time_ms_by_interval": details.get("last_bar_time_ms_by_interval") or {},
                "metric_row": metric_row,
            }
        )
        enriched["extra"] = enriched_extra
        return enriched

    def _build_daily_scan_replay_plan(self, request: dict, progress_context: dict | None = None) -> dict:
        trading_dates = self._load_trading_dates(request)
        selected_symbols = []
        selected_lookup = set()
        target_rows = []
        selection_plan = {}
        daily_summaries = []
        total_days = max(1, len(trading_dates))
        universe_mode = "manual_symbols" if request.get("symbols") else "watchlist_snapshot"
        scan_settings = self._load_historical_scan_settings(request)
        cache_started_at = time.time()
        cache_fingerprint = self._build_daily_selection_cache_base_fingerprint(request, scan_settings)
        cache_summary = self._empty_daily_selection_cache_summary(request, cache_fingerprint)
        cache_write_enabled = bool(cache_summary.get("enabled")) and self._daily_selection_cache_mode(request) == "use_or_build"
        preloaded_cache_records: dict[str, dict] = {}
        preloaded_input_fingerprints: dict[str, dict] = {}
        if cache_summary.get("enabled"):
            preloaded_cache_records, preload_meta = self._load_daily_selection_cache_records(
                cache_fingerprint["cache_key"],
                trading_dates,
            )
            should_preload_input_fingerprints = bool(request.get("daily_selection_cache_force_rebuild")) or bool(
                request.get("daily_selection_cache_revalidate_input_hash")
            ) or len(preloaded_cache_records) < len(set(trading_dates))
            if should_preload_input_fingerprints:
                preloaded_input_fingerprints, input_preload_meta = self._preload_daily_selection_input_fingerprints(
                    request,
                    trading_dates,
                )
            else:
                input_preload_meta = {
                    "source": "cached_record",
                    "fingerprint_days": 0,
                    "duration_s": 0.0,
                    "error": "",
                }
            cache_summary.update(
                {
                    "preload_source": str(preload_meta.get("source") or ""),
                    "preload_requested_days": int(preload_meta.get("requested", 0) or 0),
                    "preload_found_days": int(preload_meta.get("found", 0) or 0),
                    "preload_duration_s": float(preload_meta.get("duration_s", 0.0) or 0.0),
                    "preload_error": str(preload_meta.get("error") or ""),
                    "preload_fallback_reason": str(preload_meta.get("fallback_reason") or ""),
                    "input_fingerprint_preload_source": str(input_preload_meta.get("source") or ""),
                    "input_fingerprint_preload_days": int(input_preload_meta.get("fingerprint_days", 0) or 0),
                    "input_fingerprint_preload_duration_s": float(input_preload_meta.get("duration_s", 0.0) or 0.0),
                    "input_fingerprint_preload_error": str(input_preload_meta.get("error") or ""),
                }
            )

        for day_index, trade_date in enumerate(trading_dates, start=1):
            if self._cancel_event.is_set():
                raise BacktestCancelled()
            progress_value = 3 + int((day_index - 1) / total_days * 12)
            self._set_progress_context(
                "running",
                "scan_replay",
                f"rebuilding premarket targets for {trade_date}",
                progress_value,
                progress_context,
            )
            universe_rows = self._load_scan_universe(request, as_of_date=trade_date)
            universe_snapshot_fallback = False
            if not universe_rows and trade_date and not request.get("symbols"):
                universe_rows = self._load_scan_universe(request, as_of_date="")
                universe_snapshot_fallback = bool(universe_rows)
            universe_hash = self._build_daily_selection_universe_hash(universe_rows)
            cache_record = preloaded_cache_records.get(trade_date) if cache_summary.get("enabled") else None
            if cache_summary.get("enabled"):
                if (
                    cache_record
                    and not bool(request.get("daily_selection_cache_force_rebuild"))
                    and not bool(request.get("daily_selection_cache_revalidate_input_hash"))
                ):
                    input_fingerprint = {
                        "usable": True,
                        "hash": str(cache_record.get("input_data_hash") or ""),
                        "reason": "cached_input_hash",
                        "source": "daily_selection_cache_record",
                    }
                    cache_summary["cached_input_hash_days"] = int(cache_summary.get("cached_input_hash_days", 0) or 0) + 1
                else:
                    input_fingerprint = preloaded_input_fingerprints.get(trade_date) or self._build_daily_selection_input_fingerprint(
                        trade_date,
                        universe_rows,
                        request,
                    )
            else:
                input_fingerprint = {"usable": False, "hash": "", "reason": "cache_disabled"}
            day_cache_fingerprint = {
                **cache_fingerprint,
                "universe_hash": universe_hash,
                "input_data_hash": str(input_fingerprint.get("hash") or ""),
                "force_rebuild": bool(request.get("daily_selection_cache_force_rebuild")),
                "ignore_input_data_hash": bool(request.get("daily_selection_cache_trust_existing")),
            }
            if cache_summary.get("enabled"):
                if not bool(input_fingerprint.get("usable")):
                    cache_summary["input_unusable_days"] = int(cache_summary.get("input_unusable_days", 0) or 0) + 1
                    cache_summary["miss_days"] = int(cache_summary.get("miss_days", 0) or 0) + 1
                    self._increment_daily_selection_cache_reason(cache_summary, str(input_fingerprint.get("reason") or "input_unusable"))
                else:
                    cache_valid, cache_reason = self._daily_selection_cache_record_is_valid(cache_record, day_cache_fingerprint)
                    if cache_valid and cache_record:
                        cached_rows, cached_symbols, cached_summary = self._prepare_cached_daily_selection(
                            cache_record,
                            trade_date,
                            cache_fingerprint["cache_key"],
                        )
                        for symbol in cached_symbols:
                            if symbol not in selected_lookup:
                                selected_lookup.add(symbol)
                                selected_symbols.append(symbol)
                        target_rows.extend(cached_rows)
                        selection_plan[trade_date] = list(cached_symbols)
                        daily_summaries.append(cached_summary)
                        cache_summary["hit_days"] = int(cache_summary.get("hit_days", 0) or 0) + 1
                        self._increment_daily_selection_cache_reason(cache_summary, "hit")
                        continue
                    if cache_reason == "miss":
                        cache_summary["miss_days"] = int(cache_summary.get("miss_days", 0) or 0) + 1
                    else:
                        cache_summary["stale_days"] = int(cache_summary.get("stale_days", 0) or 0) + 1
                    self._increment_daily_selection_cache_reason(cache_summary, cache_reason)
            day_candidates = []
            scanned_count = 0
            ready_count = 0
            quality_rejected_count = 0
            rejection_summary: dict[str, int] = {}
            for item in universe_rows:
                if self._cancel_event.is_set():
                    raise BacktestCancelled()
                symbol = str(item.get("symbol", "") or "").strip().upper()
                if not symbol:
                    continue
                scanned_count += 1
                evaluated = self._evaluate_historical_scan_symbol(symbol, trade_date, request, settings=scan_settings)
                if not evaluated:
                    continue
                ready_count += 1
                if not bool(evaluated.get("quality_gate_passed")):
                    quality_rejected_count += 1
                    for example in evaluated.get("rejection_examples") or []:
                        bucket = str((example or {}).get("bucket") or "").strip()
                        if bucket:
                            rejection_summary[bucket] = int(rejection_summary.get(bucket, 0) or 0) + 1
                    continue
                score = float(evaluated.get("score", 0) or 0)
                direction_bias = str(evaluated.get("direction_bias", "neutral") or "neutral")
                if score <= 0 or direction_bias == "neutral":
                    continue
                day_candidates.append(
                    {
                        "symbol": symbol,
                        "exchange": str(item.get("exchange", "") or "").upper(),
                        "score": score,
                        "direction_bias": direction_bias,
                        "scan_reason": str(evaluated.get("reason", "") or ""),
                        "extra": dict(evaluated.get("extra") or {}),
                    }
                )

            day_candidates.sort(key=lambda item: (-float(item.get("score", 0) or 0), item.get("symbol", "")))
            pre_sd_candidate_count = len(day_candidates)
            sd_summary = {
                "enabled": False,
                "sd_scanned_count": 0,
                "sd_admitted_count": 0,
                "sd_rejected_count": 0,
                "sd_scan_budget_deferred_count": 0,
                "sd_rejection_summary": {},
            }
            day_candidates, sd_summary = self._apply_historical_sd_admission_to_candidates(
                trade_date,
                day_candidates,
                request,
                progress_context=progress_context,
            )
            selected_rows = day_candidates[: request["max_symbols"]]
            selection_plan[trade_date] = [item["symbol"] for item in selected_rows]
            day_target_rows = []
            for rank, candidate in enumerate(selected_rows, start=1):
                symbol = candidate["symbol"]
                if symbol not in selected_lookup:
                    selected_lookup.add(symbol)
                    selected_symbols.append(symbol)
                cutoff_ms = int((candidate.get("extra") or {}).get("scan_cutoff_ms", 0) or 0)
                day_target_rows.append(
                    {
                        "symbol": symbol,
                        "exchange": candidate.get("exchange", ""),
                        "date": trade_date,
                        "direction_bias": candidate.get("direction_bias", "neutral"),
                        "score": round(float(candidate.get("score", 0) or 0), 4),
                        "scan_reason": candidate.get("scan_reason", ""),
                        "status": "active",
                        "rank": rank,
                        "us_time": format_us_time(cutoff_ms) if cutoff_ms > 0 else f"{trade_date} {request.get('premarket_cutoff_time') or DEFAULT_SCAN_CUTOFF_TIME}",
                        "cn_time": format_cn_time(cutoff_ms) if cutoff_ms > 0 else "",
                        "bar_time_ms": cutoff_ms,
                        "environment": BACKTEST_ENVIRONMENT,
                        "extra": {
                            **(candidate.get("extra") or {}),
                            "source_environment": request["source_environment"],
                            "selection_rank": rank,
                            "universe_mode": universe_mode,
                            "universe_snapshot_fallback": universe_snapshot_fallback,
                            "universe_size": scanned_count,
                            **build_runtime_timestamps(),
                        },
                    }
                )
            target_rows.extend(day_target_rows)
            daily_summary = {
                "date": trade_date,
                "universe_size": scanned_count,
                "universe_snapshot_fallback": universe_snapshot_fallback,
                "ready_symbol_count": ready_count,
                "quality_rejected_count": quality_rejected_count,
                "rejection_summary": rejection_summary,
                "candidate_count": pre_sd_candidate_count,
                "post_sd_candidate_count": len(day_candidates),
                **sd_summary,
                "selected_count": len(selected_rows),
                "selected_symbols": [item["symbol"] for item in selected_rows],
                "cache_hit": False,
                "cache_status": "rebuilt" if cache_summary.get("enabled") else "disabled",
            }
            daily_summaries.append(daily_summary)
            if cache_summary.get("enabled"):
                cache_summary["rebuilt_days"] = int(cache_summary.get("rebuilt_days", 0) or 0) + 1
                if cache_write_enabled and bool(input_fingerprint.get("usable")):
                    cache_payload = {
                        "cache_key": cache_fingerprint["cache_key"],
                        "market_date": trade_date,
                        "source_environment": str(request.get("source_environment") or "live"),
                        "algorithm_version": DAILY_SELECTION_CACHE_ALGORITHM_VERSION,
                        "request_hash": cache_fingerprint["request_hash"],
                        "universe_hash": universe_hash,
                        "input_data_hash": str(input_fingerprint.get("hash") or ""),
                        "scan_settings_hash": cache_fingerprint["scan_settings_hash"],
                        "strategy_hash": cache_fingerprint["strategy_hash"],
                        "status": "valid",
                        "selected_count": len(selected_rows),
                        "target_rows": day_target_rows,
                        "selection_plan": {trade_date: [item["symbol"] for item in selected_rows]},
                        "daily_summary": daily_summary,
                        "fingerprint_extra": {
                            **dict(cache_fingerprint.get("fingerprint_extra") or {}),
                            "universe_hash": universe_hash,
                            "input_data": {
                                key: value
                                for key, value in dict(input_fingerprint or {}).items()
                                if key in {"usable", "hash", "reason", "row_count", "missing_symbol_count", "bad_row_count"}
                            },
                        },
                        "error": "",
                        "last_built_at": datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S"),
                    }
                    write_result = self._upsert_daily_selection_cache_record(cache_payload)
                    if bool(write_result.get("ok", True)) and not write_result.get("error"):
                        cache_summary["written_days"] = int(cache_summary.get("written_days", 0) or 0) + 1
                    else:
                        cache_summary["write_error_days"] = int(cache_summary.get("write_error_days", 0) or 0) + 1
                        self._increment_daily_selection_cache_reason(cache_summary, f"write_error:{write_result.get('error', 'unknown')}")

        cache_summary["duration_s"] = round(time.time() - cache_started_at, 3)
        cache_summary["total_days"] = len(trading_dates)
        cache_summary["hit_rate"] = round(
            float(cache_summary.get("hit_days", 0) or 0) / max(1, len(trading_dates)) * 100.0,
            4,
        )
        return {
            "symbols": selected_symbols,
            "selection_plan": selection_plan,
            "target_rows": target_rows,
            "summary": {
                "mode": "daily_scan_replay",
                "target_date_count": len(trading_dates),
                "selected_symbol_count": len(selected_symbols),
                "target_row_count": len(target_rows),
                "premarket_cutoff_time": request.get("premarket_cutoff_time") or DEFAULT_SCAN_CUTOFF_TIME,
                "scan_session_mode": request.get("scan_session_mode") or "extended",
                "scan_warmup_bars": int(request.get("scan_warmup_bars", BACKTEST_WARMUP_BARS) or BACKTEST_WARMUP_BARS),
                "effective_scan_warmup_bars": self._effective_indicator_warmup_bars(request, "scan_warmup_bars"),
                "universe_mode": universe_mode,
                "daily_selected_only": bool(request.get("daily_selected_only")),
                "daily_selection_require_sd_trigger": bool(request.get("daily_selection_require_sd_trigger")),
                "daily_selection_reuse_live_admission": bool(request.get("daily_selection_reuse_live_admission")),
                "daily_selection_sd_mode": self._daily_selection_sd_mode(request),
                "daily_selection_candidate_limit": int(request.get("daily_selection_candidate_limit", 0) or 0),
                "shared_admission_helper": "ibkr_compute.core.active_window_admission.is_active_window_admitted",
                "daily_selection_cache": cache_summary,
                "daily": daily_summaries,
            },
        }

    def _invert_selection_plan(self, selection_plan: dict[str, list[str]]) -> dict[str, set[str]]:
        inverted = {}
        for trade_date, symbols in (selection_plan or {}).items():
            for symbol in list(symbols or []):
                symbol_text = str(symbol or "").strip().upper()
                if not symbol_text:
                    continue
                inverted.setdefault(symbol_text, set()).add(str(trade_date or ""))
        return inverted

    def _build_daily_target_lookup(self, target_rows: list[dict], symbols: list[str]) -> dict[tuple[str, str], dict]:
        lookup: dict[tuple[str, str], dict] = {}
        fallback_rank = {str(symbol or "").strip().upper(): index for index, symbol in enumerate(symbols or [], start=1)}
        for row in target_rows or []:
            symbol = str(row.get("symbol", "") or "").strip().upper()
            date_text = str(row.get("date", "") or "")[:10]
            if not symbol or not date_text:
                continue
            extra = self._parse_object(row.get("extra"))
            rank = int(row.get("rank", extra.get("selection_rank", fallback_rank.get(symbol, 999999))) or fallback_rank.get(symbol, 999999))
            score = float(row.get("score", 0) or 0)
            strategy_policy = extra.get("strategy_policy") if isinstance(extra.get("strategy_policy"), dict) else {}
            recommended_exit_policy = (
                (strategy_policy or {}).get("recommended_exit_policy")
                if isinstance((strategy_policy or {}).get("recommended_exit_policy"), dict)
                else {}
            )
            symbol_profile = extra.get("symbol_profile") if isinstance(extra.get("symbol_profile"), dict) else {}
            lookup[(date_text, symbol)] = {
                "rank": rank,
                "score": score,
                "direction_bias": str(row.get("direction_bias", "") or ""),
                "strategy_policy": dict(strategy_policy or {}),
                "recommended_exit_policy": dict(recommended_exit_policy or {}),
                "symbol_profile": dict(symbol_profile or {}),
            }
        for symbol, rank in fallback_rank.items():
            lookup.setdefault(
                ("", symbol),
                {
                    "rank": rank,
                    "score": 0.0,
                    "direction_bias": "",
                    "strategy_policy": {},
                    "recommended_exit_policy": {},
                    "symbol_profile": {},
                },
            )
        return lookup

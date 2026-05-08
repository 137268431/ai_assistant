from __future__ import annotations

from .runtime_support import *
from .watchlist_universe import merge_trade_watchlist_rows, request_excluded_symbols


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
        warmup_limit = int(request.get("warmup_bars", BACKTEST_WARMUP_BARS) or BACKTEST_WARMUP_BARS)
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
        if not bool(request.get("daily_selection_require_sd_trigger")) and not bool(request.get("daily_selection_reuse_live_admission")):
            return day_candidates, {
                "enabled": False,
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
                rejected_count += 1
                reason = str(admission.get("reason") or "sd_rejected").strip() or "sd_rejected"
                rejection_summary[reason] = int(rejection_summary.get(reason, 0) or 0) + 1

        return admitted, {
            "enabled": True,
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

    def _load_historical_scan_settings(self, source_environment: str) -> dict:
        try:
            return _load_scan_settings(source_environment)
        except Exception:
            return {
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

    def _build_historical_scan_engines(self, symbol: str, request: dict, cutoff_ms: int) -> tuple[dict, dict]:
        params = dict((request.get("params") or {}).get("strategy_params") or DEFAULT_PARAMS)
        environment = request["source_environment"]
        session_mode = request.get("scan_session_mode") or "extended"
        lookback_limit = int(request.get("scan_warmup_bars", BACKTEST_WARMUP_BARS) or BACKTEST_WARMUP_BARS)
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
        metric_row = self._build_historical_scan_metric_row(symbol, trade_date, request, cutoff_ms, None)
        scan_settings = settings or self._load_historical_scan_settings(request["source_environment"])
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
        metric_row = self._build_historical_scan_metric_row(symbol, trade_date, request, cutoff_ms, engines)
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
            day_candidates = []
            scanned_count = 0
            ready_count = 0
            quality_rejected_count = 0
            rejection_summary: dict[str, int] = {}
            scan_settings = self._load_historical_scan_settings(request["source_environment"])
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
            for rank, candidate in enumerate(selected_rows, start=1):
                symbol = candidate["symbol"]
                if symbol not in selected_lookup:
                    selected_lookup.add(symbol)
                    selected_symbols.append(symbol)
                cutoff_ms = int((candidate.get("extra") or {}).get("scan_cutoff_ms", 0) or 0)
                target_rows.append(
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
            daily_summaries.append(
                {
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
                }
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
                "universe_mode": universe_mode,
                "daily_selected_only": bool(request.get("daily_selected_only")),
                "daily_selection_require_sd_trigger": bool(request.get("daily_selection_require_sd_trigger")),
                "daily_selection_reuse_live_admission": bool(request.get("daily_selection_reuse_live_admission")),
                "daily_selection_candidate_limit": int(request.get("daily_selection_candidate_limit", 0) or 0),
                "shared_admission_helper": "ibkr_compute.core.active_window_admission.is_active_window_admitted",
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
            lookup[(date_text, symbol)] = {
                "rank": rank,
                "score": score,
                "direction_bias": str(row.get("direction_bias", "") or ""),
            }
        for symbol, rank in fallback_rank.items():
            lookup.setdefault(("", symbol), {"rank": rank, "score": 0.0, "direction_bias": ""})
        return lookup

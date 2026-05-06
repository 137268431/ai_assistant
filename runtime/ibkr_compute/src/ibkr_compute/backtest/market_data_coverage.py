from __future__ import annotations

from .runtime_support import *


class BacktestMarketDataCoverageMixin:
    def _date_to_ms_range(self, date_from: str, date_to: str) -> tuple[int, int]:
        start = datetime.strptime(date_from, "%Y-%m-%d").replace(tzinfo=ET)
        end = datetime.strptime(date_to, "%Y-%m-%d").replace(tzinfo=ET) + timedelta(days=1) - timedelta(milliseconds=1)
        return int(start.timestamp() * 1000), int(end.timestamp() * 1000)

    def _session_edge_grace_ms(self, days: int = BACKTEST_COVERAGE_EDGE_GRACE_DAYS) -> int:
        return max(0, int(days or 0)) * 24 * 60 * 60 * 1000

    def _symbol_range_coverage_summary(
        self,
        symbol: str,
        source_environment: str,
        date_from: str,
        date_to: str,
        *,
        warmup_bars: int = BACKTEST_WARMUP_BARS,
        refresh_daily_coverage: bool = False,
    ) -> dict:
        start_ms, end_ms = self._date_to_ms_range(date_from, date_to)
        warmup_lookback_ms = interval_to_ms("5m") * max(0, int(warmup_bars or 0), BACKTEST_WARMUP_BARS)
        requested_start_ms = max(0, start_ms - warmup_lookback_ms)
        daily_summary = self._symbol_range_coverage_summary_from_daily_coverage(
            symbol,
            source_environment,
            start_ms,
            end_ms,
            refresh=refresh_daily_coverage,
        )
        if daily_summary is not None:
            return daily_summary
        sqlite_summary = self._symbol_range_coverage_summary_from_sqlite(
            symbol,
            source_environment,
            requested_start_ms,
            end_ms,
        )
        if sqlite_summary is not None:
            return sqlite_summary

        rows = self._load_bar_rows_from_sqlite(
            symbol,
            source_environment,
            start_ms=requested_start_ms,
            end_ms=end_ms,
            descending=False,
        )
        first_bar_ms = int(rows[0].get("bar_time_ms", 0) or 0) if rows else 0
        last_bar_ms = int(rows[-1].get("bar_time_ms", 0) or 0) if rows else 0
        gap_count = self._count_internal_5m_gaps(rows) if rows else 0
        edge_grace_ms = self._session_edge_grace_ms()
        missing_start = not rows or first_bar_ms <= 0 or first_bar_ms > requested_start_ms + edge_grace_ms
        missing_end = not rows or last_bar_ms <= 0 or last_bar_ms < end_ms - edge_grace_ms
        needs_backfill = bool(not rows or missing_start or missing_end or gap_count > 0)
        repair_windows = self._build_backfill_repair_windows(rows, requested_start_ms, end_ms) if needs_backfill else []
        reasons = []
        if not rows:
            reasons.append("no_rows")
        if missing_start:
            reasons.append("missing_start")
        if missing_end:
            reasons.append("missing_end")
        if gap_count > 0:
            reasons.append("internal_gaps")
        return {
            "symbol": symbol,
            "needs_backfill": needs_backfill,
            "reasons": reasons,
            "row_count": len(rows),
            "gap_count": gap_count,
            "first_bar_ms": first_bar_ms,
            "last_bar_ms": last_bar_ms,
            "first_bar_us": format_us_time(first_bar_ms) if first_bar_ms > 0 else "",
            "last_bar_us": format_us_time(last_bar_ms) if last_bar_ms > 0 else "",
            "requested_start_ms": requested_start_ms,
            "requested_end_ms": end_ms,
            "requested_start_us": format_us_time(requested_start_ms) if requested_start_ms > 0 else "",
            "requested_end_us": format_us_time(end_ms) if end_ms > 0 else "",
            "repair_window_count": len(repair_windows),
            "repair_windows": repair_windows[:20],
            "repair_windows_truncated": max(0, len(repair_windows) - 20),
            "diagnostic_source": "python_rows",
        }

    def _symbol_range_coverage_summary_from_daily_coverage(
        self,
        symbol: str,
        source_environment: str,
        requested_start_ms: int,
        requested_end_ms: int,
        *,
        refresh: bool = False,
    ) -> dict | None:
        db_path = str(runtime_backtest_sqlite_path() or "").strip()
        if not db_path or not os.path.exists(db_path):
            return None

        normalized_symbol = str(symbol or "").strip().upper()
        if not normalized_symbol:
            return None
        date_from = market_date_from_ms(int(requested_start_ms))
        date_to = market_date_from_ms(int(requested_end_ms))
        expected_dates = trading_date_strings_from_ms(int(requested_start_ms), int(requested_end_ms))
        if not expected_dates:
            return None

        try:
            with sqlite3.connect(db_path, timeout=20) as conn:
                conn.row_factory = sqlite3.Row
                existing_rows = load_daily_coverage_rows(
                    conn,
                    symbols=[normalized_symbol],
                    environment=source_environment,
                    date_from=date_from,
                    date_to=date_to,
                    interval="5m",
                    session_mode="regular",
                )
                by_date = {str(row.get("market_date") or ""): dict(row) for row in existing_rows}
                dates_to_rebuild = []
                for market_date in expected_dates:
                    row = by_date.get(market_date)
                    status = str((row or {}).get("status") or "").strip().lower()
                    if refresh or row is None or status not in DAILY_COVERAGE_OK_STATUSES:
                        dates_to_rebuild.append(market_date)

                if dates_to_rebuild:
                    rebuilt_rows = build_range_daily_coverage(
                        conn,
                        symbols=[normalized_symbol],
                        environment=source_environment,
                        date_from=min(dates_to_rebuild),
                        date_to=max(dates_to_rebuild),
                        interval="5m",
                        session_modes=("regular",),
                        source="backtest_preflight",
                        date_filter=dates_to_rebuild,
                    )
                    if rebuilt_rows:
                        with conn:
                            upsert_bar_coverage_daily(conn, rebuilt_rows)
                        for row in rebuilt_rows:
                            by_date[str(row.get("market_date") or "")] = dict(row)

                rows = [by_date[market_date] for market_date in expected_dates if market_date in by_date]
                summary = summarize_symbol_daily_coverage(
                    symbol=normalized_symbol,
                    rows=rows,
                    requested_start_ms=int(requested_start_ms),
                    requested_end_ms=int(requested_end_ms),
                    interval="5m",
                )
                summary["daily_coverage"]["rebuilt_dates"] = dates_to_rebuild[:20]
                summary["daily_coverage"]["rebuilt_dates_truncated"] = max(0, len(dates_to_rebuild) - 20)
                return summary
        except sqlite3.OperationalError:
            return None
        except Exception:
            traceback.print_exc()
            return None

    def _symbol_range_coverage_summary_from_sqlite(
        self,
        symbol: str,
        source_environment: str,
        requested_start_ms: int,
        requested_end_ms: int,
    ) -> dict | None:
        db_path = str(runtime_backtest_sqlite_path() or "").strip()
        if not db_path or not os.path.exists(db_path):
            return None

        normalized_interval = "5m"
        interval_ms = interval_to_ms(normalized_interval)
        try:
            with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5) as conn:
                conn.row_factory = sqlite3.Row
                summary = conn.execute(
                    """
                    SELECT COUNT(*) AS row_count,
                           MIN(bar_time_ms) AS first_bar_ms,
                           MAX(bar_time_ms) AS last_bar_ms,
                           SUM(CASE WHEN COALESCE(us_time, '') = '' THEN 1 ELSE 0 END) AS missing_us_time_count
                    FROM ibkr_bars
                    WHERE symbol = ?
                      AND interval = ?
                      AND environment = ?
                      AND bar_time_ms >= ?
                      AND bar_time_ms <= ?
                    """,
                    (
                        symbol,
                        normalized_interval,
                        source_environment,
                        int(requested_start_ms),
                        int(requested_end_ms),
                    ),
                ).fetchone()
                if summary is None:
                    return None

                row_count = int(summary["row_count"] or 0)
                missing_us_time_count = int(summary["missing_us_time_count"] or 0)
                if row_count > 0 and missing_us_time_count > 0:
                    return None

                first_bar_ms = int(summary["first_bar_ms"] or 0)
                last_bar_ms = int(summary["last_bar_ms"] or 0)
                raw_windows: list[dict] = []
                if row_count <= 0:
                    raw_windows.append(
                        {
                            "start_ms": int(requested_start_ms),
                            "end_ms": int(requested_end_ms),
                            "reason": "no_rows",
                        }
                    )
                    gap_count = 0
                else:
                    edge_grace_ms = self._session_edge_grace_ms()
                    if first_bar_ms > int(requested_start_ms) + edge_grace_ms:
                        raw_windows.append(
                            {
                                "start_ms": int(requested_start_ms),
                                "end_ms": max(0, first_bar_ms - interval_ms),
                                "reason": "missing_start",
                            }
                        )
                    if last_bar_ms < int(requested_end_ms) - edge_grace_ms:
                        raw_windows.append(
                            {
                                "start_ms": last_bar_ms + interval_ms,
                                "end_ms": int(requested_end_ms),
                                "reason": "missing_end",
                            }
                        )
                    gap_rows = conn.execute(
                        """
                        WITH ordered AS (
                            SELECT bar_time_ms,
                                   substr(us_time, 1, 10) AS us_day,
                                   LAG(bar_time_ms) OVER (
                                       PARTITION BY substr(us_time, 1, 10)
                                       ORDER BY bar_time_ms
                                   ) AS prev_ms
                            FROM ibkr_bars
                            WHERE symbol = ?
                              AND interval = ?
                              AND environment = ?
                              AND bar_time_ms >= ?
                              AND bar_time_ms <= ?
                        )
                        SELECT prev_ms + ? AS start_ms,
                               bar_time_ms - ? AS end_ms,
                               'internal_gap' AS reason
                        FROM ordered
                        WHERE prev_ms IS NOT NULL
                          AND us_day != ''
                          AND bar_time_ms - prev_ms > ?
                        ORDER BY start_ms
                        LIMIT 500
                        """,
                        (
                            symbol,
                            normalized_interval,
                            source_environment,
                            int(requested_start_ms),
                            int(requested_end_ms),
                            interval_ms,
                            interval_ms,
                            interval_ms * 3,
                        ),
                    ).fetchall()
                    gap_count = len(gap_rows)
                    raw_windows.extend(dict(row) for row in gap_rows)
        except Exception:
            traceback.print_exc()
            return None

        repair_windows = self._merge_backfill_repair_windows(raw_windows)
        missing_start = any("missing_start" in str(item.get("reason") or "") for item in repair_windows)
        missing_end = any("missing_end" in str(item.get("reason") or "") for item in repair_windows)
        needs_backfill = bool(row_count <= 0 or missing_start or missing_end or gap_count > 0)
        reasons = []
        if row_count <= 0:
            reasons.append("no_rows")
        if missing_start:
            reasons.append("missing_start")
        if missing_end:
            reasons.append("missing_end")
        if gap_count > 0:
            reasons.append("internal_gaps")
        return {
            "symbol": symbol,
            "needs_backfill": needs_backfill,
            "reasons": reasons,
            "row_count": row_count,
            "gap_count": gap_count,
            "first_bar_ms": first_bar_ms,
            "last_bar_ms": last_bar_ms,
            "first_bar_us": format_us_time(first_bar_ms) if first_bar_ms > 0 else "",
            "last_bar_us": format_us_time(last_bar_ms) if last_bar_ms > 0 else "",
            "requested_start_ms": int(requested_start_ms),
            "requested_end_ms": int(requested_end_ms),
            "requested_start_us": format_us_time(int(requested_start_ms)) if int(requested_start_ms) > 0 else "",
            "requested_end_us": format_us_time(int(requested_end_ms)) if int(requested_end_ms) > 0 else "",
            "repair_window_count": len(repair_windows),
            "repair_windows": repair_windows[:20],
            "repair_windows_truncated": max(0, len(repair_windows) - 20),
            "diagnostic_source": "sqlite_window",
        }

    def _count_internal_5m_gaps(self, rows: list[dict]) -> int:
        gap_count = 0
        previous_ms = 0
        previous_day = ""
        for row in rows or []:
            bar_ms = int((row or {}).get("bar_time_ms", 0) or 0)
            if bar_ms <= 0:
                continue
            current_day = str((row or {}).get("us_time", "") or "")[:10] or ms_to_et(bar_ms).strftime("%Y-%m-%d")
            if previous_ms > 0 and previous_day == current_day:
                if bar_ms - previous_ms > interval_to_ms("5m") * 3:
                    gap_count += 1
            previous_ms = bar_ms
            previous_day = current_day
        return gap_count

    def _merge_backfill_repair_windows(self, raw_windows: list[dict]) -> list[dict]:
        interval_ms = interval_to_ms("5m")
        windows: list[dict] = []
        for window in sorted(raw_windows or [], key=lambda item: (int(item.get("start_ms", 0) or 0), int(item.get("end_ms", 0) or 0))):
            start_ms = max(0, int(window.get("start_ms", 0) or 0))
            end_ms = int(window.get("end_ms", 0) or 0)
            if start_ms <= 0 or end_ms <= 0 or end_ms < start_ms:
                continue
            if windows and start_ms <= int(windows[-1]["end_ms"]) + interval_ms:
                windows[-1]["end_ms"] = max(int(windows[-1]["end_ms"]), end_ms)
                reasons = set(str(windows[-1].get("reason") or "").split(","))
                reasons.add(str(window.get("reason") or ""))
                windows[-1]["reason"] = ",".join(sorted(item for item in reasons if item))
                continue
            windows.append(
                {
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "reason": str(window.get("reason") or "missing"),
                }
            )
        for window in windows:
            window["start_us"] = format_us_time(int(window["start_ms"]))
            window["end_us"] = format_us_time(int(window["end_ms"]))
        return windows

    def _build_backfill_repair_windows(
        self,
        rows: list[dict],
        requested_start_ms: int,
        requested_end_ms: int,
    ) -> list[dict]:
        interval_ms = interval_to_ms("5m")
        edge_grace_ms = self._session_edge_grace_ms()
        raw_windows: list[dict] = []

        def add_window(start_ms: int, end_ms: int, reason: str):
            start_ms = max(0, int(start_ms or 0))
            end_ms = int(end_ms or 0)
            if start_ms <= 0 or end_ms <= 0 or end_ms < start_ms:
                return
            raw_windows.append(
                {
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "reason": reason,
                }
            )

        sorted_rows = sorted(
            [row for row in rows or [] if int((row or {}).get("bar_time_ms", 0) or 0) > 0],
            key=lambda item: int(item.get("bar_time_ms", 0) or 0),
        )
        if not sorted_rows:
            add_window(requested_start_ms, requested_end_ms, "no_rows")
            return self._merge_backfill_repair_windows(raw_windows)

        first_bar_ms = int(sorted_rows[0].get("bar_time_ms", 0) or 0)
        last_bar_ms = int(sorted_rows[-1].get("bar_time_ms", 0) or 0)
        if first_bar_ms > requested_start_ms + edge_grace_ms:
            add_window(requested_start_ms, first_bar_ms - interval_ms, "missing_start")
        if last_bar_ms < requested_end_ms - edge_grace_ms:
            add_window(last_bar_ms + interval_ms, requested_end_ms, "missing_end")

        previous_ms = 0
        previous_day = ""
        for row in sorted_rows:
            bar_ms = int((row or {}).get("bar_time_ms", 0) or 0)
            current_day = str((row or {}).get("us_time", "") or "")[:10] or ms_to_et(bar_ms).strftime("%Y-%m-%d")
            if previous_ms > 0 and previous_day == current_day and bar_ms - previous_ms > interval_ms * 3:
                add_window(previous_ms + interval_ms, bar_ms - interval_ms, "internal_gap")
            previous_ms = bar_ms
            previous_day = current_day

        return self._merge_backfill_repair_windows(raw_windows)

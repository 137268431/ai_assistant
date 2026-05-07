"""
配置管理 — 从 PB config 表读取配置
"""

from ibkr_compute.integrations.pb_client import PBClient


class Config:
    DEFAULTS = {
        "ibkr_compute_enabled": "true",
        "ibkr_bar_publish_enabled": "true",
        "ibkr_market_ws_enabled": "true",
        "ibkr_market_ws_symbols": "SPY,QQQ,VIX",
        "ibkr_target_filter_on": "false",
        "ibkr_target_subscription_limit": "80",
        "ibkr_total_subscription_limit": "80",
        "ibkr_target_refresh_sec": "60",
        "ibkr_live_warmup_days": "14",
        "ibkr_warmup_indicator_intervals": "5m,15m,30m,1h,4h,1d",
        "ibkr_warmup_indicator_buffer_days": "5",
        "ibkr_warmup_indicator_calendar_multiplier": "1.4",
        "ibkr_warmup_indicator_max_days": "60",
        "ibkr_warmup_indicator_regular_minutes_per_day": "390",
        "ibkr_warmup_indicator_backfill_enabled": "true",
        "ibkr_warmup_indicator_backfill_intervals": "15m,30m,1h,4h,1d",
        "ibkr_warmup_indicator_backfill_passes": "2",
        "ibkr_warmup_indicator_prime_retries": "3",
        "ibkr_warmup_indicator_prime_retry_delay_sec": "5",
        "ibkr_warmup_required_5m_period": "4d",
        "ibkr_restart_overlap_days": "1",
        "ibkr_manual_start_restart_gateway": "true",
        "ibkr_weekly_reauth_restart_gateway": "true",
        "ibkr_server_boot_resume_only": "true",
        "ibkr_server_boot_publish_startup_card": "true",
        "ibkr_active_repair_interval_min": "5",
        "ibkr_watchlist_backfill_interval_min": "30",
        "ibkr_watchlist_backfill_batch_size": "12",
        "ibkr_watchlist_backfill_stale_min": "20",
        "ibkr_watchlist_idle_topup_enabled": "true",
        "ibkr_watchlist_idle_topup_loop_interval_sec": "60",
        "ibkr_watchlist_idle_topup_dynamic_enabled": "true",
        "ibkr_watchlist_idle_topup_active_first_enabled": "true",
        "ibkr_watchlist_idle_topup_dynamic_loop_interval_sec": "15",
        "ibkr_watchlist_idle_topup_dynamic_max_symbols_per_cycle": "24",
        "ibkr_watchlist_idle_topup_max_estimated_bars_per_cycle": "3000",
        "ibkr_watchlist_idle_topup_batch_size": "8",
        "ibkr_watchlist_idle_topup_max_symbols_per_cycle": "0",
        "ibkr_watchlist_idle_topup_candidate_scan_size": "24",
        "ibkr_watchlist_idle_topup_request_period": "1d",
        "ibkr_watchlist_idle_topup_stale_min": "20",
        "ibkr_watchlist_idle_topup_materialize_5m": "false",
        "ibkr_watchlist_idle_topup_progress_warn_sec": "7200",
        "ibkr_watchlist_active_due_guard_sec": "45",
        "ibkr_watchlist_integrity_enabled": "true",
        "ibkr_watchlist_integrity_batch_size": "8",
        "ibkr_host_resource_monitor_enabled": "true",
        "ibkr_host_resource_monitor_interval_sec": "5",
        "ibkr_host_resource_monitor_stale_sec": "30",
        "ibkr_resource_governor_watchlist_cpu_5m_max_pct": "60",
        "ibkr_resource_governor_watchlist_load5_max": "2.8",
        "ibkr_resource_governor_watchlist_mem_available_min_mb": "2048",
        "ibkr_resource_governor_watchlist_mem_available_min_pct": "25",
        "ibkr_resource_governor_watchlist_disk_free_min_gb": "15",
        "ibkr_resource_governor_watchlist_disk_free_min_pct": "20",
        "ibkr_resource_governor_watchlist_iowait_max_pct": "8",
        "ibkr_resource_governor_warning_cpu_5m_pct": "70",
        "ibkr_resource_governor_warning_mem_available_mb": "1536",
        "ibkr_resource_governor_warning_disk_free_pct": "15",
        "ibkr_resource_governor_critical_cpu_5m_pct": "85",
        "ibkr_resource_governor_critical_mem_available_mb": "1024",
        "ibkr_resource_governor_critical_disk_free_pct": "10",
        "ibkr_resource_governor_critical_iowait_pct": "20",
        "ibkr_history_repair_enabled": "true",
        "ibkr_history_retention_enabled": "true",
        "ibkr_history_retention_days": "365",
        "ibkr_history_repair_min_bars_5m": "260",
        "ibkr_history_repair_gap_lookback": "80",
        "ibkr_history_repair_rollup_enabled": "false",
        "ibkr_bar_repair_max_concurrency": "2",
        "ibkr_bar_repair_request_spacing": "1.0",
        "ibkr_bar_repair_max_retries": "3",
        "ibkr_bar_repair_period_5m": "4d",
        "ibkr_bar_repair_period_15m": "10d",
        "ibkr_bar_repair_period_30m": "20d",
        "ibkr_bar_repair_period_1h": "40d",
        "ibkr_bar_repair_period_4h": "120d",
        "ibkr_bar_repair_period_1d": "2y",
        "ibkr_history_max_concurrency": "10",
        "ibkr_history_request_spacing": "0.15",
        "ibkr_history_interval_delay": "0.10",
        "ibkr_history_max_retries": "4",
        "ibkr_history_retry_base_delay": "2.0",
        "ibkr_history_trace_enabled": "true",
        "ibkr_history_trace_recent_limit": "20",
        "ibkr_history_trace_slow_sec": "2.0",
        "ibkr_history_trace_log_all_requests": "true",
        "ibkr_history_chunked_backfill_enabled": "true",
        "ibkr_history_chunk_days_5m": "4",
        "ibkr_history_chunk_days_15m": "14",
        "ibkr_history_chunk_days_30m": "30",
        "ibkr_history_chunk_days_1h": "60",
        "ibkr_history_chunk_days_4h": "120",
        "ibkr_startup_direct_backfill_enabled": "true",
        "ibkr_startup_direct_backfill_intervals": "5m,15m,30m,1h,4h,1d",
        "ibkr_startup_direct_backfill_required_bars": "260",
        "ibkr_startup_direct_backfill_period_5m": "4d",
        "ibkr_startup_direct_backfill_period_15m": "10d",
        "ibkr_startup_direct_backfill_period_30m": "20d",
        "ibkr_startup_direct_backfill_period_1h": "40d",
        "ibkr_startup_direct_backfill_period_4h": "120d",
        "ibkr_startup_direct_backfill_period_1d": "2y",
        "ibkr_startup_direct_backfill_client_id": "9131",
        "ibkr_startup_direct_backfill_resolve_missing_conids_enabled": "false",
        "ibkr_startup_direct_backfill_allow_live_conid_resolution": "false",
        "ibkr_startup_direct_backfill_conid_scan_intervals": "5m,15m,30m,1h,4h,1d",
        "ibkr_startup_direct_backfill_conid_scan_pages": "3",
        "ibkr_official_5m_max_concurrency": "8",
        "ibkr_official_5m_parallel_enabled": "true",
        "ibkr_runtime_direct_topup_enabled": "true",
        "ibkr_runtime_direct_topup_intervals": "15m,30m,1h,4h,1d",
        "ibkr_runtime_direct_topup_close_delay_sec": "30",
        "ibkr_runtime_direct_topup_loop_interval_sec": "5",
        "ibkr_runtime_direct_topup_period_15m": "2d",
        "ibkr_runtime_direct_topup_period_30m": "3d",
        "ibkr_runtime_direct_topup_period_1h": "5d",
        "ibkr_runtime_direct_topup_period_4h": "20d",
        "ibkr_runtime_direct_topup_period_1d": "60d",
        "signal_validity_minutes": "30",
        "signal_window_max_bars": "12",
        "ibkr_market_sentiment_enabled": "true",
        "ibkr_market_sentiment_mode": "annotate",
        "ibkr_market_sentiment_symbols": "VIX,SPY,QQQ",
        "ibkr_market_sentiment_stale_min": "20",
        "ibkr_market_sentiment_vix_calm_max": "20",
        "ibkr_market_sentiment_vix_risk_off": "25",
        "ibkr_market_sentiment_vix_panic": "30",
        "ibkr_market_sentiment_vix_reversal_floor": "20",
        "ibkr_market_sentiment_vix_falling_delta": "-0.05",
        "cooldown_bars_after_sl": "6",
        "cooldown_bars_after_reverse": "3",
        "atr_dynamic_stop_enabled": "true",
        "atr_stop_min_profit_r": "0.3",
        "atr_stop_deviation_threshold": "0.30",
        "atr_stop_min_change": "0.01",
        "reverse_flip_enabled": "false",
        "trade_window_start_time": "09:35",
        "trade_window_end_time": "15:30",
        "order_window_end_time": "15:00",
        "position_limit_max": "3",
        "eod_close_time": "15:55",
        "eod_keep_symbols": "",
        "signal_poll_interval_sec": "120",
        "ibkr_bar_batch_size": "40",
        "ibkr_bar_flush_interval": "2.0",
        "ibkr_bar_flush_retry_attempts": "4",
        "ibkr_bar_flush_retry_backoff_seconds": "1.0",
        "ibkr_bar_direct_sqlite_enabled": "true",
        "ibkr_bar_direct_sqlite_fallback_api_enabled": "true",
        "ibkr_bar_direct_sqlite_timeout_sec": "30.0",
        "ibkr_bar_direct_sqlite_read_enabled": "true",
        "ibkr_bar_direct_sqlite_read_fallback_api_enabled": "true",
        "ibkr_bar_direct_sqlite_read_timeout_sec": "30.0",
        "ibkr_indicator_direct_sqlite_enabled": "true",
        "ibkr_ws_ping_interval_sec": "45",
        "ibkr_ws_resubscribe_batch_size": "8",
        "ibkr_ws_resubscribe_gap_ms": "150",
        "ibkr_order_updates_mode": "hybrid",
        "ibkr_order_poll_interval_active_sec": "5",
        "ibkr_order_poll_interval_idle_sec": "15",
        "ibkr_order_fast_track_sec": "30",
        "ibkr_order_question_suppress_enabled": "false",
        "ibkr_order_question_suppress_message_ids": "",
        "ibkr_scan_schedule": "09:20-10:00",
        "ibkr_daily_scan_time_et": "09:20",
        "ibkr_daily_scan_min_avg_10d_volume": "100000",
        "ibkr_daily_scan_min_atr_pct": "0.15",
        "ibkr_daily_scan_min_abs_day_change_pct": "1.0",
        "ibkr_daily_scan_min_premarket_volume": "5000",
        "ibkr_daily_scan_data_completeness_enabled": "true",
        "ibkr_daily_scan_data_completeness_blocking_enabled": "false",
        "ibkr_daily_scan_data_completeness_intervals": "5m",
        "ibkr_daily_scan_indicator_snapshot_enabled": "true",
        "ibkr_daily_scan_indicator_snapshot_intervals": "5m",
        "ibkr_daily_scan_materialize_enabled": "false",
        "ibkr_daily_scan_materialize_intervals": "5m",
        "ibkr_daily_scan_auto_retry_enabled": "true",
        "ibkr_daily_scan_retry_delays_sec": "60,120,240",
        "ibkr_daily_scan_retry_stall_timeout_sec": "480",
        "ibkr_daily_scan_preload_timeout_sec": "900",
        "ibkr_publish_batch_size": "10",
        "ibkr_signal_source": "both",
        "signal_manual_confirm_enabled": "true",
        "ibkr_trading_enabled": "true",
        "system_monitor_ws_message_age_regular_warn_sec": "60",
        "system_monitor_ws_message_age_regular_critical_sec": "180",
        "system_monitor_ws_message_age_late_session_warn_sec": "600",
        "system_monitor_ws_message_age_late_session_critical_sec": "1200",
    }

    def __init__(self, pb_client: PBClient = None):
        self.pb_client = pb_client
        self._cache = dict(self.DEFAULTS)
        self._records_by_key = {}
        self._last_refresh = 0

    def refresh(self):
        if not self.pb_client:
            return
        try:
            records = self.pb_client.get_runtime_config()
            self._cache = dict(self.DEFAULTS)
            self._records_by_key = {}
            applied_global_keys = set()
            for r in records:
                key = r.get("key", "")
                value = r.get("value", "")
                environment = str(r.get("environment", "") or "").strip().lower()
                if key:
                    self._records_by_key.setdefault(key, []).append(r)
                if key and value and environment in ("", "global") and key not in applied_global_keys:
                    self._cache[key] = value
                    applied_global_keys.add(key)
        except Exception as e:
            print(f"[Config] refresh error: {e}")

    def get(self, key: str, default: str = None) -> str:
        return self._cache.get(key, default or self.DEFAULTS.get(key, ""))

    def get_bool(self, key: str, default: bool = False) -> bool:
        val = self.get(key, str(default).lower())
        return val.lower() in ("true", "1", "yes")

    def get_int(self, key: str, default: int = 0) -> int:
        try:
            return int(self.get(key, str(default)))
        except (ValueError, TypeError):
            return default

    def get_float(self, key: str, default: float = 0.0) -> float:
        try:
            return float(self.get(key, str(default)))
        except (ValueError, TypeError):
            return default

    def has_environment_override(self, key: str, environment: str) -> bool:
        runtime_environment = str(environment or "").strip().lower()
        if not key or not runtime_environment:
            return False
        return any(
            str(record.get("environment", "") or "").strip().lower() == runtime_environment
            for record in self._records_by_key.get(key, [])
        )

    def get_for_environment(self, key: str, environment: str, default: str = None) -> str:
        runtime_environment = str(environment or "").strip().lower()
        fallback = default if default is not None else self.DEFAULTS.get(key, "")
        records = self._records_by_key.get(key, [])
        best_value = None
        best_rank = -1

        for record in records:
            value = record.get("value", "")
            if value in (None, ""):
                continue
            record_environment = str(record.get("environment", "") or "").strip().lower()
            rank = 2 if record_environment == runtime_environment else (1 if record_environment in ("", "global") else -1)
            if rank > best_rank:
                best_rank = rank
                best_value = value

        return best_value if best_value is not None else fallback

    def get_bool_for_environment(self, key: str, environment: str, default: bool = False) -> bool:
        val = self.get_for_environment(key, environment, str(default).lower())
        return str(val).lower() in ("true", "1", "yes")

    def get_int_for_environment(self, key: str, environment: str, default: int = 0) -> int:
        try:
            return int(self.get_for_environment(key, environment, str(default)))
        except (ValueError, TypeError):
            return default

    def get_float_for_environment(self, key: str, environment: str, default: float = 0.0) -> float:
        try:
            return float(self.get_for_environment(key, environment, str(default)))
        except (ValueError, TypeError):
            return default

    @property
    def compute_enabled(self) -> bool:
        return self.get_bool("ibkr_compute_enabled", True)

    @property
    def scan_schedule(self) -> str:
        return self.get("ibkr_scan_schedule", "09:20-10:00")

"""
配置管理 — 从 PB config 表读取配置
"""

from ibkr_compute.core.config_registry import build_config_registry, deprecated_aliases_for
from ibkr_compute.integrations.pb_client import PBClient


class Config:
    DEFAULTS = {
        "ibkr_compute_enabled": "true",
        "pb_scheduler_enabled": "true",
        "ibkr_runtime_technical_pipeline_enabled": "false",
        "ibkr_tv_primary_runtime_slim_enabled": "true",
        "pb_cron_signal_expiry_enabled": "true",
        "pb_cron_order_expiry_enabled": "true",
        "pb_cron_order_detail_integrity_guard_enabled": "true",
        "pb_cron_ibkr_compute_runtime_enabled": "false",
        "pb_cron_system_heartbeat_enabled": "false",
        "pb_cron_system_monitor_alert_guard_enabled": "true",
        "pb_cron_system_status_reminder_enabled": "true",
        "pb_cron_ibkr_scan_runtime_enabled": "false",
        "pb_cron_system_scan_summary_enabled": "true",
        "pb_cron_ibkr_active_window_progress_status_enabled": "false",
        "pb_cron_ibkr_early_expansion_topup_enabled": "false",
        "pb_cron_ibkr_intraday_window_admission_enabled": "false",
        "pb_cron_ibkr_auth_edge_guard_enabled": "true",
        "pb_cron_ibkr_auth_pending_guard_enabled": "true",
        "pb_cron_system_data_gap_guard_enabled": "false",
        "pb_cron_ibkr_fundamentals_refresh_enabled": "false",
        "pb_cron_ibkr_data_quality_repair_sweep_enabled": "false",
        "pb_cron_ibkr_data_quality_open_sweep_enabled": "false",
        "pb_cron_ibkr_data_quality_close_sweep_enabled": "false",
        "pb_cron_ibkr_data_quality_premarket_truth_audit_enabled": "false",
        "pb_cron_ibkr_data_quality_truth_audit_enabled": "false",
        "pb_cron_ibkr_tv_indicator_audit_enabled": "false",
        "pb_cron_ibkr_weekly_reauth_reminder_enabled": "true",
        "pb_cron_ibkr_weekly_reauth_followup_enabled": "true",
        "pb_cron_ibkr_2fa_hourly_check_enabled": "true",
        "pb_cron_system_market_open_reminder_enabled": "true",
        "pb_cron_system_daily_report_enabled": "true",
        "pb_cron_ibkr_history_retention_enabled": "false",
        "pb_cron_ibkr_storage_governor_enabled": "true",
        "status_notify_enabled": "true",
        "daily_summary_notify_enabled": "true",
        "health_check_notify_enabled": "true",
        "market_closed_notify_enabled": "true",
        "market_closed_notify_weekends": "true",
        "inspection_notify_enabled": "true",
        "manual_stop_notify_enabled": "true",
        "system_monitor_host_load_consecutive_count": "2",
        "system_monitor_alert_error_cooldown_min": "15",
        "system_monitor_alert_warning_cooldown_min": "60",
        "system_monitor_account_snapshot_warning_consecutive_count": "2",
        "signal_chat_id": "oc_edb26dcc52938b7833ac9f32ae6b1620",
        "order_chat_id": "oc_5ca4585e1fd108c2c662dfc358684945",
        "trade_ledger_chat_id": "oc_c5f7f750a38692b48220b8f6c58e0ac9",
        "reverse_chat_id": "oc_2931e2b8501df3a9d869d7aebceb8fe2",
        "status_chat_id": "oc_b7b52fc28816d90e27ce50ca7922a9ac",
        "system_status_chat_id": "oc_b7b52fc28816d90e27ce50ca7922a9ac",
        "system_startup_chat_id": "oc_cc5d0a950797b1c2c010953e14bceeff",
        "system_alert_chat_id": "oc_91aa4f84bc6fedb125b1a263d91d4104",
        "system_2fa_chat_id": "oc_c48c10447685e80cfea0c003864aa51f",
        "backtest_chat_id": "oc_8c4831630f2121ffe5ff9c7f72ec9e1e",
        "pb_public_url": "https://quant.lzw-glory.top",
        "pb_auth_public_url": "https://pb.lzw-glory.top",
        "ibkr_api_public_url": "https://quant.lzw-glory.top",
        "ibkr_console_public_url": "https://quant.lzw-glory.top",
        "ibkr_api_internal_url": "http://127.0.0.1:5102",
        "ibkr_compute_internal_url": "http://127.0.0.1:5100",
        "ibkr_runtime_internal_url": "http://127.0.0.1:5101",
        "ibkr_scheduler_internal_url": "http://127.0.0.1:5103",
        "tv_max_active_targets": "100",
        "tv_max_same_direction_targets": "0",
        "tv_entry_requires_active_target": "false",
        "tv_entry_requires_authorized_symbol": "true",
        "tv_primary_trade_universe_symbols": "",
        "tv_entry_allow_self_activate": "true",
        "tv_webhook_async_route_enabled": "true",
        "tv_command_reconcile_enabled": "true",
        "tv_command_reconcile_lookback_min": "1440",
        "tv_command_reconcile_max_wait_sec": "900",
        "tv_command_reconcile_pending_warn_min": "15",
        "tv_command_reconcile_conservative_resubmit": "true",
        "tv_entry_window_enforce_enabled": "true",
        "tv_entry_primary_start": "09:45",
        "tv_entry_primary_end": "11:30",
        "tv_entry_closing_start": "14:00",
        "tv_entry_quality_end": "15:15",
        "tv_primary_direct_execution_enabled": "true",
        "tv_entry_freshness_sec": "120",
        "tv_entry_limit_freshness_sec": "240",
        "tv_entry_limit_cap_bps": "15",
        "tv_entry_adaptive_enabled": "true",
        "tv_entry_adaptive_priority": "Normal",
        "tv_entry_fill_reprice_enabled": "true",
        "tv_quality_window_rank_enforce_enabled": "false",
        "tv_quality_window_max_rank": "5",
        "tv_quality_window_min_activity_score": "80",
        "tv_quality_window_min_signal_quality_score": "85",
        "tv_closing_quality_window_min_activity_score": "90",
        "tv_closing_quality_window_min_signal_quality_score": "90",
        "tv_risk_update_seq_guard_enabled": "true",
        "tv_risk_update_require_monotonic_seq": "true",
        "tv_risk_update_never_widen_stop": "true",
        "tv_risk_update_missing_child_order_retry_pending": "true",
        "tv_risk_update_retry_missing_child_orders": "true",
        "ibkr_legacy_bar_pipeline_enabled": "false",
        "ibkr_bar_publish_enabled": "true",
        "ibkr_market_ws_enabled": "true",
        "ibkr_market_ws_symbols": "SPY,QQQ,VIX",
        "ibkr_market_calendar_symbol": "SPY",
        "ibkr_market_calendar_exchange": "SMART",
        "ibkr_market_calendar_sec_type": "STK",
        "ibkr_order_flow_enabled": "false",
        "ibkr_order_flow_mode": "enforce",
        "ibkr_order_flow_active_limit": "3",
        "ibkr_order_flow_execution_pool_size": "3",
        "ibkr_order_flow_max_position_slots": "1",
        "ibkr_order_flow_tick_types": "Last",
        "ibkr_order_flow_confirm_window_sec": "60",
        "ibkr_order_flow_tbt_freshness_sec": "120",
        "ibkr_order_flow_min_delta_ratio": "0.12",
        "ibkr_order_flow_max_spread_bps": "12",
        "ibkr_order_flow_auto_entry_enabled": "true",
        "ibkr_order_flow_auto_exit_enabled": "true",
        "ibkr_order_flow_stop_tighten_enabled": "true",
        "ibkr_order_flow_entry_timeout_sec": "60",
        "ibkr_order_flow_exit_poll_sec": "2",
        "ibkr_order_flow_marketable_limit_bps": "8",
        "ibkr_order_flow_exit_delta_ratio": "0.18",
        "ibkr_order_flow_stop_delta_ratio": "0.12",
        "ibkr_order_flow_close_fill_timeout_sec": "5",
        "ibkr_close_execution_profile": "auto_session_limit",
        "ibkr_close_regular_limit_bps": "15",
        "ibkr_close_extended_limit_bps": "50",
        "ibkr_close_overnight_limit_bps": "100",
        "ibkr_close_quote_stale_sec": "120",
        "ibkr_close_allow_market": "false",
        "ibkr_close_include_overnight_enabled": "true",
        "ibkr_runtime_feed_technical_ticks_enabled": "true",
        "candidate_queue_max": "10",
        "candidate_breakout_ttl_sec": "120",
        "candidate_pullback_ttl_sec": "300",
        "candidate_reversal_ttl_sec": "600",
        "quality_auto_full_min": "80",
        "quality_auto_small_min": "75",
        "quality_shadow_min": "70",
        "entry_breakout_order_timeout_sec": "15",
        "entry_pullback_order_timeout_sec": "90",
        "entry_watch_after_fill_sec": "180",
        "partial_take_profit_r": "1.0",
        "partial_take_profit_fraction": "0.6",
        "breakeven_trigger_r": "0.6",
        "runner_enabled": "true",
        "runner_fraction": "0.4",
        "mean_reversion_runner_enabled": "false",
        "breakout_runner_enabled": "true",
        "trend_pullback_runner_enabled": "true",
        "new_entry_cutoff_time": "14:45",
        "force_flat_time": "15:45",
        "never_widen_stop_by_order_flow": "true",
        "cvd_flip_exit_enabled": "true",
        "cvd_divergence_take_profit_enabled": "true",
        "ibkr_target_filter_on": "false",
        "ibkr_trade_target_persistent_quote_enabled": "false",
        "ibkr_target_subscription_limit": "80",
        "ibkr_total_subscription_limit": "80",
        "entry_pre_submit_temp_subscription_limit": "8",
        "ibkr_target_refresh_sec": "60",
        "ibkr_realtime_quote_stale_resubscribe_sec": "600",
        "ibkr_realtime_quote_resubscribe_cooldown_sec": "300",
        "ibkr_live_warmup_days": "14",
        "ibkr_warmup_indicator_intervals": "5m,15m,30m,1h,4h,1d",
        "ibkr_warmup_indicator_buffer_days": "5",
        "ibkr_warmup_indicator_calendar_multiplier": "1.4",
        "ibkr_warmup_indicator_max_days": "60",
        "ibkr_warmup_indicator_regular_minutes_per_day": "390",
        "ibkr_warmup_indicator_backfill_enabled": "false",
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
        "ibkr_watchlist_idle_topup_enabled": "false",
        "ibkr_watchlist_idle_topup_loop_interval_sec": "2",
        "ibkr_watchlist_idle_topup_dynamic_enabled": "true",
        "ibkr_watchlist_idle_topup_active_first_enabled": "true",
        "ibkr_watchlist_idle_topup_dynamic_loop_interval_sec": "2",
        "ibkr_watchlist_idle_topup_dynamic_max_symbols_per_cycle": "160",
        "ibkr_watchlist_idle_topup_max_estimated_bars_per_cycle": "0",
        "ibkr_watchlist_idle_topup_batch_size": "160",
        "ibkr_watchlist_idle_topup_max_symbols_per_cycle": "0",
        "ibkr_watchlist_idle_topup_candidate_scan_size": "200",
        "ibkr_watchlist_idle_topup_request_period": "1d",
        "ibkr_watchlist_idle_topup_stale_min": "20",
        "ibkr_watchlist_idle_topup_no_data_cooldown_min": "15",
        "ibkr_watchlist_idle_topup_regular_no_data_cooldown_min": "5",
        "ibkr_watchlist_inactive_no_data_count_threshold": "3",
        "ibkr_watchlist_inactive_days_threshold": "3",
        "ibkr_watchlist_hygiene_notify_cooldown_hours": "24",
        "ibkr_watchlist_idle_topup_materialize_5m": "true",
        "ibkr_watchlist_idle_topup_rollup_without_new_bars": "true",
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
        "ibkr_peak_shedding_enabled": "true",
        "ibkr_peak_cpu_shedding_pct": "78",
        "ibkr_peak_cpu_recovery_pct": "60",
        "ibkr_peak_watchlist_warning_max_symbols": "40",
        "ibkr_peak_watchlist_warning_batch_size": "40",
        "ibkr_peak_watchlist_warning_history_concurrency": "6",
        "ibkr_peak_watchlist_warning_request_spacing": "0.15",
        "ibkr_peak_watchlist_shedding_max_symbols": "8",
        "ibkr_peak_watchlist_shedding_batch_size": "8",
        "ibkr_peak_watchlist_shedding_history_concurrency": "2",
        "ibkr_peak_watchlist_shedding_request_spacing": "0.3",
        "ibkr_peak_watchlist_critical_history_concurrency": "1",
        "ibkr_peak_watchlist_critical_request_spacing": "0.5",
        "ibkr_compute_busy_defer_sec": "60",
        "ibkr_compute_busy_defer_cap_sec": "300",
        "ibkr_compute_cursor_seed_from_indicators_enabled": "true",
        "ibkr_history_repair_enabled": "false",
        "ibkr_history_retention_enabled": "false",
        "ibkr_history_retention_days": "365",
        "storage_cleanup_enabled": "true",
        "storage_cleanup_profile": "tv_primary_lean",
        "storage_cleanup_backtest_recent_limit": "5",
        "storage_cleanup_protected_batch_ids": "3gf4ouzj7oyvlao",
        "storage_cleanup_protected_run_ids": "bm9wl0lagddsd6a",
        "storage_cleanup_vacuum_warn_gb": "1.0",
        "storage_cleanup_vacuum_warn_ratio": "0.15",
        "storage_cleanup_disk_low_free_pct": "15",
        "ibkr_history_repair_min_bars_5m": "260",
        "ibkr_history_repair_gap_lookback": "80",
        "ibkr_history_repair_rollup_enabled": "false",
        "ibkr_history_repair_intraday_max_symbols_per_run": "8",
        "ibkr_history_repair_intraday_time_budget_s": "60",
        "ibkr_history_repair_offhours_max_symbols_per_run": "25",
        "ibkr_history_repair_offhours_time_budget_s": "240",
        "ibkr_bar_repair_max_concurrency": "2",
        "ibkr_bar_repair_request_spacing": "1.0",
        "ibkr_bar_repair_max_retries": "3",
        "ibkr_bar_repair_period_5m": "4d",
        "ibkr_bar_repair_period_15m": "10d",
        "ibkr_bar_repair_period_30m": "20d",
        "ibkr_bar_repair_period_1h": "40d",
        "ibkr_bar_repair_period_4h": "120d",
        "ibkr_bar_repair_period_1d": "2y",
        "ibkr_backtest_preload_enabled": "true",
        "ibkr_backtest_preload_lookback_days": "14",
        "ibkr_backtest_preload_warmup_bars": "320",
        "ibkr_backtest_preload_buffer_bars": "20",
        "ibkr_backtest_preload_max_concurrency": "1",
        "ibkr_backtest_preload_request_spacing": "1.0",
        "ibkr_backtest_preload_max_retries": "1",
        "ibkr_backtest_preload_symbol_timeout_s": "600",
        "ibkr_backtest_preload_max_batches": "120",
        "ibkr_backtest_preload_history_timeout_s": "20",
        "ibkr_backtest_preload_history_max_retries": "1",
        "ibkr_history_max_concurrency": "16",
        "ibkr_history_request_spacing": "0.05",
        "ibkr_history_interval_delay": "0.10",
        "ibkr_history_max_retries": "4",
        "ibkr_history_retry_base_delay": "2.0",
        "ibkr_history_trace_enabled": "true",
        "ibkr_history_trace_recent_limit": "20",
        "ibkr_history_trace_slow_sec": "2.0",
        "ibkr_history_trace_log_all_requests": "true",
        "ibkr_history_chunked_backfill_enabled": "true",
        "ibkr_large_operation_alert_enabled": "true",
        "ibkr_large_operation_alert_min_symbols": "25",
        "ibkr_large_operation_alert_min_tasks": "50",
        "ibkr_large_operation_alert_min_period_days": "120",
        "ibkr_large_operation_alert_min_duration_s": "120",
        "ibkr_large_operation_alert_min_written": "10000",
        "ibkr_large_operation_alert_min_requests": "100",
        "ibkr_large_operation_alert_min_retry": "10",
        "ibkr_large_operation_alert_min_throttle": "50",
        "ibkr_large_operation_alert_min_time_budget_s": "120",
        "ibkr_large_operation_progress_cooldown_s": "300",
        "ibkr_large_operation_duration_gate_sources": "watchlist_idle_topup,runtime_direct_topup,runtime_direct_topup_parallel,bar_repair,active_repair,official_5m_close",
        "ibkr_large_operation_requires_ack": "false",
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
        "ibkr_official_5m_enabled": "false",
        "ibkr_official_5m_close_delay_sec": "3",
        "ibkr_official_5m_max_concurrency": "8",
        "ibkr_official_5m_parallel_enabled": "true",
        "ibkr_runtime_direct_topup_enabled": "false",
        "ibkr_runtime_direct_topup_intervals": "15m,30m,1h,4h,1d",
        "ibkr_runtime_direct_topup_close_delay_sec": "8",
        "ibkr_runtime_direct_topup_loop_interval_sec": "1",
        "ibkr_runtime_direct_topup_parallel_enabled": "false",
        "ibkr_runtime_direct_topup_wait_for_watchlist_5m_enabled": "false",
        "ibkr_runtime_direct_topup_interval_priority": "4h,1h,30m,15m,1d",
        "ibkr_runtime_direct_topup_period_15m": "2d",
        "ibkr_runtime_direct_topup_period_30m": "3d",
        "ibkr_runtime_direct_topup_period_1h": "5d",
        "ibkr_runtime_direct_topup_period_4h": "20d",
        "ibkr_runtime_direct_topup_period_1d": "60d",
        "signal_validity_minutes": "30",
        "signal_window_max_bars": "12",
        "signal_strategy_profile": "core_two_setup_v1",
        "ibkr_signal_strategy_profile": "",
        "exit_policy_profile": "setup_aware_hybrid_v1",
        "exit_policy_overrides": "",
        "intraday_harvest_enabled": "true",
        "intraday_harvest_live_auto_enabled": "true",
        "intraday_harvest_split_brackets_enabled": "true",
        "intraday_harvest_tactical_fraction": "0.30",
        "intraday_harvest_max_daily_cycles": "2",
        "intraday_harvest_min_partial_profit_r": "0.60",
        "intraday_harvest_min_tighten_profit_r": "0.30",
        "intraday_harvest_partial_score": "2",
        "intraday_harvest_tighten_score": "1",
        "intraday_harvest_full_exit_score": "4",
        "intraday_harvest_giveback_r": "0.45",
        "intraday_harvest_breakeven_lock_r": "0.10",
        "intraday_harvest_reentry_support_bps": "35",
        "intraday_harvest_reentry_score": "2",
        "intraday_harvest_reentry_sl_atr_mult": "1.20",
        "intraday_harvest_reentry_tp_rr": "1.0",
        "intraday_harvest_min_tactical_shares": "1",
        "intraday_signal_validity_minutes": "15",
        "intraday_entry_window_start_time": "09:35",
        "intraday_entry_window_end_time": "10:30",
        "entry_limit_mode": "passive_limit_dynamic",
        "entry_limit_atr_mult": "0.30",
        "entry_limit_floor_bps": "15",
        "entry_limit_cap_bps": "30",
        "marketable_limit_bps": "10",
        "orb_bars": "6",
        "sd_squeeze_lookback": "120",
        "sd_squeeze_rank_max": "0.25",
        "sd_flat_slope_pct": "0.03",
        "sd_breakout_confirm_bars": "1",
        "sd_trend_walk_min_bars": "2",
        "intraday_min_rvol_20": "1.2",
        "intraday_min_atr_pct": "0.08",
        "intraday_max_atr_pct": "1.20",
        "intraday_max_directional_day_change_pct": "4.0",
        "intraday_trend_mismatch_max_abs_day_change_pct": "0",
        "intraday_min_signal_quality_score": "70",
        "intraday_candidate_observation_min_quality_score": "60",
        "entry_plan_version": "entry_plan_v2",
        "entry_breakout_marketable_quality_min": "80",
        "entry_reprice_policy": "single_reprice_then_cancel",
        "intraday_vwap_pullback_atr_mult": "0.15",
        "intraday_vwap_pullback_max_bps": "10",
        "intraday_vwap_pullback_long_require_trend_walk": "true",
        "intraday_setup_daily_limit": "1",
        "intraday_setup_cooldown_bars": "6",
        "intraday_reentry_policy": "controlled",
        "intraday_symbol_daily_entry_limit": "3",
        "intraday_include_legacy_signals": "false",
        "ibkr_require_target_direction_alignment": "false",
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
        "live_exit_policy_stop_update_enabled": "false",
        "atr_stop_min_profit_r": "0.3",
        "atr_stop_deviation_threshold": "0.30",
        "atr_stop_min_change": "0.01",
        "reverse_flip_enabled": "false",
        "reverse_signal_threshold": "6",
        "trade_window_start_time": "09:35",
        "trade_window_end_time": "15:30",
        "order_window_end_time": "15:00",
        "position_limit_max": "36",
        "max_strategy_open_positions": "0",
        "ibkr_buying_power_guard_enabled": "true",
        "ibkr_buying_power_warn_usd": "25000",
        "ibkr_buying_power_warn_pct_net_liq": "20",
        "ibkr_buying_power_block_usd": "10000",
        "ibkr_buying_power_block_pct_net_liq": "10",
        "ibkr_buying_power_notify_enabled": "true",
        "ibkr_buying_power_notify_cooldown_sec": "1800",
        "fixed_position_symbols": "BOXX,IBKR",
        "consecutive_stop_loss_limit": "3",
        "order_validity_minutes": "30",
        "eod_close_time": "15:55",
        "eod_keep_symbols": "BOXX,IBKR",
        "signal_poll_interval_sec": "5",
        "tv_webhook_ingest_enabled": "true",
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
        "ibkr_order_tracker_callback_cache_during_activity": "true",
        "ibkr_order_tracker_skip_live_fetch_during_order_pressure": "true",
        "ibkr_account_data_skip_during_order_pressure": "true",
        "ibkr_order_place_fast_accept_enabled": "true",
        "ibkr_signal_submit_max_concurrency": "12",
        "ibkr_signal_ack_queue_maxsize": "1000",
        "ibkr_signal_ack_retry_attempts": "5",
        "ibkr_signal_ack_retry_base_delay_sec": "0.5",
        "ibkr_order_symbol_queue_enabled": "true",
        "ibkr_order_symbol_queue_max_active_symbols": "45",
        "ibkr_gateway_order_serial_enabled": "true",
        "ibkr_gateway_order_serial_timeout_sec": "900",
        "ibkr_execution_fill_sync_interval_sec": "0",
        "ibkr_account_snapshot_positions_fallback_enabled": "false",
        "ibkr_buying_power_stale_baseline_max_age_sec": "1800",
        "ibkr_buying_power_stale_safe_enabled": "true",
        "ibkr_buying_power_stale_safe_max_age_sec": "1800",
        "ibkr_buying_power_stale_safe_min_usd": "50000",
        "ibkr_buying_power_stale_safe_block_multiple": "5",
        "ibkr_buying_power_stale_safe_exposure_multiple": "3",
        "ibkr_order_question_suppress_enabled": "false",
        "ibkr_order_question_suppress_message_ids": "",
        "ibkr_scan_schedule": "08:20-09:20",
        "watchlist_interval_min": "5",
        "ibkr_daily_scan_time_et": "08:20",
        "ibkr_daily_scan_min_avg_10d_volume": "100000",
        "ibkr_daily_scan_min_atr_pct": "0.15",
        "ibkr_daily_scan_min_abs_day_change_pct": "1.0",
        "ibkr_daily_scan_min_premarket_volume": "5000",
        "ibkr_target_activity_gate_stages_json": (
            '[{"id":"preopen_early","start_et":"08:20","end_et":"08:55","any_of":{"premarket_volume_gte":3000}},'
            '{"id":"preopen_final","start_et":"09:00","end_et":"09:25","any_of":{"premarket_volume_gte":5000}},'
            '{"id":"open_discovery","start_et":"09:30","end_et":"09:45","any_of":{"premarket_volume_gte":5000,"regular_volume_gte":10000,"elapsed_rvol_gte":1.5}},'
            '{"id":"open_followthrough","start_et":"09:50","end_et":"10:30","any_of":{"premarket_volume_gte":10000,"regular_volume_gte":30000,"elapsed_rvol_gte":1.2}},'
            '{"id":"late_morning","start_et":"10:35","end_et":"11:00","any_of":{"regular_volume_gte":60000,"elapsed_rvol_gte":1.0}}]'
        ),
        "ibkr_daily_scan_day_gain_trigger_enabled": "true",
        "ibkr_daily_scan_day_gain_trigger_pct": "4.0",
        "ibkr_open_report_target_capture_enabled": "true",
        "ibkr_open_report_target_wait_sec": "45",
        "ibkr_open_report_target_poll_sec": "3",
        "ibkr_daily_scan_active_target_limit": "24",
        "ibkr_daily_scan_active_min_score": "40",
        "ibkr_dynamic_admission_enabled": "true",
        "ibkr_dynamic_admission_min_score": "58",
        "ibkr_symbol_profile_enabled": "true",
        "ibkr_symbol_profile_refresh_days": "30",
        "ibkr_symbol_profile_stale_days": "90",
        "ibkr_symbol_profile_backfill_batch_size": "20",
        "ibkr_fundamentals_enabled": "true",
        "ibkr_fundamentals_refresh_days": "7",
        "ibkr_fundamentals_stale_days": "30",
        "ibkr_fundamentals_batch_size": "12",
        "ibkr_fundamentals_gate_profile": "balanced",
        "ibkr_fundamentals_failed_gates_block_trade": "false",
        "pb_cron_ibkr_fundamentals_refresh_enabled": "false",
        "ibkr_daily_scan_data_completeness_enabled": "true",
        "ibkr_daily_scan_data_completeness_blocking_enabled": "true",
        "ibkr_daily_scan_data_completeness_intervals": "5m,15m,30m,1h,4h,1d",
        "ibkr_daily_scan_data_completeness_blocking_intervals": "5m",
        "ibkr_daily_scan_runtime_topup_wait_sec": "20",
        "ibkr_daily_scan_runtime_topup_poll_sec": "2",
        "ibkr_daily_scan_runtime_fallback_grace_sec": "480",
        "ibkr_daily_scan_indicator_snapshot_enabled": "true",
        "ibkr_daily_scan_indicator_snapshot_intervals": "5m,15m,30m,1h,4h,1d",
        "ibkr_daily_scan_rollup_enabled": "true",
        "ibkr_daily_scan_rollup_incremental": "true",
        "ibkr_daily_scan_rollup_intervals": "15m,30m,1h,4h,1d",
        "ibkr_rollup_parallel_enabled": "true",
        "ibkr_rollup_max_workers": "5",
        "ibkr_daily_scan_materialize_enabled": "true",
        "ibkr_daily_scan_materialize_intervals": "5m,15m,30m,1h",
        "ibkr_timeframe_param_profiles_json": (
            '{"5m":{"signal_strategy_profile":"core_two_setup_v1","intraday_include_legacy_signals":false},'
            '"15m":{"sd_length":96,"dtp_sma_length":80,"dtp_atr_length":160,"crsi_domcycle":24,"signal_window_max_bars":8},'
            '"30m":{"sd_length":80,"dtp_sma_length":70,"dtp_atr_length":140,"ema_slope_lookback":10},'
            '"1h":{"sd_length":80,"dtp_sma_length":60,"dtp_atr_length":120,"ema_slope_lookback":8},'
            '"4h":{"sd_length":60,"dtp_sma_length":50,"dtp_atr_length":100,"ema_slope_lookback":6},'
            '"1d":{"sd_length":50,"dtp_sma_length":40,"dtp_atr_length":80,"ema_slope_lookback":5}}'
        ),
        "ibkr_daily_scan_auto_retry_enabled": "true",
        "ibkr_daily_scan_retry_delays_sec": "60,120,240",
        "ibkr_daily_scan_retry_stall_timeout_sec": "480",
        "ibkr_daily_scan_preload_timeout_sec": "900",
        "ibkr_publish_batch_size": "10",
        "ibkr_signal_source": "tradingview",
        "signal_manual_confirm_enabled": "false",
        "ibkr_trading_enabled": "true",
        "ibkr_live_trading_enabled": "true",
        "tv_premarket_validation_enabled": "false",
        "tv_premarket_validation_run_id": "",
        "tv_premarket_validation_token": "",
        "tv_premarket_validation_expires_at_ms": "0",
        "system_monitor_ws_message_age_regular_warn_sec": "60",
        "system_monitor_ws_message_age_regular_critical_sec": "180",
        "system_monitor_ws_message_age_late_session_warn_sec": "600",
        "system_monitor_ws_message_age_late_session_critical_sec": "1200",
        "system_data_gap_bar_lag_alert_min": "20",
        "system_data_gap_indicator_lag_alert_min": "30",
        "system_data_gap_alert_cooldown_min": "30",
        "system_data_gap_indicator_requires_targets": "true",
    }
    REGISTRY = build_config_registry(DEFAULTS)

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
        fallback = default or self.DEFAULTS.get(key, "")
        best_value = self._record_value_for_environment(key, "")
        if best_value is None:
            for alias_key in deprecated_aliases_for(self.REGISTRY, key):
                best_value = self._record_value_for_environment(alias_key, "")
                if best_value is not None:
                    break
        if best_value is not None:
            return best_value
        if key in self._cache:
            return self._cache.get(key, fallback)
        for alias_key in deprecated_aliases_for(self.REGISTRY, key):
            if alias_key in self._cache:
                return self._cache.get(alias_key, fallback)
        return fallback

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

    def has_value_for_environment(self, key: str, environment: str) -> bool:
        runtime_environment = str(environment or "").strip().lower()
        if not key:
            return False
        for record in self._records_by_key.get(key, []):
            value = record.get("value", "")
            if value in (None, ""):
                continue
            record_environment = str(record.get("environment", "") or "").strip().lower()
            if record_environment == runtime_environment or record_environment in ("", "global"):
                return True
        return False

    def _record_value_for_environment(self, key: str, environment: str) -> str | None:
        runtime_environment = str(environment or "").strip().lower()
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

        return best_value

    def get_for_environment(self, key: str, environment: str, default: str = None) -> str:
        fallback = default if default is not None else self.DEFAULTS.get(key, "")
        best_value = self._record_value_for_environment(key, environment)
        if best_value is None:
            for alias_key in deprecated_aliases_for(self.REGISTRY, key):
                best_value = self._record_value_for_environment(alias_key, environment)
                if best_value is not None:
                    break
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
        return self.get("ibkr_scan_schedule", "08:20-09:20")

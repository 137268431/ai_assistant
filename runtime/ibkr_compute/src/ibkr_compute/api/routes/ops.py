from __future__ import annotations

from ibkr_compute.api.ops.views import (
    build_backtest_cancel_response,
    build_backtest_batch_detail_response,
    build_backtest_batches_response,
    build_backtest_cleanup_response,
    build_backtest_execution_cost_fills_response,
    build_backtest_execution_cost_import_recent_fills_response,
    build_backtest_execution_cost_import_response,
    build_backtest_execution_cost_profile_response,
    build_backtest_replay_response,
    build_backtest_run_detail_response,
    build_backtest_run_response,
    build_backtest_runs_response,
    build_backtest_preload_status_response,
    build_backtest_status_response,
    build_bar_repair_status_response,
    build_history_rebuild_start_response,
    build_history_rebuild_status_response,
    build_ibkr_data_quality_daily_repair_response,
    build_ibkr_data_quality_daily_rescan_response,
    build_ibkr_data_quality_repair_response,
    build_ibkr_data_quality_scan_response,
    build_ibkr_data_quality_truth_audit_response,
    build_health_response,
    build_retention_cleanup_response,
    build_status_response,
    build_storage_cleanup_response,
)
from ibkr_compute.api.runtime_proxy import register_runtime_proxy_route, should_proxy_runtime_requests


def register_ops_routes(app):
    @app.route("/ibkr/history/rebuild/start", methods=["POST"])
    def ibkr_history_rebuild_start():
        return build_history_rebuild_start_response()

    @app.route("/ibkr/history/rebuild/status", methods=["GET"])
    def ibkr_history_rebuild_status():
        return build_history_rebuild_status_response()

    @app.route("/retention/cleanup", methods=["POST"])
    def retention_cleanup():
        return build_retention_cleanup_response()

    @app.route("/storage/cleanup", methods=["POST"])
    def storage_cleanup():
        return build_storage_cleanup_response()

    @app.route("/backtest/run", methods=["POST"])
    def backtest_run():
        return build_backtest_run_response()

    @app.route("/backtest/status", methods=["GET"])
    def backtest_status():
        return build_backtest_status_response()

    @app.route("/backtest/runs", methods=["GET"])
    def backtest_runs():
        return build_backtest_runs_response()

    @app.route("/backtest/run", methods=["GET"])
    def backtest_run_detail():
        return build_backtest_run_detail_response()

    @app.route("/backtest/batches", methods=["GET"])
    def backtest_batches():
        return build_backtest_batches_response()

    @app.route("/backtest/batch", methods=["GET"])
    def backtest_batch_detail():
        return build_backtest_batch_detail_response()

    @app.route("/backtest/cancel", methods=["POST"])
    def backtest_cancel():
        return build_backtest_cancel_response()

    @app.route("/backtest/replay", methods=["GET"])
    def backtest_replay():
        return build_backtest_replay_response()

    @app.route("/backtest/cleanup", methods=["POST"])
    def backtest_cleanup():
        return build_backtest_cleanup_response()

    @app.route("/backtest/execution-cost/import", methods=["POST"])
    def backtest_execution_cost_import():
        return build_backtest_execution_cost_import_response()

    @app.route("/backtest/execution-cost/import-recent-fills", methods=["POST"])
    def backtest_execution_cost_import_recent_fills():
        return build_backtest_execution_cost_import_recent_fills_response()

    @app.route("/backtest/execution-cost/fills", methods=["GET", "POST"])
    def backtest_execution_cost_fills():
        return build_backtest_execution_cost_fills_response()

    @app.route("/backtest/execution-cost/profile", methods=["GET", "POST"])
    def backtest_execution_cost_profile():
        return build_backtest_execution_cost_profile_response()

    @app.route("/health", methods=["GET"])
    def health():
        return build_health_response()

    @app.route("/status", methods=["GET"])
    def status():
        return build_status_response()

    @app.route("/ibkr/bar-repair/status", methods=["GET", "POST"])
    def ibkr_bar_repair_status():
        return build_bar_repair_status_response()

    @app.route("/ibkr/backtest-preload/status", methods=["GET", "POST"])
    def ibkr_backtest_preload_status():
        return build_backtest_preload_status_response()

    if should_proxy_runtime_requests():
        register_runtime_proxy_route(app, "ibkr_data_quality_scan", "/ibkr/data-quality/scan", ["POST"])
        register_runtime_proxy_route(app, "ibkr_data_quality_repair", "/ibkr/data-quality/repair", ["POST"])
        register_runtime_proxy_route(app, "ibkr_data_quality_truth_audit", "/ibkr/data-quality/truth-audit", ["POST"])
        register_runtime_proxy_route(app, "ibkr_data_quality_daily_rescan", "/ibkr/data-quality/daily-rescan", ["POST"])
        register_runtime_proxy_route(app, "ibkr_data_quality_daily_repair", "/ibkr/data-quality/daily-repair", ["POST"])
    else:
        @app.route("/ibkr/data-quality/scan", methods=["POST"])
        def ibkr_data_quality_scan():
            return build_ibkr_data_quality_scan_response()

        @app.route("/ibkr/data-quality/repair", methods=["POST"])
        def ibkr_data_quality_repair():
            return build_ibkr_data_quality_repair_response()

        @app.route("/ibkr/data-quality/truth-audit", methods=["POST"])
        def ibkr_data_quality_truth_audit():
            return build_ibkr_data_quality_truth_audit_response()

        @app.route("/ibkr/data-quality/daily-rescan", methods=["POST"])
        def ibkr_data_quality_daily_rescan():
            return build_ibkr_data_quality_daily_rescan_response()

        @app.route("/ibkr/data-quality/daily-repair", methods=["POST"])
        def ibkr_data_quality_daily_repair():
            return build_ibkr_data_quality_daily_repair_response()

from control_plane_split_stack_helpers import *


class ControlPlaneSplitStackStatusMonitorTest(unittest.TestCase):
    def test_api_pb_client_reads_runtime_config_directly_from_pocketbase(self):
        self.assertFalse(api_app_mod.pb.prefer_runtime_config_api)

    def test_today_counts_exposes_grouped_order_count(self):
        rows = [
            {"id": "entry-1", "trade_group_id": "tg-1", "role": "entry"},
            {"id": "tp-1", "trade_group_id": "tg-1", "role": "take_profit"},
            {"id": "sl-1", "trade_group_id": "tg-1", "role": "stop_loss"},
        ]

        def fake_count(collection, filter_expr):
            return 3 if collection == "orders" else 0

        with mock.patch.object(api_app_mod, "_pb_count_records", side_effect=fake_count):
            with mock.patch.object(api_app_mod, "_pb_load_records_for_count", return_value=rows):
                payload = api_app_mod._load_today_counts("live", "2026-05-06")

        self.assertEqual(payload["orders"], 3)
        self.assertEqual(payload["main_orders"], 1)
        self.assertEqual(payload["order_groups"], 1)

    def test_today_counts_ignores_protective_rows_with_split_group_aliases(self):
        rows = [
            {"id": "entry-1", "trade_group_id": "sig-1_entry", "signal_id": "sig-1", "role": "entry"},
            {"id": "tp-1", "trade_group_id": "tp_sig-1", "signal_id": "sig-1", "role": "take_profit"},
            {"id": "sl-1", "trade_group_id": "sl_sig-1", "signal_id": "sig-1", "role": "stop_loss"},
        ]

        with mock.patch.object(api_app_mod, "_pb_count_records", side_effect=lambda collection, _filter: 3 if collection == "orders" else 0):
            with mock.patch.object(api_app_mod, "_pb_load_records_for_count", return_value=rows):
                payload = api_app_mod._load_today_counts("live", "2026-05-06")

        self.assertEqual(payload["orders"], 3)
        self.assertEqual(payload["main_orders"], 1)
        self.assertEqual(payload["order_groups"], 1)

    def test_runtime_config_route_returns_effective_environment_values(self):
        rows = [
            {"key": "alpha", "value": "global", "environment": "global", "updated": "2026-04-22 00:00:00"},
            {"key": "alpha", "value": "live", "environment": "live", "updated": "2026-04-22 00:05:00"},
            {"key": "beta", "value": "fallback", "environment": "", "updated": "2026-04-22 00:01:00"},
        ]

        with mock.patch.object(api_app_mod.pb, "get_runtime_config", return_value=rows):
            with mock.patch.object(api_app_mod.request, "args", {"environment": "live"}):
                payload = api_app_mod.custom_ibkr_runtime_config()

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["environment"], "live")
        self.assertEqual(payload["scope"], "effective")
        item_map = {item["key"]: item["value"] for item in payload["items"]}
        self.assertEqual(item_map["alpha"], "live")
        self.assertEqual(item_map["beta"], "fallback")
        self.assertIn("ibkr-api", payload["service_topology"]["services"])
        self.assertIn("ibkr-scheduler", payload["service_topology"]["services"])

    def test_api_status_reports_native_routes(self):
        scheduler_payload = {
            "ok": True,
            "status": "running",
            "environment": "live",
            "loop_interval_seconds": 30,
            "ingest_cursor": {},
            "compute_dispatch_cursor": {},
            "jobs": {"ibkr_compute_runtime": {"status": "ok"}},
        }
        with mock.patch.object(api_app_mod.config, "refresh", return_value=None):
            with mock.patch.object(api_app_mod, "_scheduler_status", return_value=scheduler_payload):
                payload = api_app_mod.status()
        self.assertTrue(payload["ok"])
        self.assertIn("ibkr/2fa/request", payload["compatibility"]["native_custom_routes"])
        self.assertIn("ibkr/2fa/respond", payload["compatibility"]["native_custom_routes"])
        self.assertIn("ibkr/2fa/result", payload["compatibility"]["native_custom_routes"])
        self.assertIn("ibkr/2fa/status", payload["compatibility"]["native_custom_routes"])
        self.assertIn("ibkr/2fa/takeover", payload["compatibility"]["native_custom_routes"])
        self.assertIn("ibkr/2fa/probe", payload["compatibility"]["native_custom_routes"])
        self.assertIn("ibkr/2fa/panic-reset", payload["compatibility"]["native_custom_routes"])
        self.assertIn("ibkr/bars", payload["compatibility"]["native_custom_routes"])
        self.assertIn("ibkr/indicator", payload["compatibility"]["native_custom_routes"])
        self.assertIn("ibkr/indicators", payload["compatibility"]["native_custom_routes"])
        self.assertIn("ibkr/scan", payload["compatibility"]["native_custom_routes"])
        self.assertIn("ibkr/data_quality/upsert", payload["compatibility"]["native_custom_routes"])
        self.assertIn("ibkr/data_quality/truth_upsert", payload["compatibility"]["native_custom_routes"])
        self.assertIn("ibkr/data_quality/summary", payload["compatibility"]["native_custom_routes"])
        self.assertIn("ibkr/data_quality/list", payload["compatibility"]["native_custom_routes"])
        self.assertIn("ibkr/account_snapshot", payload["compatibility"]["native_custom_routes"])
        self.assertIn("ibkr/healthz", payload["compatibility"]["native_custom_routes"])
        self.assertIn("ibkr/screener", payload["compatibility"]["native_custom_routes"])
        self.assertIn("ibkr/today-targets", payload["compatibility"]["native_custom_routes"])
        self.assertIn("ibkr/watchlist/upsert", payload["compatibility"]["native_custom_routes"])
        self.assertIn("ibkr/targets/upsert", payload["compatibility"]["native_custom_routes"])
        self.assertIn("ibkr/screener/targets", payload["compatibility"]["native_custom_routes"])
        self.assertIn("ibkr/orders/upsert", payload["compatibility"]["native_custom_routes"])
        self.assertIn("ibkr/orders/cancel_sync", payload["compatibility"]["native_custom_routes"])
        self.assertIn("ibkr/orders/cancel_group", payload["compatibility"]["native_custom_routes"])
        self.assertIn("ibkr/reverse/dispatch", payload["compatibility"]["native_custom_routes"])
        self.assertIn("ibkr/runtime/config", payload["compatibility"]["native_custom_routes"])
        self.assertIn("ibkr/signal", payload["compatibility"]["native_custom_routes"])
        self.assertIn("ibkr/signals", payload["compatibility"]["native_custom_routes"])
        self.assertIn("ibkr/signals/pending", payload["compatibility"]["native_custom_routes"])
        self.assertIn("ibkr/startup/progress", payload["compatibility"]["native_custom_routes"])
        self.assertIn("ibkr/startup/status", payload["compatibility"]["native_custom_routes"])
        self.assertIn("ibkr/statusz", payload["compatibility"]["native_custom_routes"])
        self.assertIn("ibkr/reverse/calculate", payload["compatibility"]["native_custom_routes"])
        self.assertIn("order/cancel", payload["compatibility"]["native_webhook_routes"])
        self.assertIn("signal/confirm", payload["compatibility"]["native_webhook_routes"])
        self.assertIn("signal/cancel", payload["compatibility"]["native_webhook_routes"])
        self.assertIn("order/close", payload["compatibility"]["native_webhook_routes"])
        self.assertNotIn("ibkr/signal", payload["compatibility"]["delegated_pocketbase_custom_routes"])
        self.assertNotIn("ibkr/signals", payload["compatibility"]["delegated_pocketbase_custom_routes"])
        self.assertNotIn("ibkr/orders/upsert", payload["compatibility"]["delegated_pocketbase_custom_routes"])
        self.assertNotIn("ibkr/orders/cancel_group", payload["compatibility"]["delegated_pocketbase_custom_routes"])
        self.assertNotIn("ibkr/orders/close_group", payload["compatibility"]["delegated_pocketbase_custom_routes"])
        self.assertNotIn("ibkr/orders/reconcile", payload["compatibility"]["delegated_pocketbase_custom_routes"])
        self.assertNotIn("ibkr/reverse/ack", payload["compatibility"]["delegated_pocketbase_custom_routes"])
        self.assertNotIn("ibkr/reverse/calculate", payload["compatibility"]["delegated_pocketbase_custom_routes"])
        self.assertNotIn("ibkr/reverse/dispatch", payload["compatibility"]["delegated_pocketbase_custom_routes"])
        self.assertNotIn("ibkr/reverse/list", payload["compatibility"]["delegated_pocketbase_custom_routes"])
        self.assertNotIn("ibkr/reverse/pending", payload["compatibility"]["delegated_pocketbase_custom_routes"])
        self.assertNotIn("ibkr/signals/ack", payload["compatibility"]["delegated_pocketbase_custom_routes"])
        self.assertNotIn("order/cancel", payload["compatibility"]["delegated_pocketbase_webhook_routes"])
        self.assertNotIn("order/close", payload["compatibility"]["delegated_pocketbase_webhook_routes"])
        self.assertNotIn("signal/confirm", payload["compatibility"]["delegated_pocketbase_webhook_routes"])
        self.assertNotIn("signal/cancel", payload["compatibility"]["delegated_pocketbase_webhook_routes"])
        self.assertIn("ibkr/account", payload["compatibility"]["direct_proxy_routes"])
        self.assertIn("ibkr/bar-repair/status", payload["compatibility"]["direct_proxy_routes"])
        self.assertIn("bar_repair/status", payload["compatibility"]["proxy_action_routes"])
        self.assertNotIn("ibkr/account_snapshot", payload["compatibility"]["direct_proxy_routes"])
        self.assertIn("system/cronz", payload["compatibility"]["native_custom_routes"])
        self.assertIn("system/event", payload["compatibility"]["native_custom_routes"])
        self.assertIn("system/healthz", payload["compatibility"]["native_custom_routes"])
        self.assertIn("system/jobs/heartbeat", payload["compatibility"]["native_custom_routes"])
        self.assertIn("system/jobs/intraday_window_admission", payload["compatibility"]["native_custom_routes"])
        self.assertIn("system/jobs/monitor_alert_guard", payload["compatibility"]["native_custom_routes"])
        self.assertIn("system/jobs/scan_summary", payload["compatibility"]["native_custom_routes"])
        self.assertIn("system/jobs/status_reminder", payload["compatibility"]["native_custom_routes"])
        self.assertIn("system/schedulerz", payload["compatibility"]["native_custom_routes"])
        self.assertIn("system/scheduler/jobs/run", payload["compatibility"]["native_custom_routes"])
        self.assertIn("system/monitorz", payload["compatibility"]["native_custom_routes"])
        self.assertIn("system/summaryz", payload["compatibility"]["native_custom_routes"])
        self.assertIn("ibkr-api", payload["service_topology"]["services"])
        self.assertEqual(payload["compatibility"]["pocketbase_proxy_routes"]["custom"], "/api/custom/*")
        self.assertEqual(payload["compatibility"]["pocketbase_proxy_routes"]["webhook"], "/webhook/*")
        self.assertFalse(payload["compatibility"]["fallback_to_pocketbase_custom"])
        self.assertFalse(payload["compatibility"]["fallback_to_pocketbase_webhooks"])
        self.assertEqual(payload["compatibility"]["unmatched_custom_route_behavior"], "404_from_ibkr_api")
        self.assertEqual(payload["compatibility"]["unmatched_webhook_route_behavior"], "404_from_ibkr_api")

    def test_generic_custom_proxy_rejects_unmatched_routes_in_api(self):
        payload, status_code = api_app_mod.custom_proxy("ibkr/legacy_fallback")
        self.assertEqual(status_code, 404)
        self.assertEqual(payload["error"], "unsupported_custom_route")
        self.assertEqual(payload["route_family"], "custom")
        self.assertEqual(payload["subpath"], "ibkr/legacy_fallback")
        self.assertEqual(payload["source"], "ibkr-api")

    def test_generic_webhook_proxy_rejects_unmatched_routes_in_api(self):
        payload, status_code = api_app_mod.webhook_proxy("legacy/hook")
        self.assertEqual(status_code, 404)
        self.assertEqual(payload["error"], "unsupported_webhook_route")
        self.assertEqual(payload["route_family"], "webhook")
        self.assertEqual(payload["subpath"], "legacy/hook")
        self.assertEqual(payload["source"], "ibkr-api")

    def test_monitorz_route_merges_split_stack_service_monitor(self):
        base_monitor_payload = {
            "ok": True,
            "status": "ok",
            "environment": "live",
            "runtime": {
                "runtime_phase": "running",
                "session": {"authenticated": True},
                "websocket": {"connected": True, "ready": True},
                "gateway": {"running": True, "reachable": True, "managed_by": "ibkr-runtime", "pid": 123},
            },
            "compute": {
                "ready_engines": 4,
                "total_engines": 5,
                "compute_count": 12,
                "tracked_cursors": 3,
            },
            "flags": [],
            "pocketbase": {"disk": {"status": "ready", "data_path": "/opt/pocketbase/pb_data"}},
            "service_topology": api_app_mod.build_service_topology(),
        }
        scheduler_payload = {
            "ok": True,
            "status": "running",
            "environment": "live",
            "loop_interval_seconds": 30,
            "ingest_cursor": {"intervals": {"5m": {"latest_bar_time_ms": 1713797100000}}},
            "compute_dispatch_cursor": {"intervals": {"5m": {"latest_bar_time_ms": 1713796800000, "last_dispatched_at_ms": 1713797160000}}},
            "jobs": {"ibkr_compute_runtime": {"status": "ok"}},
        }
        request_results = [
            {"ok": True, "status_code": 200, "payload": base_monitor_payload, "target_url": "http://compute/ibkr/monitor", "error": ""},
            {"ok": True, "status_code": 200, "payload": {"code": 200, "message": "OK"}, "target_url": "http://pb/api/health", "error": ""},
            {
                "ok": True,
                "status_code": 200,
                "payload": {
                    "ok": True,
                    "status": "running",
                    "service": "ibkr-backtest",
                    "backtest": {"ok": True, "status": "idle", "ib_gateway_client_id": 81},
                },
                "target_url": "http://backtest/health",
                "error": "",
            },
        ]

        with mock.patch.object(api_app_mod.config, "refresh", return_value=None):
            with mock.patch.object(api_app_mod, "_request_json", side_effect=request_results):
                with mock.patch.object(api_app_mod, "_scheduler_status", return_value=scheduler_payload):
                    with mock.patch.object(api_app_mod.pb, "get_runtime_config", return_value=[]):
                        with mock.patch.object(api_app_mod.pb, "get_records", return_value=[]):
                            with mock.patch.object(api_app_mod.pb, "get_all_records", return_value=[]):
                                with mock.patch.object(api_app_mod, "_probe_console_status", return_value={"ok": True, "status_code": 200, "target_url": "http://console/index.html", "error": ""}):
                                    with mock.patch.object(api_app_mod.request, "args", {"environment": "live"}):
                                        payload = api_app_mod.custom_system_monitorz()

        self.assertTrue(payload["ok"])
        self.assertIn("service_monitor", payload)
        self.assertIn("ibkr-console", payload["service_monitor"]["services"])
        self.assertEqual(payload["service_monitor"]["services"]["ibkr-scheduler"]["status"], "running")
        self.assertEqual(payload["scheduler"]["dispatch_lag_min"], 5.0)
        self.assertEqual(payload["upstream_monitor"]["target_url"], "http://compute/ibkr/monitor")
        self.assertEqual(payload["pocketbase"]["disk"]["data_path"], "/opt/pocketbase/pb_data")

    def test_monitorz_service_map_does_not_mark_compute_offline_for_runtime_auth_issue(self):
        base_monitor_payload = {
            "ok": False,
            "status": "error",
            "environment": "live",
            "runtime": {
                "runtime_phase": "stopped",
                "session": {"authenticated": False},
                "websocket": {"connected": False, "ready": False},
                "gateway": {"running": True, "reachable": True, "managed_by": "ibkr-runtime", "pid": 123},
            },
            "compute": {
                "status": "running",
                "ready_engines": 0,
                "total_engines": 0,
                "compute_count": 0,
                "tracked_cursors": 0,
            },
            "flags": [{"code": "session_unauthenticated"}],
            "pocketbase": {"disk": {"status": "ready", "data_path": "/opt/pocketbase/pb_data"}},
            "service_topology": api_app_mod.build_service_topology(),
        }
        scheduler_payload = {
            "ok": True,
            "status": "running",
            "environment": "live",
            "loop_interval_seconds": 30,
            "ingest_cursor": {},
            "compute_dispatch_cursor": {},
            "jobs": {"ibkr_compute_runtime": {"status": "ok"}},
        }
        request_results = [
            {"ok": False, "status_code": 200, "payload": base_monitor_payload, "target_url": "http://compute/ibkr/monitor", "error": ""},
            {"ok": True, "status_code": 200, "payload": {"code": 200, "message": "OK"}, "target_url": "http://pb/api/health", "error": ""},
            {
                "ok": True,
                "status_code": 200,
                "payload": {
                    "ok": True,
                    "status": "running",
                    "service": "ibkr-backtest",
                    "backtest": {"ok": True, "status": "idle", "ib_gateway_client_id": 81},
                },
                "target_url": "http://backtest/health",
                "error": "",
            },
        ]

        with mock.patch.object(api_app_mod.config, "refresh", return_value=None):
            with mock.patch.object(api_app_mod, "_request_json", side_effect=request_results):
                with mock.patch.object(api_app_mod, "_scheduler_status", return_value=scheduler_payload):
                    with mock.patch.object(api_app_mod.pb, "get_runtime_config", return_value=[]):
                        with mock.patch.object(api_app_mod.pb, "get_records", return_value=[]):
                            with mock.patch.object(api_app_mod.pb, "get_all_records", return_value=[]):
                                with mock.patch.object(api_app_mod, "_probe_console_status", return_value={"ok": True, "status_code": 200, "target_url": "http://console/index.html", "error": ""}):
                                    with mock.patch.object(api_app_mod.request, "args", {"environment": "live"}):
                                        payload = api_app_mod.custom_system_monitorz()

        service_statuses = payload["service_monitor"]["services"]
        self.assertEqual(service_statuses["ibkr-compute"]["status"], "running")
        self.assertEqual(service_statuses["ibkr-runtime"]["status"], "degraded")

    def test_summaryz_route_returns_native_split_stack_payload(self):
        compute_health = {
            "ok": True,
            "payload": {
                "ok": True,
                "status": "running",
                "compute_count": 9,
                "error_count": 1,
                "uptime_s": 600,
                "last_compute": "2026-04-22T07:05:00-04:00",
                "last_scan": "2026-04-22T07:00:00-04:00",
                "service_topology": api_app_mod.build_service_topology(),
            },
            "error": "",
        }
        compute_status = {
            "ok": True,
            "payload": _sample_compute_status_payload(),
            "error": "",
        }
        runtime_result = {
            "ok": True,
            "payload": _sample_runtime_status_payload(authenticated=True),
            "error": "",
            "selected_upstream": "http://runtime/ibkr/status",
            "proxy_upstream": "http://compute/ibkr/status",
            "direct_upstream": "http://runtime/ibkr/status",
        }
        config_rows = [
            {"key": "ibkr_compute_enabled", "value": "TRUE", "environment": "live"},
            {"key": "ibkr_trading_enabled", "value": "FALSE", "environment": "live"},
            {"key": "pb_scheduler_enabled", "value": "TRUE", "environment": "global"},
        ]
        recent_rows = [
            {
                "id": "evt-1",
                "event_type": "status_change",
                "level": "info",
                "source": "ibkr_scheduler",
                "environment": "live",
                "title": "[LIVE] Scheduler dispatch ok",
                "notified": True,
                "us_time": "2026-04-22 07:10:00",
                "created": "2026-04-22 07:10:01",
            }
        ]
        with mock.patch.object(api_app_mod, "_fetch_compute_health", return_value=compute_health):
            with mock.patch.object(api_app_mod, "_fetch_compute_status", return_value=compute_status):
                with mock.patch.object(api_app_mod, "_fetch_runtime_status", return_value=runtime_result):
                    with mock.patch.object(api_app_mod.pb, "get_runtime_config", return_value=config_rows):
                        with mock.patch.object(api_app_mod.pb, "get_records", return_value=recent_rows):
                            with mock.patch.object(api_app_mod, "_load_today_counts", return_value={}):
                                with mock.patch.object(api_app_mod.request, "args", {"environment": "live", "lite": "1"}):
                                    payload = api_app_mod.custom_system_summaryz()

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "running")
        self.assertEqual(payload["environment"], "live")
        self.assertFalse(payload["ibkr_trading_enabled"])
        self.assertEqual(payload["ibkr_compute"]["compute_count"], 9)
        self.assertEqual(payload["ibkr_runtime"]["proxy_upstream"], "http://runtime/ibkr/status")
        self.assertEqual(payload["recent_events"][0]["id"], "evt-1")
        self.assertIn("ibkr-api", payload["service_topology"]["services"])

    def test_system_healthz_route_uses_native_monitor_summary(self):
        monitor_payload = {
            "ok": True,
            "status": "warning",
            "requested_environment": "live",
            "actual_runtime_environment": "paper",
            "runtime_environment_mismatch": True,
            "service_topology": api_app_mod.build_service_topology(),
            "service_monitor": {"services": {"ibkr-api": {"status": "running"}}},
            "scheduler": {"status": "running"},
        }
        with mock.patch.object(api_app_mod, "_build_system_monitor_payload", return_value=monitor_payload):
            with mock.patch.object(api_app_mod.request, "args", {"environment": "live"}):
                payload = api_app_mod.custom_system_healthz()

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "warning")
        self.assertEqual(payload["actual_runtime_environment"], "paper")
        self.assertTrue(payload["runtime_environment_mismatch"])
        self.assertIn("ibkr-api", payload["service_topology"]["services"])

    def test_startup_status_route_reads_persisted_state(self):
        state_record = {
            "date": "global",
            "data": {
                "startup_label": "LIVE-20260422-070000-001",
                "status": "running",
                "current_step": "manual_trigger",
                "current_blocker": "等待人工开始 2FA",
                "operator_action": "去飞书点击开始验证",
                "steps": {
                    "manual_trigger": {
                        "label": "在飞书手动触发 2FA",
                        "status": "waiting",
                    }
                },
            },
        }
        with mock.patch.object(api_app_mod.pb, "get_state", return_value=state_record):
            with mock.patch.object(api_app_mod.request, "args", {"environment": "live"}):
                payload = api_app_mod.custom_ibkr_startup_status()

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["startup_label"], "LIVE-20260422-070000-001")
        self.assertEqual(payload["state"]["current_step"], "manual_trigger")
        self.assertEqual(payload["state"]["operator_action"], "去飞书点击开始验证")

    def test_system_event_route_writes_record_with_native_delivery(self):
        records = []

        def fake_create_record(collection, data):
            records.append((collection, data))
            return data

        with mock.patch.object(api_app_mod.request, "get_json", return_value={
            "environment": "live",
            "title": "Runtime 已恢复",
            "detail": {"状态结论": "系统恢复"},
            "event_type": "status_change",
            "level": "info",
            "source": "ibkr_compute",
        }):
            with mock.patch.object(api_app_mod.config, "refresh", return_value=None):
                with mock.patch.object(api_app_mod, "_deliver_system_event_notification", return_value={"success": True, "message_id": "msg-1"}):
                    with mock.patch.object(api_app_mod.pb, "create_record", side_effect=fake_create_record):
                        payload = api_app_mod.custom_system_event()

        self.assertTrue(payload["ok"])
        self.assertTrue(payload["notified"])
        self.assertEqual(payload["message_id"], "msg-1")
        self.assertEqual(records[0][0], "system_events")
        self.assertEqual(records[0][1]["environment"], "live")
        self.assertTrue(str(records[0][1]["title"]).startswith("[LIVE] "))

    def test_startup_progress_route_upserts_state_and_records_event(self):
        state_store = {}
        records = []

        def fake_get_state(state_key, environment, date="global"):
            return state_store.get((state_key, environment, date))

        def fake_upsert_state(state_key, environment, data, date="global"):
            record = {
                "state_key": state_key,
                "environment": environment,
                "date": date,
                "data": dict(data),
            }
            state_store[(state_key, environment, date)] = record
            return record

        def fake_create_record(collection, data):
            records.append((collection, data))
            return data

        with mock.patch.object(api_app_mod.request, "get_json", return_value={
            "environment": "live",
            "action": "begin",
            "title": "IBKR Runtime 启动中",
            "summary": "等待 Gateway 与 2FA",
            "current_step": "auth",
            "current_blocker": "等待手动触发 2FA",
            "operator_action": "去飞书点击开始验证",
            "fields": {"启动原因": "manual_start"},
            "steps": {"auth": {"status": "waiting", "detail": "等待人工按钮"}},
            "record_event": True,
            "event_type": "status_change",
            "event_title": "IBKR Runtime 等待手动 2FA",
            "event_detail": {"状态结论": "等待中"},
            "level": "warning",
            "event_source": "ibkr_compute",
        }):
            with mock.patch.object(api_app_mod.pb, "get_state", side_effect=fake_get_state):
                with mock.patch.object(api_app_mod.pb, "upsert_state", side_effect=fake_upsert_state):
                    with mock.patch.object(api_app_mod.pb, "create_record", side_effect=fake_create_record):
                        with mock.patch.object(api_app_mod, "_deliver_startup_progress_card", return_value={"success": True, "message_id": "startup-msg-1"}):
                            payload = api_app_mod.custom_ibkr_startup_progress()

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["environment"], "live")
        self.assertEqual(payload["message_id"], "startup-msg-1")
        self.assertTrue(payload["startup_label"].startswith("LIVE-"))
        saved = state_store[(api_app_mod.IBKR_STARTUP_STATE_KEY, "live", "global")]["data"]
        self.assertEqual(saved["message_id"], "startup-msg-1")
        self.assertEqual(saved["current_step"], "auth")
        self.assertTrue(any(collection == "system_events" for collection, _ in records))

    def test_two_factor_status_route_merges_runtime_state(self):
        state_record = {
            "date": "global",
            "data": {
                "status": "requested",
                "reason": "manual_reauth",
                "recovery_phase": "idle",
            },
        }
        runtime_result = {
            "ok": True,
            "payload": _sample_runtime_status_payload(authenticated=True),
            "error": "",
            "selected_upstream": "http://runtime/ibkr/status",
            "proxy_upstream": "http://compute/ibkr/status",
            "direct_upstream": "http://runtime/ibkr/status",
        }
        with mock.patch.object(api_app_mod.pb, "get_state", return_value=state_record):
            with mock.patch.object(api_app_mod, "_fetch_runtime_status", return_value=runtime_result):
                with mock.patch.object(api_app_mod.request, "args", {"environment": "live"}):
                    payload = api_app_mod.custom_ibkr_two_factor_status()

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["state"]["status"], "success")
        self.assertTrue(payload["state"]["runtime_authenticated"])
        self.assertTrue(payload["state"]["gateway_reachable"])
        self.assertFalse(payload["state"]["runtime_environment_mismatch"])

    def test_statusz_route_returns_merged_split_stack_payload(self):
        compute_result = {"ok": True, "payload": _sample_compute_status_payload(), "error": ""}
        runtime_result = {
            "ok": True,
            "payload": _sample_runtime_status_payload(authenticated=True),
            "error": "",
            "selected_upstream": "http://runtime/ibkr/status",
            "proxy_upstream": "http://compute/ibkr/status",
            "direct_upstream": "http://runtime/ibkr/status",
        }
        with mock.patch.object(api_app_mod, "_fetch_compute_status", return_value=compute_result) as fetch_compute_status:
            with mock.patch.object(api_app_mod, "_fetch_runtime_status", return_value=runtime_result):
                with mock.patch.object(api_app_mod, "_load_daily_scan_state", return_value={"market_date": "2026-04-22"}):
                    with mock.patch.object(api_app_mod, "_count_active_today_targets", return_value=1):
                        with mock.patch.object(api_app_mod.request, "args", {"environment": "live", "lite": "1"}):
                            payload = api_app_mod.custom_ibkr_statusz()

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["requested_environment"], "live")
        self.assertEqual(payload["actual_runtime_environment"], "live")
        self.assertFalse(payload["warmup_details_included"])
        self.assertEqual(payload["compute"]["statusz_mode"], "lite")
        self.assertEqual(payload["runtime"]["market_universe"]["active_target_count"], 1)
        self.assertIn(payload["live_readiness"]["source"], {"compute_engines", "runtime_warmup_snapshot"})
        self.assertIn("ibkr-runtime", payload["service_topology"]["services"])
        fetch_compute_status.assert_called_once_with("live", include_engines=False)

    def test_live_readiness_prefers_open_runtime_gate_during_background_refresh(self):
        compute_payload = _sample_compute_status_payload()
        compute_payload["engines"] = {}
        compute_payload["multi_timeframe_readiness"] = {
            "symbols_total": 1,
            "intervals": {
                "5m": {
                    "status": "blocked",
                    "symbols_total": 1,
                    "missing_ready_symbols_total": 1,
                    "missing_ready_symbols": ["AAPL"],
                }
            },
        }
        runtime_payload = _sample_runtime_status_payload(authenticated=True)
        runtime_payload["warmup"]["phase"] = "running"
        runtime_payload["warmup"]["trading_gate_open"] = True
        runtime_payload["warmup"]["trading_gate_reason"] = "ready"
        runtime_payload["warmup"]["pending_symbols"] = ["SPY"]
        runtime_payload["multi_timeframe_readiness"] = {
            "symbols_total": 1,
            "intervals": {
                "5m": {
                    "status": "blocked",
                    "symbols_total": 1,
                    "missing_ready_symbols_total": 1,
                    "missing_ready_symbols": ["AAPL"],
                }
            },
        }

        live = api_app_mod._build_statusz_live_readiness(compute_payload, runtime_payload)

        self.assertEqual(live["source"], "runtime_warmup_snapshot")
        self.assertTrue(live["gate_open"])
        self.assertTrue(live["trade_allowed"])
        self.assertEqual(live["gate_reason"], "ready")

    def test_statusz_route_canonicalizes_reboot_starting_runtime_state(self):
        compute_result = {"ok": True, "payload": _sample_compute_status_payload(), "error": ""}
        runtime_result = {
            "ok": True,
            "payload": _sample_runtime_status_payload(authenticated=False),
            "error": "",
            "selected_upstream": "http://runtime/ibkr/status",
            "proxy_upstream": "http://compute/ibkr/status",
            "direct_upstream": "http://runtime/ibkr/status",
        }
        with mock.patch.dict(os.environ, {"IBKR_SERVICE_PROFILE": "api", "IBKR_RUNTIME_MODE": "remote"}, clear=False):
            with mock.patch.object(api_app_mod, "_fetch_compute_status", return_value=compute_result):
                with mock.patch.object(api_app_mod, "_fetch_runtime_status", return_value=runtime_result):
                    with mock.patch.object(api_app_mod, "_load_daily_scan_state", return_value={"market_date": "2026-04-22"}):
                        with mock.patch.object(api_app_mod, "_count_active_today_targets", return_value=1):
                            with mock.patch.object(api_app_mod.request, "args", {"environment": "live", "lite": "1"}):
                                payload = api_app_mod.custom_ibkr_statusz()

        services = payload["service_topology"]["services"]
        monitor_services = payload["service_monitor"]["services"]
        self.assertEqual(payload["service_topology"]["service_profile"], "api")
        self.assertEqual(services["ibkr-api"]["status"], "running")
        self.assertEqual(services["ibkr-runtime"]["status"], "starting")
        self.assertEqual(services["ibkr-runtime"]["readiness_phase"], "auth_pending")
        self.assertEqual(monitor_services["ibkr-runtime"]["status"], services["ibkr-runtime"]["status"])

    def test_statusz_route_requests_full_compute_status_when_full_requested(self):
        compute_result = {"ok": True, "payload": _sample_compute_status_payload(), "error": ""}
        runtime_result = {
            "ok": True,
            "payload": _sample_runtime_status_payload(authenticated=True),
            "error": "",
            "selected_upstream": "http://runtime/ibkr/status",
            "proxy_upstream": "http://compute/ibkr/status",
            "direct_upstream": "http://runtime/ibkr/status",
        }
        with mock.patch.object(api_app_mod, "_fetch_compute_status", return_value=compute_result) as fetch_compute_status:
            with mock.patch.object(api_app_mod, "_fetch_runtime_status", return_value=runtime_result):
                with mock.patch.object(api_app_mod, "_load_daily_scan_state", return_value={"market_date": "2026-04-22"}):
                    with mock.patch.object(api_app_mod, "_count_active_today_targets", return_value=1):
                        with mock.patch.object(api_app_mod.request, "args", {"environment": "live", "full": "1"}):
                            payload = api_app_mod.custom_ibkr_statusz()

        self.assertTrue(payload["warmup_details_included"])
        fetch_compute_status.assert_called_once_with("live", include_engines=True)

    def test_healthz_route_returns_merged_compute_runtime_health(self):
        compute_result = {
            "ok": True,
            "payload": {
                "ok": True,
                "status": "running",
                "service_topology": api_app_mod.build_service_topology(),
            },
            "error": "",
        }
        runtime_result = {
            "ok": True,
            "payload": {
                "ok": True,
                "status": "running",
                "environment": "live",
                "service_topology": api_app_mod.build_service_topology(),
            },
            "error": "",
            "upstream": "http://runtime/health",
        }
        with mock.patch.object(api_app_mod, "_fetch_compute_health", return_value=compute_result):
            with mock.patch.object(api_app_mod, "_fetch_runtime_health", return_value=runtime_result):
                with mock.patch.object(api_app_mod.request, "args", {"environment": "live"}):
                    payload = api_app_mod.custom_ibkr_healthz()

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "running")
        self.assertEqual(payload["actual_runtime_environment"], "live")
        self.assertIn("ibkr-runtime", payload["service_topology"]["services"])

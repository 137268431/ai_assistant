from control_plane_split_stack_helpers import *


class ControlPlaneSplitStackSchedulerJobsTest(unittest.TestCase):
    def test_system_signal_expiry_job_route_uses_native_builder(self):
        sentinel = {"ok": True, "expired_count": 1, "source": "ibkr-api"}
        with mock.patch.object(api_app_mod.request, "get_json", return_value={"environment": "live"}):
            with mock.patch.object(api_app_mod, "build_signal_expiry_response", return_value=(sentinel, 200)) as builder_mock:
                payload = api_app_mod.custom_system_job_signal_expiry()
        self.assertEqual(payload["expired_count"], 1)
        builder_mock.assert_called_once()

    def test_system_order_detail_integrity_job_route_uses_native_builder(self):
        sentinel = {"success": True, "summary": {"repaired": 1}, "source": "ibkr-api"}
        with mock.patch.object(api_app_mod.request, "get_json", return_value={"environment": "live"}):
            with mock.patch.object(api_app_mod, "build_order_detail_integrity_response", return_value=(sentinel, 200)) as builder_mock:
                payload = api_app_mod.custom_system_job_order_detail_integrity()
        self.assertEqual(payload["summary"]["repaired"], 1)
        builder_mock.assert_called_once()

    def test_schedulerz_route_returns_scheduler_summary(self):
        scheduler_payload = {
            "ok": True,
            "status": "running",
            "environment": "live",
            "loop_interval_seconds": 30,
            "ingest_cursor": {"intervals": {"5m": {"latest_bar_time_ms": 1713797100000}}},
            "compute_dispatch_cursor": {"intervals": {"5m": {"latest_bar_time_ms": 1713796800000, "last_dispatched_at_ms": 1713797160000}}},
            "jobs": {
                "ibkr_compute_runtime": {
                    "status": "ok",
                    "last_success_at_ms": 1713797160000,
                }
            },
        }
        with mock.patch.object(api_app_mod.config, "refresh", return_value=None):
            with mock.patch.object(api_app_mod, "_scheduler_status", return_value=scheduler_payload):
                with mock.patch.object(api_app_mod.request, "args", {"environment": "live"}):
                    payload = api_app_mod.custom_system_schedulerz()

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["scheduler"]["job_count"], 1)
        self.assertEqual(payload["scheduler"]["dispatch_lag_min"], 5.0)
        self.assertIn("ibkr-scheduler", payload["service_topology"]["services"])

    def test_manual_scheduler_scan_job_route_proxies_allowlisted_job(self):
        calls = []

        def fake_request_json_request(method, base_url, path, params=None, json_body=None, timeout=5.0):
            calls.append(
                {
                    "method": method,
                    "base_url": base_url,
                    "path": path,
                    "params": params,
                    "json_body": json_body,
                    "timeout": timeout,
                }
            )
            return {
                "ok": True,
                "status_code": 200,
                "target_url": "http://scheduler/jobs/run/ibkr_scan_runtime",
                "error": "",
                "payload": {
                    "ok": True,
                    "status_code": 200,
                    "payload": {
                        "ok": True,
                        "active": 2,
                        "candidates": 3,
                        "removed": 1,
                        "errors": 0,
                    },
                },
            }

        with mock.patch.object(
            api_app_mod.request,
            "get_json",
            return_value={
                "environment": "live",
                "job_id": "ibkr_scan_runtime",
                "trigger_source": "console_manual_daily_scan",
            },
        ):
            with mock.patch.object(api_app_mod, "_request_json_request", side_effect=fake_request_json_request):
                payload = api_app_mod.custom_system_scheduler_job_run()

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["job_id"], "ibkr_scan_runtime")
        self.assertEqual(payload["scan_result"]["active"], 2)
        self.assertEqual(calls[0]["method"], "POST")
        self.assertEqual(calls[0]["path"], "/jobs/run/ibkr_scan_runtime")
        self.assertEqual(calls[0]["json_body"]["trigger_source"], "console_manual_daily_scan")
        self.assertNotIn("scheduled_slot", calls[0]["json_body"])

    def test_manual_scheduler_job_route_rejects_non_allowlisted_job(self):
        with mock.patch.object(
            api_app_mod.request,
            "get_json",
            return_value={
                "environment": "live",
                "job_id": "system_daily_report",
            },
        ):
            payload, status_code = api_app_mod.custom_system_scheduler_job_run()

        self.assertEqual(status_code, 400)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "unsupported_scheduler_job")
        self.assertEqual(payload["allowed_jobs"], ["ibkr_scan_runtime"])

    def test_scheduler_dispatches_only_new_persisted_bars_and_updates_cursor(self):
        pb = _FakePB()
        pb.states[(BAR_INGEST_CURSOR_STATE_KEY, "live", "global")] = {
            "data": {"intervals": {"5m": {"latest_bar_time_ms": 1713797100000}}}
        }
        pb.states[(COMPUTE_DISPATCH_CURSOR_STATE_KEY, "live", "global")] = {
            "data": {"intervals": {"5m": {"latest_bar_time_ms": 1713796800000}}}
        }
        scheduler = SchedulerService(pb, _FakeConfig())

        with mock.patch("ibkr_scheduler.scheduler_app.requests.post", return_value=_FakeResponse({"ok": True, "processed": 3})):
            result = scheduler.run_job("ibkr_compute_runtime", market_data_mode="live", trigger_source="api_manual")

        self.assertTrue(result["ok"])
        dispatch = pb.states[(COMPUTE_DISPATCH_CURSOR_STATE_KEY, "live", "global")]["data"]
        self.assertEqual(dispatch["intervals"]["5m"]["latest_bar_time_ms"], 1713797100000)
        job_state = pb.states[("ibkr_scheduler_job_state:ibkr_compute_runtime", "live", "global")]["data"]
        self.assertEqual(job_state["status"], "ok")
        self.assertGreater(int(job_state["last_success_at_ms"]), 0)
        self.assertTrue(any(collection == "system_events" for collection, _ in pb.records))

    def test_scheduler_cron_payload_includes_native_system_visibility_jobs(self):
        items = build_cron_payload(_FakeConfig(), "live", {})
        item_ids = {item["id"] for item in items}
        self.assertIn("system_heartbeat", item_ids)
        self.assertIn("system_monitor_alert_guard", item_ids)
        self.assertIn("system_status_reminder", item_ids)
        market_open = next(item for item in items if item["id"] == "system_market_open_reminder")
        self.assertIn("system_scan_summary", market_open["deprecated_aliases"])

    def test_system_heartbeat_job_persists_issue_state(self):
        states = {}
        events = []

        def get_state_payload(state_key, environment):
            return {"data": states.get((state_key, environment), {})}

        def upsert_state(state_key, environment, data, date):
            states[(state_key, environment)] = dict(data)
            return data

        def emit_system_event(**kwargs):
            events.append(kwargs)
            return {"ok": True, "notified": True}

        payload, status_code = build_system_heartbeat_response(
            payload={"environment": "live"},
            normalize_environment=lambda value, default="live": str(value or default),
            time_strings=lambda: {"us": "2026-04-23 12:00:00", "cn": "2026-04-24 00:00:00", "date": "2026-04-23"},
            build_system_summary_payload=lambda environment, lite_mode=False: {
                "status": "degraded",
                "ibkr_compute": {"status": "running"},
                "ibkr_runtime": {"status": "degraded"},
                "today": {"ibkr_bars": 1, "ibkr_signals": 0, "orders": 0},
            },
            build_system_monitor_payload=lambda environment: {
                "status": "warning",
                "runtime": {
                    "status": "degraded",
                    "session": {"authenticated": False},
                    "websocket": {"connected": False},
                    "gateway": {"running": True},
                },
                "compute": {"status": "running"},
                "scheduler": {"status": "running", "latest_ingested_bar_time_ms": 1, "dispatch_lag_min": 2.5},
                "service_monitor": {"status_counts": {"running": 5, "degraded": 1}},
                "flags": [{"code": "session_unauthenticated", "severity": "warning"}],
            },
            emit_system_event=emit_system_event,
            get_state_payload=get_state_payload,
            upsert_state=upsert_state,
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["unhealthy"])
        self.assertEqual(payload["job_id"], "system_heartbeat")
        self.assertTrue(events)
        detail = events[0]["detail"]
        self.assertIn("影响", detail)
        self.assertIn("原因", detail)
        self.assertIn("建议", detail)
        self.assertIn("IBKR 会话尚未认证", detail["原因"])
        self.assertIn("实时行情 WebSocket", detail["影响"])
        self.assertNotIn("问题码", detail)
        self.assertIn("诊断码", detail)
        self.assertIn(("system_notify_heartbeat", "live"), states)
        self.assertTrue(states[("system_notify_heartbeat", "live")]["last_issue_hash"])

    def test_system_heartbeat_ignores_nominal_info_flag(self):
        states = {}
        events = []

        def get_state_payload(state_key, environment):
            return {"data": states.get((state_key, environment), {})}

        def upsert_state(state_key, environment, data, date):
            states[(state_key, environment)] = dict(data)
            return data

        def emit_system_event(**kwargs):
            events.append(kwargs)
            return {"ok": True, "notified": True}

        payload, status_code = build_system_heartbeat_response(
            payload={"environment": "live"},
            normalize_environment=lambda value, default="live": str(value or default),
            time_strings=lambda: {"us": "2026-04-23 12:01:00", "cn": "2026-04-24 00:01:00", "date": "2026-04-23"},
            build_system_summary_payload=lambda environment, lite_mode=False: {
                "status": "running",
                "ibkr_compute": {"status": "running"},
                "ibkr_runtime": {"status": "running"},
                "today": {"ibkr_bars": 1, "ibkr_signals": 0, "orders": 0},
            },
            build_system_monitor_payload=lambda environment: {
                "status": "ok",
                "runtime": {
                    "status": "running",
                    "session": {"authenticated": True},
                    "websocket": {"connected": True, "ready": True},
                    "gateway": {"running": True, "reachable": True},
                },
                "compute": {"status": "running"},
                "scheduler": {"status": "running", "latest_ingested_bar_time_ms": 1, "dispatch_lag_min": 0.0},
                "service_monitor": {
                    "status_counts": {"running": 6, "degraded": 1},
                    "services": {
                        "ibkr-compute": {
                            "status": "degraded",
                            "detail": "engines 0/354",
                            "fault_domain": "compute_plane",
                        },
                    },
                },
                "flags": [{"code": "monitor_nominal", "severity": "info"}],
            },
            emit_system_event=emit_system_event,
            get_state_payload=get_state_payload,
            upsert_state=upsert_state,
        )

        self.assertEqual(status_code, 200)
        self.assertFalse(payload["unhealthy"])
        self.assertEqual(payload["issue_codes"], [])
        self.assertFalse(events)

    def test_system_heartbeat_info_hides_ignored_degraded_counts(self):
        states = {}
        events = []

        def get_state_payload(state_key, environment):
            return {"data": states.get((state_key, environment), {})}

        def upsert_state(state_key, environment, data, date):
            states[(state_key, environment)] = dict(data)
            return data

        def emit_system_event(**kwargs):
            events.append(kwargs)
            return {"ok": True, "notified": True}

        payload, status_code = build_system_heartbeat_response(
            payload={"environment": "live", "emit_nominal_ok": True},
            normalize_environment=lambda value, default="live": str(value or default),
            time_strings=lambda: {"us": "2026-04-23 12:00:00", "cn": "2026-04-24 00:00:00", "date": "2026-04-23"},
            build_system_summary_payload=lambda environment, lite_mode=False: {
                "status": "running",
                "ibkr_compute": {"status": "running"},
                "ibkr_runtime": {"status": "running"},
                "today": {"ibkr_bars": 1, "ibkr_signals": 0, "orders": 0},
            },
            build_system_monitor_payload=lambda environment: {
                "status": "ok",
                "runtime": {
                    "status": "running",
                    "session": {"authenticated": True},
                    "websocket": {"connected": True, "ready": True},
                    "gateway": {"running": True, "reachable": True},
                },
                "compute": {"status": "running"},
                "scheduler": {"status": "running", "latest_ingested_bar_time_ms": 1, "dispatch_lag_min": 0.0},
                "service_monitor": {
                    "status_counts": {"running": 6, "degraded": 1},
                    "services": {
                        "ibkr-compute": {
                            "status": "degraded",
                            "detail": "engines 0/354",
                            "fault_domain": "compute_plane",
                        },
                    },
                },
                "flags": [{"code": "monitor_nominal", "severity": "info"}],
            },
            emit_system_event=emit_system_event,
            get_state_payload=get_state_payload,
            upsert_state=upsert_state,
        )

        self.assertEqual(status_code, 200)
        self.assertFalse(payload["unhealthy"])
        self.assertEqual(payload["issue_codes"], [])
        self.assertTrue(events)
        detail = events[0]["detail"]
        self.assertNotIn("故障域统计", detail)
        self.assertNotIn("诊断码", detail)
        self.assertEqual(detail["结论"], "系统与 IBKR 连接正常。")

    def test_system_heartbeat_suppresses_nominal_ok_by_default(self):
        states = {}
        events = []

        def get_state_payload(state_key, environment):
            return {"data": states.get((state_key, environment), {})}

        def upsert_state(state_key, environment, data, date):
            states[(state_key, environment)] = dict(data)
            return data

        def emit_system_event(**kwargs):
            events.append(kwargs)
            return {"ok": True, "notified": True}

        payload, status_code = build_system_heartbeat_response(
            payload={"environment": "live"},
            normalize_environment=lambda value, default="live": str(value or default),
            time_strings=lambda: {"us": "2026-04-23 12:00:00", "cn": "2026-04-24 00:00:00", "date": "2026-04-23"},
            build_system_summary_payload=lambda environment, lite_mode=False: {
                "status": "running",
                "ibkr_compute": {"status": "running"},
                "ibkr_runtime": {"status": "running"},
                "today": {"ibkr_bars": 1, "ibkr_signals": 0, "orders": 0},
            },
            build_system_monitor_payload=lambda environment: {
                "status": "ok",
                "runtime": {
                    "status": "running",
                    "session": {"authenticated": True},
                    "websocket": {"connected": True, "ready": True},
                    "gateway": {"running": True, "reachable": True},
                },
                "compute": {"status": "running"},
                "scheduler": {"status": "running", "latest_ingested_bar_time_ms": 1, "dispatch_lag_min": 0.0},
                "service_monitor": {"status_counts": {"running": 6}},
                "flags": [{"code": "monitor_nominal", "severity": "info"}],
            },
            emit_system_event=emit_system_event,
            get_state_payload=get_state_payload,
            upsert_state=upsert_state,
        )

        self.assertEqual(status_code, 200)
        self.assertFalse(payload["unhealthy"])
        self.assertTrue(payload["nominal_ok_suppressed"])
        self.assertFalse(events)
        self.assertEqual(states[("system_notify_heartbeat", "live")]["last_ok_suppressed_hour"], "2026-04-23 12")

    def test_system_heartbeat_reports_actionable_degraded_service_name(self):
        states = {}
        events = []

        def get_state_payload(state_key, environment):
            return {"data": states.get((state_key, environment), {})}

        def upsert_state(state_key, environment, data, date):
            states[(state_key, environment)] = dict(data)
            return data

        def emit_system_event(**kwargs):
            events.append(kwargs)
            return {"ok": True, "notified": True}

        payload, status_code = build_system_heartbeat_response(
            payload={"environment": "live"},
            normalize_environment=lambda value, default="live": str(value or default),
            time_strings=lambda: {"us": "2026-04-23 12:02:00", "cn": "2026-04-24 00:02:00", "date": "2026-04-23"},
            build_system_summary_payload=lambda environment, lite_mode=False: {
                "status": "running",
                "ibkr_compute": {"status": "running"},
                "ibkr_runtime": {"status": "running"},
                "today": {"ibkr_bars": 1, "ibkr_signals": 0, "orders": 0},
            },
            build_system_monitor_payload=lambda environment: {
                "status": "ok",
                "runtime": {
                    "status": "running",
                    "session": {"authenticated": True},
                    "websocket": {"connected": True, "ready": True},
                    "gateway": {"running": True, "reachable": True},
                },
                "compute": {"status": "running"},
                "scheduler": {"status": "running", "latest_ingested_bar_time_ms": 1, "dispatch_lag_min": 0.0},
                "service_monitor": {
                    "status_counts": {"running": 6, "degraded": 1},
                    "services": {
                        "ibkr-scheduler": {
                            "status": "degraded",
                            "detail": "lag 12.00m",
                            "fault_domain": "scheduler",
                        },
                    },
                },
                "flags": [{"code": "monitor_nominal", "severity": "info"}],
            },
            emit_system_event=emit_system_event,
            get_state_payload=get_state_payload,
            upsert_state=upsert_state,
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["unhealthy"])
        self.assertEqual(payload["issue_codes"], ["services_degraded:1"])
        self.assertTrue(events)
        self.assertIn("ibkr-scheduler degraded", events[0]["detail"]["原因"])

    def test_system_heartbeat_emits_partial_recovery_when_service_offline_clears(self):
        states = {
            ("system_notify_heartbeat", "paper"): {
                "last_issue_hash": "previous-offline",
                "last_issue_ms": 1779100000000,
                "last_issue_codes": ["services_offline:1"],
            }
        }
        events = []

        def get_state_payload(state_key, environment):
            return {"data": states.get((state_key, environment), {})}

        def upsert_state(state_key, environment, data, date):
            states[(state_key, environment)] = dict(data)
            return data

        def emit_system_event(**kwargs):
            events.append(kwargs)
            return {"ok": True, "notified": True, "title": kwargs.get("title")}

        payload, status_code = build_system_heartbeat_response(
            payload={"environment": "paper"},
            normalize_environment=lambda value, default="live": str(value or default),
            time_strings=lambda: {"us": "2026-05-18 09:40:33", "cn": "2026-05-18 21:40:33", "date": "2026-05-18"},
            build_system_summary_payload=lambda environment, lite_mode=False: {
                "status": "running",
                "ibkr_compute": {"status": "running"},
                "ibkr_runtime": {"status": "running"},
                "today": {"ibkr_bars": 8207, "ibkr_signals": 0, "orders": 0},
            },
            build_system_monitor_payload=lambda environment: {
                "status": "warning",
                "runtime": {
                    "status": "running",
                    "session": {"authenticated": True},
                    "websocket": {"connected": True, "ready": True},
                    "gateway": {"running": True, "reachable": True},
                },
                "compute": {"status": "running"},
                "scheduler": {"status": "running", "latest_ingested_bar_time_ms": 0, "dispatch_lag_min": 0.0},
                "service_monitor": {
                    "status_counts": {"running": 8},
                    "services": {
                        "ibkr-runtime": {"status": "running", "ib_gateway_client_id": 31},
                        "ibkr-compute": {"status": "running", "ib_gateway_client_id": 51},
                        "ibkr-api": {"status": "running", "ib_gateway_client_id": 61},
                        "ibkr-scheduler": {"status": "running", "ib_gateway_client_id": 71},
                        "ibkr-backtest": {"status": "running", "worker_status": "idle", "ib_gateway_client_id": 81},
                    },
                },
                "flags": [
                    {
                        "code": "no_active_targets",
                        "severity": "warning",
                        "title": "No active trade targets",
                        "detail": "watchlist 中存在交易标的，但当前 active target 数为 0",
                    }
                ],
            },
            emit_system_event=emit_system_event,
            get_state_payload=get_state_payload,
            upsert_state=upsert_state,
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["unhealthy"])
        self.assertTrue(payload["partial_recovery"])
        self.assertEqual(events[0]["title"], "IBKR 系统部分恢复")
        self.assertEqual(events[0]["level"], "info")
        self.assertIn("services_offline:1", events[0]["detail"]["已恢复诊断码"])
        self.assertIn("no_active_targets", events[0]["detail"]["仍存在诊断码"])
        self.assertEqual(events[1]["title"], "IBKR 系统心跳异常")
        self.assertEqual(states[("system_notify_heartbeat", "paper")]["last_partial_recovery_at"], "2026-05-18 09:40:33")

    def test_system_status_reminder_includes_backtest_service_semantics(self):
        events = []

        payload, status_code = build_system_status_reminder_response(
            payload={"environment": "live"},
            normalize_environment=lambda value, default="live": str(value or default),
            time_strings=lambda: {"us": "2026-04-23 12:12:00", "cn": "2026-04-24 00:12:00", "date": "2026-04-23"},
            build_system_summary_payload=lambda environment, lite_mode=False: {
                "status": "running",
                "ibkr_compute": {"status": "running"},
                "ibkr_runtime": {"status": "running"},
                "today": {"ibkr_bars": 1, "ibkr_signals": 0, "orders": 0},
            },
            build_system_monitor_payload=lambda environment: {
                "status": "ok",
                "runtime": {
                    "status": "running",
                    "session": {"authenticated": True},
                    "websocket": {"connected": True, "ready": True},
                    "gateway": {"running": True, "reachable": True},
                },
                "compute": {"status": "running"},
                "scheduler": {"status": "running", "latest_ingested_bar_time_ms": 1, "dispatch_lag_min": 0.0},
                "service_monitor": {
                    "status_counts": {"running": 8},
                    "services": {
                        "ibkr-runtime": {"status": "running", "ib_gateway_client_id": 31},
                        "ibkr-compute": {"status": "running", "ib_gateway_client_id": 51},
                        "ibkr-api": {"status": "running", "ib_gateway_client_id": 61},
                        "ibkr-scheduler": {"status": "running", "ib_gateway_client_id": 71},
                        "ibkr-backtest": {
                            "status": "running",
                            "worker_status": "idle",
                            "ib_gateway_client_id": 81,
                        },
                    },
                },
                "flags": [],
            },
            emit_system_event=lambda **kwargs: events.append(kwargs) or {"ok": True, "notified": True},
        )

        self.assertEqual(status_code, 200)
        self.assertEqual(payload["job_id"], "system_status_reminder")
        self.assertTrue(events)
        detail = events[0]["detail"]
        self.assertIn("Backtest running", detail["系统服务"])
        self.assertIn("worker idle", detail["Backtest"])
        self.assertIn("client 81", detail["Backtest"])
        self.assertIn("non-blocking", detail["Backtest"])
        self.assertIn("Runtime client 31", detail["IB ClientID"])
        self.assertIn("Compute client 51", detail["IB ClientID"])
        self.assertIn("API client 61", detail["IB ClientID"])
        self.assertIn("Scheduler client 71", detail["IB ClientID"])
        self.assertIn("Backtest client 81", detail["IB ClientID"])
        self.assertIn("running:8", detail["服务统计"])

    def test_system_monitor_alert_job_emits_on_warning_flags(self):
        states = {}
        events = []

        def get_state_payload(state_key, environment):
            return {"data": states.get((state_key, environment), {})}

        def upsert_state(state_key, environment, data, date):
            states[(state_key, environment)] = dict(data)
            return data

        def emit_system_event(**kwargs):
            events.append(kwargs)
            return {"ok": True, "notified": True}

        payload, status_code = build_system_monitor_alert_guard_response(
            payload={"environment": "live"},
            normalize_environment=lambda value, default="live": str(value or default),
            time_strings=lambda: {"us": "2026-04-23 12:05:00", "cn": "2026-04-24 00:05:00", "date": "2026-04-23"},
            build_system_monitor_payload=lambda environment: {
                "status": "warning",
                "flags": [{"code": "websocket_not_ready", "severity": "warning", "title": "WS", "detail": "offline"}],
                "runtime": {"session": {"authenticated": False}, "websocket": {"connected": False}},
                "scheduler": {"latest_ingested_bar_time_ms": 1, "dispatch_lag_min": 4.0},
                "service_monitor": {
                    "status_counts": {"running": 7, "degraded": 1},
                    "services": {
                        "ibkr-runtime": {"status": "degraded", "ib_gateway_client_id": 31},
                        "ibkr-compute": {"status": "running", "ib_gateway_client_id": 51},
                        "ibkr-api": {"status": "running", "ib_gateway_client_id": 61},
                        "ibkr-scheduler": {"status": "running", "ib_gateway_client_id": 71},
                        "ibkr-backtest": {
                            "status": "running",
                            "worker_status": "idle",
                            "ib_gateway_client_id": 81,
                        },
                    },
                },
                "pocketbase": {"disk": {"filesystem": {"used_pct": 10}}},
            },
            emit_system_event=emit_system_event,
            get_state_payload=get_state_payload,
            upsert_state=upsert_state,
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["triggered"])
        self.assertEqual(payload["job_id"], "system_monitor_alert_guard")
        self.assertEqual(events[0]["title"], "IBKR Monitor 告警（1项）")
        self.assertIn("client 81", events[0]["detail"]["Backtest"])
        self.assertIn("Runtime client 31", events[0]["detail"]["IB ClientID"])
        self.assertIn("Backtest client 81", events[0]["detail"]["IB ClientID"])
        self.assertIn("running:7", events[0]["detail"]["服务统计"])
        self.assertIn(("system_monitor_alert", "live"), states)

import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.broker.ib_gateway import _IBGatewayApp, _PendingRequest
from ibkr_compute.orchestration.auth_recovery import TradingServiceAuthRecoveryMixin


class BrokerReadyGuardTest(unittest.TestCase):
    def test_requests_fail_fast_when_broker_not_ready(self):
        scenarios = [
            ("request_open_orders", "reqOpenOrders"),
            ("request_positions", "reqPositions"),
            ("request_account_summary", "reqAccountSummary"),
        ]

        for method_name, request_attr in scenarios:
            with self.subTest(method=method_name):
                app = _IBGatewayApp("127.0.0.1", 4001, 31)
                app.connect_and_start = mock.Mock(return_value=False)
                app.status = mock.Mock(
                    return_value={
                        "ready": False,
                        "connected": False,
                        "status_code": 503,
                        "last_error_code": 502,
                        "last_error": "Couldn't connect to TWS",
                    }
                )
                setattr(app, request_attr, mock.Mock())

                with self.assertRaisesRegex(
                    RuntimeError,
                    rf"{method_name}:broker_not_ready:status_code=503:last_error_code=502",
                ):
                    getattr(app, method_name)()

                getattr(app, request_attr).assert_not_called()

    def test_reconnect_uses_stored_host_and_port_after_disconnect_reset(self):
        app = _IBGatewayApp("127.0.0.1", 4001, 31)
        app.host = None
        app.port = None
        app.isConnected = mock.Mock(return_value=False)
        app._start_network_thread_locked = mock.Mock()

        with mock.patch("ibkr_compute.broker.ib_gateway.IBAPI_AVAILABLE", True):
            with mock.patch("ibkr_compute.broker.ib_gateway.EClient.connect", create=True) as connect_mock:
                with mock.patch.object(app._ready_event, "wait", return_value=True):
                    ready = app.connect_and_start(timeout=3)

        self.assertTrue(ready)
        connect_mock.assert_called_once_with("127.0.0.1", 4001, 31)

    def test_account_data_circuit_fails_fast_after_repeated_timeouts(self):
        app = _IBGatewayApp("127.0.0.1", 4001, 31)
        app._ensure_ready = mock.Mock(return_value={"ready": True})
        app.reqPositions = mock.Mock()

        for kind in ("positions", "open_orders", "account_summary", "executions"):
            app._record_account_data_issue(kind, f"{kind}_timeout")

        with self.assertRaisesRegex(TimeoutError, "account_data_circuit_open"):
            app.request_positions(timeout=1)

        app.reqPositions.assert_not_called()
        self.assertTrue(app.status()["account_data_circuit"]["active"])

    def test_duplicate_account_data_timeouts_are_deduped_for_circuit(self):
        app = _IBGatewayApp("127.0.0.1", 4001, 31)

        for _ in range(4):
            app._record_account_data_issue("positions", "positions_timeout")

        circuit = app.status()["account_data_circuit"]
        self.assertFalse(circuit["active"])
        self.assertEqual(1, circuit["recent_failure_count"])
        self.assertEqual("timeout", circuit["recent_failures"][0]["family"])

    def test_account_data_control_flow_reasons_do_not_trip_circuit(self):
        app = _IBGatewayApp("127.0.0.1", 4001, 31)

        for reason in (
            "request_positions:broker_not_ready:status_code=401:ready_timeout",
            "account_data_pacing_cooldown:positions:reason=min_interval:retry_after_s=30.0",
            "account_data_circuit_open:positions_timeout:retry_after_s=60.0",
        ):
            for _ in range(4):
                app._record_account_data_issue("positions", reason)

        circuit = app.status()["account_data_circuit"]
        self.assertFalse(circuit["active"])
        self.assertEqual(0, circuit["recent_failure_count"])

    def test_account_data_circuit_clears_after_success(self):
        app = _IBGatewayApp("127.0.0.1", 4001, 31)
        for kind in ("positions", "open_orders", "account_summary", "executions"):
            app._record_account_data_issue(kind, f"{kind}_timeout")

        self.assertTrue(app.status()["account_data_circuit"]["active"])

        app._record_account_data_success("account_summary")

        self.assertFalse(app.status()["account_data_circuit"]["active"])
        self.assertEqual(0, app.status()["account_data_circuit"]["recent_failure_count"])

    def test_account_data_unsubscribed_errors_contribute_to_circuit(self):
        app = _IBGatewayApp("127.0.0.1", 4001, 31)
        for _ in range(4):
            app.error(-1, 2100, "API client has been unsubscribed from account data.")

        circuit = app.status()["account_data_circuit"]
        self.assertFalse(circuit["active"])
        self.assertEqual(1, circuit["recent_failure_count"])
        self.assertEqual("subscription", circuit["recent_failures"][0]["family"])

    def test_expected_account_update_unsubscribe_does_not_trip_circuit(self):
        app = _IBGatewayApp("127.0.0.1", 4001, 31)

        for _ in range(4):
            app._mark_expected_account_updates_unsubscribe()
            app.error(-1, 2100, "API client has been unsubscribed from account data.")

        circuit = app.status()["account_data_circuit"]
        self.assertFalse(circuit["active"])
        self.assertEqual(0, circuit["recent_failure_count"])

    def test_account_summary_cancels_subscription_and_uses_cache(self):
        app = _IBGatewayApp("127.0.0.1", 4001, 31)
        app._managed_accounts = "DU123"
        app._ensure_ready = mock.Mock(return_value={"ready": True})
        app.cancelAccountSummary = mock.Mock()

        def req_summary(req_id, _group, _tags):
            app.accountSummary(req_id, "DU123", "NetLiquidation", "100000", "USD")
            app.accountSummary(req_id, "DU123", "BuyingPower", "50000", "USD")
            app.accountSummaryEnd(req_id)

        app.reqAccountSummary = mock.Mock(side_effect=req_summary)

        first = app.request_account_summary(timeout=1)
        second = app.request_account_summary(timeout=1)

        self.assertIn("BuyingPower", first)
        self.assertEqual(first, second)
        app.reqAccountSummary.assert_called_once()
        app.cancelAccountSummary.assert_called_once()

    def test_account_summary_cancels_on_timeout(self):
        app = _IBGatewayApp("127.0.0.1", 4001, 31)
        app._ensure_ready = mock.Mock(return_value={"ready": True})
        app.reqAccountSummary = mock.Mock()
        app.cancelAccountSummary = mock.Mock()
        app._await = mock.Mock(side_effect=TimeoutError("account_summary_timeout"))

        with self.assertRaisesRegex(TimeoutError, "account_summary_timeout"):
            app.request_account_summary(timeout=1)

        app.cancelAccountSummary.assert_called_once()

    def test_positions_cancel_after_snapshot(self):
        app = _IBGatewayApp("127.0.0.1", 4001, 31)
        app._ensure_ready = mock.Mock(return_value={"ready": True})
        app.reqPositions = mock.Mock()
        app.cancelPositions = mock.Mock()
        app._await = mock.Mock(return_value=[{"ticker": "SPY", "position": 1}])

        positions = app.request_positions(timeout=1)

        self.assertEqual([{"ticker": "SPY", "position": 1}], positions)
        app.cancelPositions.assert_called_once()

    def test_account_data_requests_are_serialized_across_kinds(self):
        app = _IBGatewayApp("127.0.0.1", 4001, 31)
        app._managed_accounts = "DU123"
        app._ensure_ready = mock.Mock(return_value={"ready": True})
        events = []
        positions_started = threading.Event()
        release_positions = threading.Event()

        def req_positions():
            events.append("positions_start")
            positions_started.set()
            self.assertTrue(release_positions.wait(timeout=2.0))
            app.positionEnd()
            events.append("positions_end")

        def req_summary(req_id, _group, _tags):
            events.append("summary_start")
            app.accountSummary(req_id, "DU123", "BuyingPower", "100000", "USD")
            app.accountSummaryEnd(req_id)
            events.append("summary_end")

        app.reqPositions = mock.Mock(side_effect=req_positions)
        app.cancelPositions = mock.Mock()
        app.reqAccountSummary = mock.Mock(side_effect=req_summary)
        app.cancelAccountSummary = mock.Mock()

        positions_result = []
        summary_result = {}
        positions_thread = threading.Thread(target=lambda: positions_result.extend(app.request_positions(timeout=2)))
        summary_thread = threading.Thread(target=lambda: summary_result.update(app.request_account_summary(timeout=2)))

        positions_thread.start()
        self.assertTrue(positions_started.wait(timeout=1.0))
        summary_thread.start()
        time.sleep(0.1)
        self.assertNotIn("summary_start", events)
        release_positions.set()
        positions_thread.join(timeout=2.0)
        summary_thread.join(timeout=2.0)

        self.assertEqual(["positions_start", "positions_end", "summary_start", "summary_end"], events)
        self.assertEqual([], positions_result)
        self.assertIn("BuyingPower", summary_result)

    def test_positions_use_stale_cache_while_account_data_circuit_open(self):
        app = _IBGatewayApp("127.0.0.1", 4001, 31)
        app._ensure_ready = mock.Mock(return_value={"ready": True})
        app.reqPositions = mock.Mock()
        app.cancelPositions = mock.Mock()
        app._account_cache_store(("positions",), [{"ticker": "AAPL", "position": 3}], ttl_seconds=0.01)
        time.sleep(0.02)

        for kind in ("positions", "open_orders", "account_summary", "executions"):
            app._record_account_data_issue(kind, f"{kind}_timeout")

        positions = app.request_positions(timeout=1)

        self.assertEqual([{"ticker": "AAPL", "position": 3}], positions)
        app.reqPositions.assert_not_called()
        app.cancelPositions.assert_not_called()

    def test_account_summary_pacing_cooldown_uses_stale_cache(self):
        app = _IBGatewayApp("127.0.0.1", 4001, 31)
        app._ensure_ready = mock.Mock(return_value={"ready": True})
        app.reqAccountSummary = mock.Mock()
        app.cancelAccountSummary = mock.Mock()
        app._account_cache_store(("account_summary",), {"BuyingPower": {"value": "50000"}}, ttl_seconds=0.01)
        time.sleep(0.02)
        app._mark_account_request_cooldown("account_summary", "account_summary_request_limit", 60.0)

        summary = app.request_account_summary(timeout=1)

        self.assertIn("BuyingPower", summary)
        app.reqAccountSummary.assert_not_called()
        app.cancelAccountSummary.assert_not_called()
        pacing = app.status()["account_data_pacing"]["by_kind"]["account_summary"]
        self.assertTrue(pacing["blocked"])
        self.assertEqual("account_summary_request_limit", pacing["blocked_reason"])

    def test_account_summary_322_sets_pacing_cooldown(self):
        app = _IBGatewayApp("127.0.0.1", 4001, 31)
        app._pending_requests[1001] = _PendingRequest(kind="account_summary")

        app.error(
            1001,
            322,
            "Error processing request.-'Q' : cause - Maximum number of account summary requests exceeded; "
            "desubscribe to previous request first",
        )

        pacing = app.status()["account_data_pacing"]["by_kind"]["account_summary"]
        self.assertTrue(pacing["cooldown_active"])
        self.assertEqual("account_summary_request_limit", pacing["cooldown_reason"])


class _DummyStaleBrokerService(TradingServiceAuthRecoveryMixin):
    def __init__(self):
        self._starting = False
        self._running = True
        self.pb = None
        self.auth_handler = SimpleNamespace(_report_2fa_status=lambda **_kwargs: None)
        self._auth_required_reason = "session_expired"
        self._auth_cycle_seq = 0
        self._auth_recovery_lock = threading.RLock()
        self._auth_probe_stop = threading.Event()
        self._auth_probe_thread = None
        self._auth_restart_thread = None
        self._auth_recovery_state = {
            "cycle_id": "cycle-1",
            "recovery_phase": "silent_probe",
            "recovery_class": "scheduled_restart",
            "recovery_reason": "session_expired",
            "interruption_kind": "session_expired",
            "manual_takeover_active": False,
            "manual_takeover_started_at": "",
            "manual_takeover_until": "",
            "probe_started_at": "2026-04-17T16:05:11-04:00",
            "probe_last_checked_at": "2026-04-17T16:05:58-04:00",
            "probe_attempts": 1,
            "probe_result": "self_heal_pending",
            "auto_restart_scheduled": False,
            "last_runtime_authenticated_at": "2026-04-17T14:14:02-04:00",
            "last_gateway_status_code": 401,
            "last_recovery_source": "session_keeper",
            "lock_owner": "auth_probe",
            "lock_expires_at": "",
            "updated_at": "",
        }
        self.restart_calls = []
        self.manual_2fa_calls = []
        self.gateway_manager = SimpleNamespace(
            is_running=True,
            status=lambda: {"status_code": 401},
        )
        self.broker = SimpleNamespace(
            force_reconnect=lambda reason="": {
                "ok": False,
                "ready": False,
                "authenticated": False,
                "status_code": 401,
                "last_error_code": 0,
                "last_error": "",
            },
            fresh_health_probe=lambda reason="": {
                "ok": True,
                "ready": True,
                "authenticated": True,
                "status_code": 200,
                "probe_client_id": 9101,
            },
        )
        self.session_keeper = SimpleNamespace(check_auth_status=lambda: {"authenticated": False})

    def _now_iso(self) -> str:
        return "2026-04-17T16:06:01-04:00"

    def _build_2fa_detail(self, reason: str = "") -> dict:
        return {"reason": reason}

    def _schedule_auth_restart(self, reason: str, source: str, trigger_login: bool = False):
        self.restart_calls.append(
            {
                "reason": reason,
                "source": source,
                "trigger_login": bool(trigger_login),
            }
        )
        return True

    def _request_manual_2fa(self, reason: str, message: str) -> bool:
        self.manual_2fa_calls.append({"reason": reason, "message": message})
        return True


class StaleBrokerRecoveryTest(unittest.TestCase):
    def _service_mod(self, market_session_kind: str = "regular"):
        logger = SimpleNamespace(
            info=lambda *args, **kwargs: None,
            debug=lambda *args, **kwargs: None,
            warning=lambda *args, **kwargs: None,
            error=lambda *args, **kwargs: None,
        )
        return SimpleNamespace(
            logger=logger,
            ET=None,
            AUTH_RECOVERY_LOCK_TTL_SECONDS=120,
            AUTH_PROBE_WINDOW_SECONDS=45,
            AUTH_PROBE_LATE_SESSION_WINDOW_SECONDS=180,
            AUTH_PROBE_SELF_HEAL_GRACE_SECONDS=90,
            AUTH_PROBE_LATE_SESSION_SELF_HEAL_GRACE_SECONDS=300,
            AUTH_PROBE_INTERVAL_SECONDS=1,
            AUTH_RECOVERY_PB_FIELDS=(),
            classify_market_session_kind=lambda: market_session_kind,
        )

    def test_fresh_probe_authentication_schedules_runtime_restart(self):
        service = _DummyStaleBrokerService()

        with mock.patch("ibkr_compute.orchestration.auth_recovery._service_mod", return_value=self._service_mod()):
            recovered, attempts = service._attempt_auth_probe_self_heal(
                cycle_id="cycle-1",
                interruption_kind="session_expired",
                recovery_reason="session_expired",
                source="session_keeper",
                attempts=1,
            )

        self.assertTrue(recovered)
        self.assertGreaterEqual(attempts, 2)
        self.assertEqual(len(service.restart_calls), 1)
        scheduled = service.restart_calls[0]
        self.assertEqual(scheduled["reason"], "stale_inprocess_broker")
        self.assertEqual(scheduled["source"], "auth_probe_fresh_broker")
        self.assertFalse(scheduled["trigger_login"])
        self.assertEqual(service.manual_2fa_calls, [])
        self.assertTrue(service._auth_recovery_state["auto_restart_scheduled"])
        self.assertEqual(service._auth_recovery_state["recovery_class"], "stale_broker")
        self.assertEqual(service._auth_recovery_state["probe_result"], "stale_broker_restart_scheduled")

    def test_late_session_extends_probe_window_and_self_heal_grace(self):
        service = _DummyStaleBrokerService()

        with mock.patch(
            "ibkr_compute.orchestration.auth_recovery._service_mod",
            return_value=self._service_mod(market_session_kind="afterhours"),
        ):
            self.assertEqual(service._auth_probe_window_seconds("session_expired"), 180)
            self.assertEqual(service._auth_probe_self_heal_grace_seconds("session_expired"), 300)

    def test_regular_session_keeps_default_probe_window(self):
        service = _DummyStaleBrokerService()

        with mock.patch(
            "ibkr_compute.orchestration.auth_recovery._service_mod",
            return_value=self._service_mod(market_session_kind="regular"),
        ):
            self.assertEqual(service._auth_probe_window_seconds("session_expired"), 45)
            self.assertEqual(service._auth_probe_self_heal_grace_seconds("session_expired"), 90)


if __name__ == "__main__":
    unittest.main()

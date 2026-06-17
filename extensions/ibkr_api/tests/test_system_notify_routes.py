import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SERVICE_SRC_ROOTS = [
    Path(__file__).resolve().parents[3] / "runtime" / "ibkr_api" / "src",
    Path(__file__).resolve().parents[3] / "runtime" / "ibkr_scheduler" / "src",
    Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src",
]
for src_root in SERVICE_SRC_ROOTS:
    if str(src_root) not in sys.path:
        sys.path.insert(0, str(src_root))

os.environ.setdefault("IBKR_SCHEDULER_AUTOSTART", "false")

if "flask" not in sys.modules:
    import types

    flask_stub = types.ModuleType("flask")

    class _FakeFlask:
        def __init__(self, name):
            self.name = name
            self.logger = SimpleNamespace(debug=lambda *args, **kwargs: None)

        def route(self, _path, methods=None):
            def decorator(func):
                return func

            return decorator

        def before_request(self, func):
            return func

        def after_request(self, func):
            return func

        def teardown_request(self, func):
            return func

    flask_stub.Flask = _FakeFlask
    flask_stub.Response = object
    flask_stub.jsonify = lambda payload: payload
    flask_stub.request = SimpleNamespace(
        args={},
        headers={},
        method="GET",
        get_json=lambda silent=True: {},
        get_data=lambda cache=True: b"",
    )
    sys.modules["flask"] = flask_stub

from ibkr_api import api_app as api_app_mod
from ibkr_api.system.events import build_system_event_card, system_event_chat_id


def _build_system_event_card(title, detail=None, *, level="info"):
    return build_system_event_card(
        level,
        "ibkr_api",
        title,
        detail or {},
        "live",
        normalize_environment=lambda value, default="live": str(value or default),
        add_environment_to_detail=lambda value, environment: dict(value or {}, environment=environment),
        time_strings=lambda: {"us": "2026-06-17 05:45:28", "cn": "2026-06-17 17:45:28"},
        system_page_url=lambda environment: "",
        label_title_with_environment=lambda title, environment: f"[{environment.upper()}] {title}",
    )


class SystemNotifyRoutesTest(unittest.TestCase):
    def test_recovery_event_card_uses_green_header(self):
        card = _build_system_event_card(
            "IBKR 行情会话冲突已恢复",
            {"已恢复诊断码": "market_data_session_conflict", "恢复时间": "2026-06-17 05:41:21"},
        )

        self.assertEqual("green", card["header"]["template"])

    def test_partial_recovery_event_card_uses_green_header(self):
        card = _build_system_event_card(
            "IBKR 系统部分恢复",
            {"已恢复诊断码": "services_offline:1", "仍存在诊断码": "no_active_targets"},
        )

        self.assertEqual("green", card["header"]["template"])

    def test_regular_info_event_card_stays_blue(self):
        card = _build_system_event_card("Compute startup completed", {"reason": "ok"})

        self.assertEqual("blue", card["header"]["template"])

    def test_unrecovered_info_event_card_does_not_match_recovery_color(self):
        card = _build_system_event_card("IBKR Runtime 未恢复", {"诊断码": "runtime_unavailable"})

        self.assertEqual("blue", card["header"]["template"])

    def test_backtest_event_uses_backtest_chat_id(self):
        values = {
            "backtest_chat_id": "oc_backtest",
            "system_status_chat_id": "oc_status",
            "system_alert_chat_id": "oc_alert",
            "system_2fa_chat_id": "oc_2fa",
        }

        chat_id = system_event_chat_id(
            "status_change",
            "info",
            "live",
            "ibkr_compute",
            "Backtest 发现更优同周期结果",
            {"run_id": "run-1", "date_from": "2026-04-01", "date_to": "2026-04-02"},
            normalize_environment=lambda value, default="live": str(value or default),
            config_value=lambda key, default, environment: values.get(key, default),
            default_2fa_chat_id="oc_2fa_default",
            default_alert_chat_id="oc_alert_default",
            default_system_chat_id="oc_status_default",
        )

        self.assertEqual("oc_backtest", chat_id)

    def test_regular_status_event_still_uses_status_chat_id(self):
        values = {
            "backtest_chat_id": "oc_backtest",
            "system_status_chat_id": "oc_status",
        }

        chat_id = system_event_chat_id(
            "status_change",
            "info",
            "live",
            "ibkr_compute",
            "Compute startup completed",
            {"reason": "ok"},
            normalize_environment=lambda value, default="live": str(value or default),
            config_value=lambda key, default, environment: values.get(key, default),
            default_2fa_chat_id="oc_2fa_default",
            default_alert_chat_id="oc_alert_default",
            default_system_chat_id="oc_status_default",
        )

        self.assertEqual("oc_status", chat_id)

    def test_health_report_persists_heartbeat_event(self):
        request_payload = {
            "environment": "paper",
            "et_time": "2026-04-23 09:30:00",
            "bj_time": "2026-04-23 21:30:00",
            "runtime_status": "healthy",
        }

        with mock.patch.object(api_app_mod.request, "get_json", return_value=request_payload):
            with mock.patch.object(api_app_mod.pb, "create_record", return_value={"id": "evt-1"}) as create_mock:
                payload = api_app_mod.custom_ibkr_health_report()

        self.assertTrue(payload["ok"])
        self.assertTrue(payload["persisted"])
        create_mock.assert_called_once_with(
            "system_events",
            {
                "event_type": "heartbeat",
                "level": "info",
                "source": "ibkr_compute",
                "environment": "paper",
                "title": "[Broker PAPER] IBKR 健康上报",
                "detail": {
                    "environment": "paper",
                    "et_time": "2026-04-23 09:30:00",
                    "bj_time": "2026-04-23 21:30:00",
                    "runtime_status": "healthy",
                },
                "us_time": "2026-04-23 09:30:00",
                "cn_time": "2026-04-23 21:30:00",
                "notified": False,
            },
        )

    def test_notify_route_uses_emit_system_event_flow(self):
        request_payload = {
            "environment": "live",
            "type": "warning",
            "title": "Compute degraded",
            "detail": {"reason": "lagging"},
        }

        with mock.patch.object(api_app_mod.request, "get_json", return_value=request_payload):
            with mock.patch.object(
                api_app_mod,
                "_deliver_system_event_notification",
                return_value={"success": True, "message_id": "msg-1"},
            ) as deliver_mock:
                with mock.patch.object(api_app_mod, "_write_system_event_record", return_value={"id": "evt-1"}) as write_mock:
                    payload = api_app_mod.custom_ibkr_notify()

        self.assertTrue(payload["ok"])
        self.assertTrue(payload["notified"])
        self.assertTrue(payload["persisted"])
        self.assertEqual("msg-1", payload["message_id"])
        deliver_mock.assert_called_once_with(
            "status_change",
            "warning",
            "ibkr_compute",
            "Compute degraded",
            {"reason": "lagging"},
            "live",
            message_id="",
        )
        write_mock.assert_called_once_with(
            "status_change",
            "warning",
            "ibkr_compute",
            "Compute degraded",
            {"reason": "lagging"},
            "live",
            True,
        )


if __name__ == "__main__":
    unittest.main()

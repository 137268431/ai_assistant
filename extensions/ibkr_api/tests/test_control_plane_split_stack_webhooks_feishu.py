from control_plane_split_stack_helpers import *


class ControlPlaneSplitStackWebhooksFeishuTest(unittest.TestCase):
    def test_order_cancel_webhook_uses_native_builder(self):
        with mock.patch.object(api_app_mod.request, "args", {"id": "sig-1_entry", "environment": "live"}):
            with mock.patch.object(
                api_app_mod,
                "build_order_cancel_webhook_response",
                return_value=({"body": "<html>cancel</html>", "content_type": "text/html; charset=utf-8"}, 200),
            ) as builder_mock:
                response = api_app_mod.webhook_order_cancel()
        self.assertEqual(response[0], "<html>cancel</html>")
        self.assertEqual(response[1], 200)
        self.assertEqual(response[2]["Content-Type"], "text/html; charset=utf-8")
        builder_mock.assert_called_once()

    def test_order_close_webhook_uses_native_builder(self):
        with mock.patch.object(api_app_mod.request, "args", {"id": "sig-1_entry", "environment": "live"}):
            with mock.patch.object(
                api_app_mod,
                "build_order_close_webhook_response",
                return_value=({"body": "<html>close</html>", "content_type": "text/html; charset=utf-8"}, 200),
            ) as builder_mock:
                response = api_app_mod.webhook_order_close()
        self.assertEqual(response[0], "<html>close</html>")
        self.assertEqual(response[1], 200)
        self.assertEqual(response[2]["Content-Type"], "text/html; charset=utf-8")
        builder_mock.assert_called_once()

    def test_signal_confirm_webhook_uses_native_builder(self):
        with mock.patch.object(api_app_mod.request, "args", {"id": "sig-1", "environment": "live"}):
            with mock.patch.object(api_app_mod, "_config_value", return_value="7") as config_mock:
                with mock.patch.object(
                    api_app_mod,
                    "build_signal_confirm_webhook_response",
                    return_value=({"body": "<html>confirm</html>", "content_type": "text/html; charset=utf-8"}, 200),
                ) as builder_mock:
                    response = api_app_mod.webhook_signal_confirm()
        self.assertEqual(response[0], "<html>confirm</html>")
        self.assertEqual(response[1], 200)
        self.assertEqual(response[2]["Content-Type"], "text/html; charset=utf-8")
        builder_mock.assert_called_once()
        self.assertIn("config_value", builder_mock.call_args.kwargs)
        self.assertTrue(callable(builder_mock.call_args.kwargs["config_value"]))
        config_mock.assert_not_called()

    def test_signal_cancel_webhook_uses_native_builder(self):
        with mock.patch.object(api_app_mod.request, "args", {"id": "sig-1", "environment": "live"}):
            with mock.patch.object(
                api_app_mod,
                "build_signal_cancel_webhook_response",
                return_value=({"body": "<html>cancel</html>", "content_type": "text/html; charset=utf-8"}, 200),
            ) as builder_mock:
                response = api_app_mod.webhook_signal_cancel()
        self.assertEqual(response[0], "<html>cancel</html>")
        self.assertEqual(response[1], 200)
        self.assertEqual(response[2]["Content-Type"], "text/html; charset=utf-8")
        builder_mock.assert_called_once()
        self.assertNotIn("config_value", builder_mock.call_args.kwargs)
        self.assertIn("cancel_broker_order", builder_mock.call_args.kwargs)

    def test_feishu_callback_response_sets_update_card_token_header(self):
        class _JsonResponse:
            def __init__(self, payload):
                self.payload = payload
                self.headers = {}

        with mock.patch.object(api_app_mod, "jsonify", side_effect=lambda payload: _JsonResponse(payload)):
            response, status_code = api_app_mod._feishu_callback_response(
                {"ok": True},
                update_token="token-123",
                status_code=202,
            )

        self.assertEqual(status_code, 202)
        self.assertEqual(response.payload["ok"], True)
        self.assertEqual(response.headers["update_card_token"], "token-123")

    def test_webhook_tv_indicator_route_uses_native_indicator_upsert(self):
        sentinel = {"ok": True, "kind": "indicator"}
        with mock.patch.object(api_app_mod.request, "get_json", return_value={"type": "indicator", "symbol": "AAPL"}):
            with mock.patch.object(api_app_mod, "_upsert_tv_indicator", return_value=sentinel) as upsert_mock:
                payload = api_app_mod.webhook_tv()
        self.assertIs(payload, sentinel)
        upsert_mock.assert_called_once_with({"type": "indicator", "symbol": "AAPL"})

    def test_webhook_tv_indicator_skips_ingest_when_disabled(self):
        with mock.patch.object(
            api_app_mod.request,
            "get_json",
            return_value={"type": "indicator", "symbol": "AAPL", "environment": "live"},
        ):
            with mock.patch.object(api_app_mod, "_config_value", return_value="FALSE") as config_mock:
                with mock.patch.object(api_app_mod, "_upsert_tv_indicator") as upsert_mock:
                    payload = api_app_mod.webhook_tv()

        self.assertEqual(payload["ok"], True)
        self.assertEqual(payload["skipped"], True)
        self.assertEqual(payload["reason"], "tv_webhook_ingest_enabled=false")
        self.assertEqual(payload["config_value"], "FALSE")
        config_mock.assert_called_once_with("tv_webhook_ingest_enabled", "TRUE", "live")
        upsert_mock.assert_not_called()

    def test_webhook_tv_signal_route_uses_native_signal_upsert(self):
        sentinel = {"ok": True, "kind": "signal"}
        with mock.patch.object(api_app_mod.request, "get_json", return_value={"symbol": "AAPL"}):
            with mock.patch.object(api_app_mod, "_upsert_tv_signal", return_value=sentinel) as upsert_mock:
                payload = api_app_mod.webhook_tv()
        self.assertIs(payload, sentinel)
        upsert_mock.assert_called_once_with({"symbol": "AAPL"})

    def test_webhook_feishu_callback_url_verification_returns_challenge(self):
        with mock.patch.object(api_app_mod.request, "get_json", return_value={"type": "url_verification", "challenge": "abc"}):
            payload = api_app_mod.webhook_feishu_callback()
        self.assertEqual(payload["challenge"], "abc")

    def test_webhook_feishu_callback_dispatches_signal_actions(self):
        callback_result = {"toast": {"type": "success"}}
        with mock.patch.object(
            api_app_mod.request,
            "get_json",
            return_value={
                "event": {
                    "token": "card-token",
                    "action": {
                        "value": {
                            "action": "confirm",
                            "signal_id": "sig-1",
                            "environment": "live",
                        }
                    },
                }
            },
        ):
            with mock.patch.object(api_app_mod, "_dispatch_feishu_signal_callback", return_value=(callback_result, 200)) as dispatch_mock:
                payload = api_app_mod.webhook_feishu_callback()
        self.assertEqual(payload["toast"]["type"], "success")
        dispatch_mock.assert_called_once_with("confirm", "sig-1", "live")


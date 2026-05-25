import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SERVICE_SRC_ROOTS = [
    Path(__file__).resolve().parents[3] / "runtime" / "ibkr_api" / "src",
    Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src",
]
for src_root in SERVICE_SRC_ROOTS:
    if str(src_root) not in sys.path:
        sys.path.insert(0, str(src_root))

if "flask" not in sys.modules:
    flask_stub = types.ModuleType("flask")

    class _FakeFlask:
        def __init__(self, name):
            self.name = name

        def route(self, _path, methods=None):
            def decorator(func):
                return func
            return decorator

    flask_stub.Flask = _FakeFlask
    flask_stub.Response = object
    flask_stub.jsonify = lambda payload: payload
    flask_stub.request = SimpleNamespace(args={}, headers={}, method="GET", get_json=lambda silent=True: {})
    sys.modules["flask"] = flask_stub

from ibkr_api.control import broker_mode_switch as mode_switch


def _as_dict(value):
    return dict(value or {}) if isinstance(value, dict) else {}


def _normalize_environment(value, default="paper"):
    text = str(value or default or "paper").strip().lower()
    return text if text in {"paper", "live"} else str(default or "paper")


def _fetch_runtime_status(environment="paper"):
    return {"ok": True, "payload": {"broker_mode": environment, "environment": environment}}


def _state_payload(_state_key, _environment, date="global"):
    return {"data": {"status": "idle", "recovery_phase": "", "date": date}}


def _empty_snapshot():
    return {
        "ok": True,
        "environment": "paper",
        "positions": [],
        "live_open_orders": [],
        "counts": {
            "open_positions": 0,
            "open_orders": 0,
            "pb_active_order_groups": 0,
            "stale_pb_order_groups": 0,
            "pb_only_active_order_groups": 0,
            "pb_shadow_groups": 0,
        },
        "summary": {"account_code": "DU1234567"},
    }


def _write_ibc_config(path: Path, *, login="paper_user", password="paper_pw", mode="paper", port="4001") -> None:
    path.write_text(
        "\n".join(
            [
                f"IbLoginId={login}",
                f"IbPassword={password}",
                f"TradingMode={mode}",
                "StoreSettingsOnServer=no",
                f"OverrideTwsApiPort={port}",
                "ReadOnlyApi=no",
            ]
        )
        + "\n"
    )


class BrokerModeSwitchTest(unittest.TestCase):
    def test_update_mode_env_files_preserves_comments_and_creates_backup(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text(
                "# runtime env\n"
                "IBKR_BROKER_MODE=paper\n"
                "IBKR_ACCOUNT_ID=U1234567\n"
                "\n"
                "OTHER=value\n"
            )

            result = mode_switch.update_mode_env_files("live", env_files=[str(env_path)], timestamp="20260521093000")

            self.assertEqual(1, len(result))
            self.assertTrue(result[0]["updated"])
            self.assertTrue(Path(result[0]["backup_path"]).exists())
            self.assertEqual(
                [
                    "# runtime env",
                    "IBKR_BROKER_MODE=live",
                    "IBKR_ACCOUNT_ID=U1234567",
                    "",
                    "OTHER=value",
                    "IBKR_GATEWAY_MODE=live",
                ],
                env_path.read_text().splitlines(),
            )
            self.assertIn("IBKR_BROKER_MODE=paper", Path(result[0]["backup_path"]).read_text())

    def test_preview_blocks_open_positions_orders_and_pb_groups(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            ibc_path = Path(temp_dir) / "config.ini"
            _write_ibc_config(ibc_path)
            env_path.write_text(
                "IBKR_ACCOUNT_ID=U1234567\n"
                "IBKR_PAPER_ACCOUNT_ID=DU1234567\n"
                "IBKR_LIVE_USERNAME=live_user\n"
                "IBKR_PASSWORD=shared_pw\n"
                "IBGW_PORT=4001\n"
            )
            snapshot = _empty_snapshot()
            snapshot["counts"] = {
                "open_positions": 2,
                "open_orders": 3,
                "pb_active_order_groups": 1,
                "stale_pb_order_groups": 1,
                "pb_only_active_order_groups": 0,
                "pb_shadow_groups": 0,
            }

            with mock.patch.object(mode_switch, "build_account_snapshot_response", return_value=(snapshot, 200)):
                payload, status_code = mode_switch.build_broker_mode_switch_preview_response(
                    object(),
                    payload={"broker_mode": "paper", "target_broker_mode": "live"},
                    normalize_environment=_normalize_environment,
                    request_json_request=lambda *args, **kwargs: {},
                    runtime_base_url="http://runtime",
                    fetch_runtime_status=_fetch_runtime_status,
                    as_dict=_as_dict,
                    get_state_payload=_state_payload,
                    env_files=[str(env_path)],
                    ibc_config_path=str(ibc_path),
                )

            self.assertEqual(200, status_code)
            self.assertFalse(payload["allowed"])
            self.assertEqual({"open_positions", "open_orders", "pb_active_or_stale_groups"}, {item["code"] for item in payload["blockers"]})

    def test_preview_blocks_missing_target_account(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            ibc_path = Path(temp_dir) / "config.ini"
            _write_ibc_config(ibc_path)
            env_path.write_text("IBKR_PAPER_ACCOUNT_ID=DU1234567\nIBKR_LIVE_USERNAME=live_user\nIBKR_PASSWORD=shared_pw\n")

            with mock.patch.object(mode_switch, "build_account_snapshot_response", return_value=(_empty_snapshot(), 200)):
                payload, _status_code = mode_switch.build_broker_mode_switch_preview_response(
                    object(),
                    payload={"broker_mode": "paper", "target_broker_mode": "live"},
                    normalize_environment=_normalize_environment,
                    request_json_request=lambda *args, **kwargs: {},
                    runtime_base_url="http://runtime",
                    fetch_runtime_status=_fetch_runtime_status,
                    as_dict=_as_dict,
                    get_state_payload=_state_payload,
                    env_files=[str(env_path)],
                    ibc_config_path=str(ibc_path),
                )

            self.assertFalse(payload["allowed"])
            self.assertIn("target_account_missing", {item["code"] for item in payload["blockers"]})

    def test_preview_blocks_live_switch_without_explicit_live_username(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            ibc_path = Path(temp_dir) / "config.ini"
            _write_ibc_config(ibc_path)
            env_path.write_text(
                "IBKR_USERNAME=paper_user\n"
                "IBKR_ACCOUNT_ID=U1234567\n"
                "IBKR_PAPER_ACCOUNT_ID=DU1234567\n"
                "IBKR_PASSWORD=shared_pw\n"
            )

            with mock.patch.object(mode_switch, "build_account_snapshot_response", return_value=(_empty_snapshot(), 200)):
                payload, status_code = mode_switch.build_broker_mode_switch_preview_response(
                    object(),
                    payload={"broker_mode": "paper", "target_broker_mode": "live"},
                    normalize_environment=_normalize_environment,
                    request_json_request=lambda *args, **kwargs: {},
                    runtime_base_url="http://runtime",
                    fetch_runtime_status=_fetch_runtime_status,
                    as_dict=_as_dict,
                    get_state_payload=_state_payload,
                    env_files=[str(env_path)],
                    ibc_config_path=str(ibc_path),
                )

            self.assertEqual(200, status_code)
            self.assertFalse(payload["allowed"])
            self.assertIn("target_login_missing", {item["code"] for item in payload["blockers"]})
            self.assertFalse(payload["gateway_config"]["target_login_present"])

    def test_preview_blocks_missing_ibc_config(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            ibc_path = Path(temp_dir) / "missing.ini"
            env_path.write_text(
                "IBKR_ACCOUNT_ID=U1234567\n"
                "IBKR_PAPER_ACCOUNT_ID=DU1234567\n"
                "IBKR_LIVE_USERNAME=live_user\n"
                "IBKR_PASSWORD=shared_pw\n"
            )

            with mock.patch.object(mode_switch, "build_account_snapshot_response", return_value=(_empty_snapshot(), 200)):
                payload, _status_code = mode_switch.build_broker_mode_switch_preview_response(
                    object(),
                    payload={"broker_mode": "paper", "target_broker_mode": "live"},
                    normalize_environment=_normalize_environment,
                    request_json_request=lambda *args, **kwargs: {},
                    runtime_base_url="http://runtime",
                    fetch_runtime_status=_fetch_runtime_status,
                    as_dict=_as_dict,
                    get_state_payload=_state_payload,
                    env_files=[str(env_path)],
                    ibc_config_path=str(ibc_path),
                )

            self.assertFalse(payload["allowed"])
            self.assertIn("ibc_config_missing", {item["code"] for item in payload["blockers"]})

    def test_switch_rejects_invalid_confirm_text(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            ibc_path = Path(temp_dir) / "config.ini"
            _write_ibc_config(ibc_path)
            original_config = ibc_path.read_text()
            env_path.write_text(
                "IBKR_BROKER_MODE=paper\n"
                "IBKR_GATEWAY_MODE=paper\n"
                "IBKR_ACCOUNT_ID=U1234567\n"
                "IBKR_PAPER_ACCOUNT_ID=DU1234567\n"
                "IBKR_LIVE_USERNAME=live_user\n"
                "IBKR_PASSWORD=shared_pw\n"
            )

            with mock.patch.object(mode_switch, "build_account_snapshot_response", return_value=(_empty_snapshot(), 200)):
                payload, status_code = mode_switch.build_broker_mode_switch_response(
                    object(),
                    payload={"broker_mode": "paper", "target_broker_mode": "live", "confirm_text": "SWITCH PAPER"},
                    normalize_environment=_normalize_environment,
                    request_json_request=lambda *args, **kwargs: {},
                    runtime_base_url="http://runtime",
                    fetch_runtime_status=_fetch_runtime_status,
                    as_dict=_as_dict,
                    get_state_payload=_state_payload,
                    env_files=[str(env_path)],
                    ibc_config_path=str(ibc_path),
                    systemctl_action=lambda _service, _action: {"ok": True},
                    schedule_api_restart=lambda _reason: {"ok": True, "scheduled": True},
                )

            self.assertEqual(400, status_code)
            self.assertFalse(payload["ok"])
            self.assertEqual("invalid_confirm_text", payload["error"])
            self.assertEqual(original_config, ibc_path.read_text())

    def test_switch_updates_env_and_runs_restart_plan(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            ibc_path = Path(temp_dir) / "config.ini"
            _write_ibc_config(ibc_path)
            env_path.write_text(
                "IBKR_BROKER_MODE=paper\n"
                "IBKR_GATEWAY_MODE=paper\n"
                "IBKR_ACCOUNT_ID=U1234567\n"
                "IBKR_PAPER_ACCOUNT_ID=DU1234567\n"
                "IBKR_LIVE_USERNAME=live_user\n"
                "IBKR_LIVE_PASSWORD=live_pw\n"
                "IBKR_USERNAME=paper_user\n"
                "IBKR_PASSWORD=paper_pw\n"
                "IBGW_PORT=4001\n"
            )
            systemctl_calls = []

            def fake_systemctl(service, action):
                systemctl_calls.append((service, action))
                return {"ok": True, "service": service, "action": action}

            api_restart_reasons = []

            def fake_api_restart(reason):
                api_restart_reasons.append(reason)
                return {"ok": True, "scheduled": True, "reason": reason}

            with mock.patch.object(mode_switch, "build_account_snapshot_response", return_value=(_empty_snapshot(), 200)):
                payload, status_code = mode_switch.build_broker_mode_switch_response(
                    object(),
                    payload={"broker_mode": "paper", "target_broker_mode": "live", "confirm_text": "SWITCH LIVE"},
                    normalize_environment=_normalize_environment,
                    request_json_request=lambda *args, **kwargs: {},
                    runtime_base_url="http://runtime",
                    fetch_runtime_status=_fetch_runtime_status,
                    as_dict=_as_dict,
                    get_state_payload=_state_payload,
                    env_files=[str(env_path)],
                    ibc_config_path=str(ibc_path),
                    systemctl_action=fake_systemctl,
                    schedule_api_restart=fake_api_restart,
                )

            self.assertEqual(202, status_code)
            self.assertTrue(payload["ok"])
            self.assertIn("IBKR_BROKER_MODE=live", env_path.read_text())
            self.assertIn("IBKR_GATEWAY_MODE=live", env_path.read_text())
            self.assertIn("IBKR_USERNAME=live_user", env_path.read_text())
            self.assertIn("IBKR_PASSWORD=live_pw", env_path.read_text())
            self.assertIn("IBKR_PAPER_USERNAME=paper_user", env_path.read_text())
            self.assertIn("IBKR_PAPER_PASSWORD=paper_pw", env_path.read_text())
            config_text = ibc_path.read_text()
            self.assertIn("IbLoginId=live_user", config_text)
            self.assertIn("IbPassword=live_pw", config_text)
            self.assertIn("TradingMode=live", config_text)
            self.assertIn("OverrideTwsApiPort=4001", config_text)
            self.assertTrue(Path(payload["ibc_config_update"]["backup_path"]).exists())
            self.assertEqual(
                [
                    ("ibkr-runtime", "stop"),
                    ("ibkr-gateway", "restart"),
                    ("ibkr-runtime", "restart"),
                    ("ibkr-compute", "restart"),
                    ("ibkr-scheduler", "restart"),
                ],
                systemctl_calls,
            )
            self.assertEqual(["broker_mode_switch_live"], api_restart_reasons)
            self.assertEqual("ibkr-api", payload["restart_results"][-1]["service"])


if __name__ == "__main__":
    unittest.main()

import os
import sys
import unittest
from pathlib import Path


SERVICE_SRC_ROOTS = [
    Path(__file__).resolve().parents[3] / "runtime" / "ibkr_api" / "src",
    Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src",
]
for src_root in SERVICE_SRC_ROOTS:
    if str(src_root) not in sys.path:
        sys.path.insert(0, str(src_root))

os.environ.setdefault("IBKR_SCHEDULER_AUTOSTART", "false")

from ibkr_api.system.jobs.active_window_progress_status import (
    ACTIVE_WINDOW_PROGRESS_CARD_STATE_KEY,
    build_active_window_progress_status_response,
)


class ActiveWindowProgressStatusJobTest(unittest.TestCase):
    def _sample_progress(self, payload):
        return {
            "ok": True,
            "environment": payload.get("market_data_mode", "live"),
            "market_date": payload.get("market_date", "2026-05-28"),
            "computed_at_us": "2026-05-28 09:45:00",
            "computed_at_cn": "2026-05-28 21:45:00",
            "summary": {
                "total": 2,
                "active_count": 2,
                "with_live_bar_count": 2,
                "window_active_count": 1,
                "window_valid_count": 2,
                "confirmed_count": 0,
                "candidate_signal_count": 1,
                "current_candidate_signal_count": 1,
                "blocked_count": 0,
                "near_expiry_count": 0,
                "trace_error_count": 0,
                "window_status_counts": {"upper_active": 1, "no_window": 1},
                "timeline_data": {
                    "symbols_with_today_bars_count": 2,
                    "latest_bar_time_max_us": "2026-05-28 09:40:00",
                    "latest_bar_time_min_us": "2026-05-28 09:35:00",
                },
            },
            "returned_count": 2,
            "items": [
                {
                    "symbol": "AAPL",
                    "window_status": "upper_active",
                    "trace_stage": "candidate",
                    "component_progress": 0.75,
                    "bars_remaining": 6,
                    "collected_components": ["sd_upper_active", "trend"],
                    "missing_components": ["confirm"],
                    "candidate_signal": {"direction": "long"},
                    "candidate_signal_label": "long setup",
                    "latest_us_time": "2026-05-28 09:40:00",
                    "freshness_min": 1,
                    "target_score": 88,
                },
                {
                    "symbol": "MSFT",
                    "window_status": "no_window",
                    "trace_stage": "none",
                    "component_progress": 0.25,
                    "bars_remaining": 12,
                    "collected_components": ["bar"],
                    "missing_components": ["sd_window", "confirm"],
                    "latest_us_time": "2026-05-28 09:35:00",
                    "freshness_min": 6,
                    "target_score": 72,
                },
            ],
        }

    def _deps(self, *, states=None, sends=None, updates=None, events=None, update_result=None, send_result=None):
        states = states if states is not None else {}
        sends = sends if sends is not None else []
        updates = updates if updates is not None else []
        events = events if events is not None else []
        progress_payloads = []
        today_payloads = []

        def build_active_window_progress_response(*, payload):
            progress_payloads.append(dict(payload))
            return self._sample_progress(payload), 200

        def build_today_targets_response(*, payload):
            today_payloads.append(dict(payload))
            return {
                "ok": True,
                "market_date": payload.get("market_date", "2026-05-28"),
                "summary": {"total": 3, "active_count": 2, "candidate_count": 1},
                "items": [],
            }, 200

        def feishu_send_interactive(card, chat_id, environment):
            sends.append({"card": card, "chat_id": chat_id, "environment": environment})
            result = {"success": True, "message_id": f"msg-{len(sends)}"}
            result.update(send_result or {})
            return result

        def feishu_update_interactive(message_id, card, environment):
            updates.append({"message_id": message_id, "card": card, "environment": environment})
            result = {"success": True, "message_id": message_id}
            result.update(update_result or {})
            return result

        def get_state_payload(state_key, environment, date=None):
            return {"data": dict(states.get((state_key, environment, date), {}).get("data") or {})}

        def upsert_state(state_key, environment, data, date):
            states[(state_key, environment, date)] = {"data": dict(data)}
            return states[(state_key, environment, date)]

        def write_system_event_record(*args, **kwargs):
            events.append((args, kwargs))
            return {"id": f"event-{len(events)}"}

        deps = {
            "normalize_environment": lambda value, default: str(value or default).strip().lower() or default,
            "time_strings": lambda: {"us": "2026-05-28 09:45:00", "cn": "2026-05-28 21:45:00", "date": "2026-05-28"},
            "build_active_window_progress_response": build_active_window_progress_response,
            "build_today_targets_response": build_today_targets_response,
            "feishu_send_interactive": feishu_send_interactive,
            "feishu_update_interactive": feishu_update_interactive,
            "write_system_event_record": write_system_event_record,
            "get_state_payload": get_state_payload,
            "upsert_state": upsert_state,
            "config_value": lambda key, default, environment: default,
            "console_base_url": lambda: "https://quant.example.com",
            "startup_chat_id": lambda environment: f"startup-chat-{environment}",
        }
        return deps, progress_payloads, today_payloads

    def test_first_run_sends_to_startup_chat_and_persists_message_id(self):
        states = {}
        sends = []
        updates = []
        events = []
        deps, progress_payloads, today_payloads = self._deps(states=states, sends=sends, updates=updates, events=events)

        payload, status_code = build_active_window_progress_status_response(
            payload={"broker_mode": "paper", "market_data_mode": "live"},
            **deps,
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "sent")
        self.assertTrue(payload["sent"])
        self.assertFalse(payload["updated"])
        self.assertEqual(payload["message_id"], "msg-1")
        self.assertEqual(sends[0]["chat_id"], "startup-chat-paper")
        self.assertEqual(sends[0]["environment"], "paper")
        self.assertFalse(updates)
        self.assertEqual(progress_payloads[0]["status"], "active")
        self.assertEqual(progress_payloads[0]["limit"], 200)
        self.assertEqual(today_payloads[0]["target_status"], "active")
        state = states[(ACTIVE_WINDOW_PROGRESS_CARD_STATE_KEY, "paper", "2026-05-28")]["data"]
        self.assertEqual(state["message_id"], "msg-1")
        self.assertEqual(state["chat_id"], "startup-chat-paper")
        self.assertTrue(events)
        self.assertIn("IBKR 标的/信号窗口动态", sends[0]["card"]["header"]["title"]["content"])

    def test_second_run_updates_existing_message_id_without_sending_new_card(self):
        states = {
            (ACTIVE_WINDOW_PROGRESS_CARD_STATE_KEY, "paper", "2026-05-28"): {
                "data": {"message_id": "msg-existing", "chat_id": "startup-chat-paper"}
            }
        }
        sends = []
        updates = []
        events = []
        deps, _progress_payloads, _today_payloads = self._deps(states=states, sends=sends, updates=updates, events=events)

        payload, status_code = build_active_window_progress_status_response(
            payload={"broker_mode": "paper", "market_data_mode": "live"},
            **deps,
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "updated")
        self.assertTrue(payload["updated"])
        self.assertFalse(payload["sent"])
        self.assertEqual(payload["message_id"], "msg-existing")
        self.assertEqual(updates[0]["message_id"], "msg-existing")
        self.assertFalse(sends)
        self.assertFalse(events)
        state = states[(ACTIVE_WINDOW_PROGRESS_CARD_STATE_KEY, "paper", "2026-05-28")]["data"]
        self.assertEqual(state["message_id"], "msg-existing")

    def test_dry_run_builds_card_without_delivery_or_state_write(self):
        states = {}
        sends = []
        updates = []
        events = []
        deps, _progress_payloads, _today_payloads = self._deps(states=states, sends=sends, updates=updates, events=events)

        payload, status_code = build_active_window_progress_status_response(
            payload={"broker_mode": "paper", "market_data_mode": "live", "dry_run": True},
            **deps,
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "dry_run")
        self.assertIn("card", payload)
        self.assertFalse(sends)
        self.assertFalse(updates)
        self.assertFalse(events)
        self.assertFalse(states)

    def test_update_failure_falls_back_to_send_and_replaces_state_message_id(self):
        states = {
            (ACTIVE_WINDOW_PROGRESS_CARD_STATE_KEY, "paper", "2026-05-28"): {
                "data": {"message_id": "msg-old", "chat_id": "startup-chat-paper"}
            }
        }
        sends = []
        updates = []
        events = []
        deps, _progress_payloads, _today_payloads = self._deps(
            states=states,
            sends=sends,
            updates=updates,
            events=events,
            update_result={"success": False, "error": "message_not_found"},
            send_result={"message_id": "msg-new"},
        )

        payload, status_code = build_active_window_progress_status_response(
            payload={"broker_mode": "paper", "market_data_mode": "live"},
            **deps,
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "fallback_sent")
        self.assertTrue(payload["sent"])
        self.assertFalse(payload["updated"])
        self.assertEqual(payload["message_id"], "msg-new")
        self.assertEqual(updates[0]["message_id"], "msg-old")
        self.assertEqual(sends[0]["chat_id"], "startup-chat-paper")
        state = states[(ACTIVE_WINDOW_PROGRESS_CARD_STATE_KEY, "paper", "2026-05-28")]["data"]
        self.assertEqual(state["message_id"], "msg-new")
        self.assertTrue(events)
        self.assertEqual(events[0][0][1], "warning")


if __name__ == "__main__":
    unittest.main()

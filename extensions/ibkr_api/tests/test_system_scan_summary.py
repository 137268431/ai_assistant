import sys
import unittest
from pathlib import Path

SRC_ROOTS = [
    Path(__file__).resolve().parents[3] / "runtime" / "ibkr_api" / "src",
    Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src",
]
for src_root in SRC_ROOTS:
    if str(src_root) not in sys.path:
        sys.path.insert(0, str(src_root))

from ibkr_api.system.jobs.scan_summary import build_system_scan_summary_response


class SystemScanSummaryTest(unittest.TestCase):
    def test_scan_summary_delivers_to_signal_chat(self):
        sent = []
        states = {}
        events = []

        def build_today_targets_response(*, payload):
            return {
                "market_date": payload["market_date"],
                "computed_at_us": "2026-04-28 09:20:17",
                "summary": {
                    "total": 1,
                    "active_count": 1,
                    "candidate_count": 0,
                    "operable_count": 1,
                },
                "items": [
                    {
                        "symbol": "NVDA",
                        "status": "active",
                        "score": 31,
                        "scan_reason": "15m:ema_bullish",
                    }
                ],
            }, 200

        payload, status_code = build_system_scan_summary_response(
            payload={"environment": "live"},
            normalize_environment=lambda value, default: str(value or default).strip().lower(),
            time_strings=lambda: {"us": "2026-04-28 09:20:17", "date": "2026-04-28"},
            build_today_targets_response=build_today_targets_response,
            feishu_send_interactive=lambda card, chat_id, environment: sent.append(
                {"card": card, "chat_id": chat_id, "environment": environment}
            ) or {"success": True, "message_id": "om-scan"},
            write_system_event_record=lambda *args, **kwargs: events.append((args, kwargs)) or {},
            get_state_payload=lambda state_key, environment: {"data": states.get((state_key, environment), {})},
            upsert_state=lambda key, environment, data, date: states.update({(key, environment): data}) or data,
            config_value=lambda key, default, environment: "TRUE",
            console_base_url=lambda: "https://quant.lzw-glory.top",
            signal_chat_id=lambda environment: f"signal-chat-{environment}",
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["notified"])
        self.assertEqual(sent[0]["chat_id"], "signal-chat-live")
        self.assertEqual(sent[0]["environment"], "live")
        self.assertEqual(states[("system_notify_scan_summary", "live")]["message_id"], "om-scan")
        self.assertEqual(events[0][0][0], "scan_summary")


if __name__ == "__main__":
    unittest.main()

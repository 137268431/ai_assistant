from control_plane_split_stack_helpers import *


class ControlPlaneSplitStackDataGapGuardTest(unittest.TestCase):
    def test_data_gap_guard_ignores_vix_bar_gap(self):
        class FakeGapPB:
            def __init__(self):
                self.states = {}

            def get_state(self, state_key, environment, date="global"):
                return self.states.get((state_key, environment, date))

            def upsert_state(self, state_key, environment, data, date="global"):
                self.states[(state_key, environment, date)] = {"data": dict(data)}
                return self.states[(state_key, environment, date)]

            def get_records(self, collection, filter=None, sort=None, per_page=200, page=1):
                if collection == "watchlist":
                    return []
                if collection == "ibkr_targets":
                    return [{"symbol": "AAPL", "status": "active"}, {"symbol": "VIX", "status": "active"}]
                if collection == "ibkr_bars":
                    return [
                        {
                            "environment": "live",
                            "symbol": "AAPL",
                            "interval": "5m",
                            "bar_time_ms": 1713864300000,
                            "us_time": "2026-04-23 04:05:00",
                            "session_type": "regular",
                        },
                        {
                            "environment": "live",
                            "symbol": "VIX",
                            "interval": "5m",
                            "bar_time_ms": 1713863400000,
                            "us_time": "2026-04-23 03:50:00",
                            "session_type": "regular",
                        },
                    ]
                if collection == "ibkr_indicators":
                    return [
                        {
                            "environment": "live",
                            "symbol": "AAPL",
                            "interval": "5",
                            "bar_time_ms": 1713864300000,
                            "us_time": "2026-04-23 04:05:00",
                            "session_type": "regular",
                        }
                    ]
                return []

        events = []
        payload, status_code = build_data_gap_guard_response(
            FakeGapPB(),
            payload={"environment": "live"},
            normalize_environment=lambda value, default="live": str(value or default),
            time_strings=lambda: {"us": "2026-04-23 04:10:00", "cn": "2026-04-23 16:10:00", "date": "2026-04-23"},
            emit_system_event=lambda **kwargs: events.append(kwargs) or {"ok": True, "notified": True},
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["summary"]["market_activity_detected"])
        self.assertFalse(payload["summary"]["has_issue"])
        self.assertEqual(payload["summary"]["excluded_gap_symbols"], ["VIX"])
        self.assertEqual(payload["summary"]["bar_lag_symbols"], [])
        self.assertFalse(events)

    def test_data_gap_guard_ignores_non_target_bar_lag(self):
        class FakeGapPB:
            def __init__(self):
                self.states = {}

            def get_state(self, state_key, environment, date="global"):
                return self.states.get((state_key, environment, date))

            def upsert_state(self, state_key, environment, data, date="global"):
                self.states[(state_key, environment, date)] = {"data": dict(data)}
                return self.states[(state_key, environment, date)]

            def get_records(self, collection, filter=None, sort=None, per_page=200, page=1):
                if collection == "watchlist":
                    return [{"symbol": "AAPL"}, {"symbol": "MSFT"}, {"symbol": "GOOG"}]
                if collection == "ibkr_targets":
                    return [{"symbol": "AAPL", "status": "active"}, {"symbol": "MSFT", "status": "candidate"}]
                if collection == "ibkr_bars":
                    return [
                        {
                            "environment": "live",
                            "symbol": "AAPL",
                            "interval": "5m",
                            "bar_time_ms": 1713881100000,
                            "us_time": "2026-04-23 09:45:00",
                            "session_type": "regular",
                        },
                        {
                            "environment": "live",
                            "symbol": "MSFT",
                            "interval": "5m",
                            "bar_time_ms": 1713881100000,
                            "us_time": "2026-04-23 09:45:00",
                            "session_type": "regular",
                        },
                        {
                            "environment": "live",
                            "symbol": "GOOG",
                            "interval": "5m",
                            "bar_time_ms": 1713877500000,
                            "us_time": "2026-04-23 08:45:00",
                            "session_type": "regular",
                        },
                    ]
                if collection == "ibkr_indicators":
                    return [
                        {
                            "environment": "live",
                            "symbol": "AAPL",
                            "interval": "5",
                            "bar_time_ms": 1713881100000,
                            "us_time": "2026-04-23 09:45:00",
                            "session_type": "regular",
                        },
                        {
                            "environment": "live",
                            "symbol": "MSFT",
                            "interval": "5",
                            "bar_time_ms": 1713881100000,
                            "us_time": "2026-04-23 09:45:00",
                            "session_type": "regular",
                        },
                    ]
                return []

        events = []
        payload, status_code = build_data_gap_guard_response(
            FakeGapPB(),
            payload={"environment": "live"},
            normalize_environment=lambda value, default="live": str(value or default),
            time_strings=lambda: {"us": "2026-04-23 09:50:00", "cn": "2026-04-23 21:50:00", "date": "2026-04-23"},
            emit_system_event=lambda **kwargs: events.append(kwargs) or {"ok": True, "notified": True},
        )

        self.assertEqual(status_code, 200)
        self.assertEqual(payload["summary"]["target_count"], 2)
        self.assertEqual(payload["summary"]["alertable_symbol_count"], 2)
        self.assertEqual(payload["summary"]["today_bar_symbol_count"], 3)
        self.assertFalse(payload["summary"]["has_issue"])
        self.assertEqual(payload["summary"]["bar_lag_symbols"], [])
        self.assertFalse(events)

    def test_data_gap_guard_ignores_watchlist_indicator_lag_without_targets(self):
        class FakeGapPB:
            def __init__(self):
                self.states = {}

            def get_state(self, state_key, environment, date="global"):
                return self.states.get((state_key, environment, date))

            def upsert_state(self, state_key, environment, data, date="global"):
                self.states[(state_key, environment, date)] = {"data": dict(data)}
                return self.states[(state_key, environment, date)]

            def get_records(self, collection, filter=None, sort=None, per_page=200, page=1):
                if collection == "watchlist":
                    return [{"symbol": "AAPL"}, {"symbol": "LOW"}]
                if collection == "ibkr_targets":
                    return []
                if collection == "ibkr_bars":
                    return [
                        {
                            "environment": "live",
                            "symbol": "AAPL",
                            "interval": "5m",
                            "bar_time_ms": 1713881100000,
                            "us_time": "2026-04-23 09:45:00",
                            "session_type": "regular",
                        },
                        {
                            "environment": "live",
                            "symbol": "LOW",
                            "interval": "5m",
                            "bar_time_ms": 1713881100000,
                            "us_time": "2026-04-23 09:45:00",
                            "session_type": "regular",
                        },
                    ]
                if collection == "ibkr_indicators":
                    return []
                return []

        events = []
        payload, status_code = build_data_gap_guard_response(
            FakeGapPB(),
            payload={"environment": "live"},
            normalize_environment=lambda value, default="live": str(value or default),
            time_strings=lambda: {"us": "2026-04-23 09:50:00", "cn": "2026-04-23 21:50:00", "date": "2026-04-23"},
            emit_system_event=lambda **kwargs: events.append(kwargs) or {"ok": True, "notified": True},
        )

        self.assertEqual(status_code, 200)
        self.assertEqual(payload["summary"]["target_count"], 0)
        self.assertEqual(payload["summary"]["indicator_monitored_symbol_count"], 0)
        self.assertTrue(payload["summary"]["indicator_requires_targets"])
        self.assertFalse(payload["summary"]["has_issue"])
        self.assertEqual(payload["summary"]["indicator_lag_symbols"], [])
        self.assertFalse(events)

    def test_data_gap_guard_ignores_premarket_indicator_lag(self):
        class FakeGapPB:
            def __init__(self):
                self.states = {}

            def get_state(self, state_key, environment, date="global"):
                return self.states.get((state_key, environment, date))

            def upsert_state(self, state_key, environment, data, date="global"):
                self.states[(state_key, environment, date)] = {"data": dict(data)}
                return self.states[(state_key, environment, date)]

            def get_records(self, collection, filter=None, sort=None, per_page=200, page=1):
                if collection == "watchlist":
                    return []
                if collection == "ibkr_targets":
                    return [{"symbol": "AU", "status": "active"}]
                if collection == "ibkr_bars":
                    return [
                        {
                            "environment": "live",
                            "symbol": "AU",
                            "interval": "5m",
                            "bar_time_ms": 1713865500000,
                            "us_time": "2026-04-23 04:25:00",
                            "session_type": "premarket",
                        },
                    ]
                if collection == "ibkr_indicators":
                    return [
                        {
                            "environment": "live",
                            "symbol": "AU",
                            "interval": "5",
                            "bar_time_ms": 1713864600000,
                            "us_time": "2026-04-23 04:10:00",
                            "session_type": "premarket",
                        }
                    ]
                return []

        events = []
        payload, status_code = build_data_gap_guard_response(
            FakeGapPB(),
            payload={"environment": "live"},
            normalize_environment=lambda value, default="live": str(value or default),
            time_strings=lambda: {"us": "2026-04-23 04:30:00", "cn": "2026-04-23 16:30:00", "date": "2026-04-23"},
            emit_system_event=lambda **kwargs: events.append(kwargs) or {"ok": True, "notified": True},
        )

        self.assertEqual(status_code, 200)
        self.assertFalse(payload["summary"]["market_activity_detected"])
        self.assertFalse(payload["summary"]["has_issue"])
        self.assertEqual(payload["summary"]["ignored_non_regular_symbols"], ["AU"])
        self.assertEqual(payload["summary"]["indicator_lag_symbols"], [])
        self.assertFalse(events)

    def test_data_gap_guard_ignores_exact_indicator_lag_threshold(self):
        class FakeGapPB:
            def __init__(self):
                self.states = {}

            def get_state(self, state_key, environment, date="global"):
                return self.states.get((state_key, environment, date))

            def upsert_state(self, state_key, environment, data, date="global"):
                self.states[(state_key, environment, date)] = {"data": dict(data)}
                return self.states[(state_key, environment, date)]

            def get_records(self, collection, filter=None, sort=None, per_page=200, page=1):
                if collection == "watchlist":
                    return []
                if collection == "ibkr_targets":
                    return [{"symbol": "AU", "status": "active"}]
                if collection == "ibkr_bars":
                    return [
                        {
                            "environment": "live",
                            "symbol": "AU",
                            "interval": "5m",
                            "bar_time_ms": 1713881100000,
                            "us_time": "2026-04-23 09:45:00",
                            "session_type": "regular",
                        },
                    ]
                if collection == "ibkr_indicators":
                    return [
                        {
                            "environment": "live",
                            "symbol": "AU",
                            "interval": "5",
                            "bar_time_ms": 1713879300000,
                            "us_time": "2026-04-23 09:15:00",
                            "session_type": "regular",
                        }
                    ]
                return []

        events = []
        payload, status_code = build_data_gap_guard_response(
            FakeGapPB(),
            payload={"environment": "live"},
            normalize_environment=lambda value, default="live": str(value or default),
            time_strings=lambda: {"us": "2026-04-23 09:50:00", "cn": "2026-04-23 21:50:00", "date": "2026-04-23"},
            emit_system_event=lambda **kwargs: events.append(kwargs) or {"ok": True, "notified": True},
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["summary"]["market_activity_detected"])
        self.assertEqual(payload["summary"]["indicator_lag_alert_min"], 30)
        self.assertEqual(payload["summary"]["indicator_monitored_symbol_count"], 1)
        self.assertFalse(payload["summary"]["has_issue"])
        self.assertEqual(payload["summary"]["indicator_lag_symbols"], [])
        self.assertFalse(events)

    def test_data_gap_guard_uses_configured_indicator_lag_threshold(self):
        class FakeGapPB:
            def __init__(self):
                self.states = {}

            def get_state(self, state_key, environment, date="global"):
                return self.states.get((state_key, environment, date))

            def upsert_state(self, state_key, environment, data, date="global"):
                self.states[(state_key, environment, date)] = {"data": dict(data)}
                return self.states[(state_key, environment, date)]

            def get_records(self, collection, filter=None, sort=None, per_page=200, page=1):
                if collection == "watchlist":
                    return []
                if collection == "ibkr_targets":
                    return [{"symbol": "AU", "status": "active"}]
                if collection == "ibkr_bars":
                    return [
                        {
                            "environment": "live",
                            "symbol": "AU",
                            "interval": "5m",
                            "bar_time_ms": 1713881100000,
                            "us_time": "2026-04-23 09:45:00",
                            "session_type": "regular",
                        },
                    ]
                if collection == "ibkr_indicators":
                    return [
                        {
                            "environment": "live",
                            "symbol": "AU",
                            "interval": "5",
                            "bar_time_ms": 1713880200000,
                            "us_time": "2026-04-23 09:30:00",
                            "session_type": "regular",
                        }
                    ]
                return []

        events = []
        payload, status_code = build_data_gap_guard_response(
            FakeGapPB(),
            payload={"environment": "live"},
            normalize_environment=lambda value, default="live": str(value or default),
            time_strings=lambda: {"us": "2026-04-23 09:50:00", "cn": "2026-04-23 21:50:00", "date": "2026-04-23"},
            emit_system_event=lambda **kwargs: events.append(kwargs) or {"ok": True, "notified": True},
            config_value=lambda key, default, environment: "20" if key == "system_data_gap_indicator_lag_alert_min" else default,
        )

        self.assertEqual(status_code, 200)
        self.assertEqual(payload["summary"]["indicator_lag_alert_min"], 20)
        self.assertFalse(payload["summary"]["has_issue"])
        self.assertEqual(payload["summary"]["indicator_lag_symbols"], [])
        self.assertFalse(events)

    def test_data_gap_guard_alerts_regular_indicator_lag(self):
        class FakeGapPB:
            def __init__(self):
                self.states = {}

            def get_state(self, state_key, environment, date="global"):
                return self.states.get((state_key, environment, date))

            def upsert_state(self, state_key, environment, data, date="global"):
                self.states[(state_key, environment, date)] = {"data": dict(data)}
                return self.states[(state_key, environment, date)]

            def get_records(self, collection, filter=None, sort=None, per_page=200, page=1):
                if collection == "watchlist":
                    return []
                if collection == "ibkr_targets":
                    return [{"symbol": "AU", "status": "active"}]
                if collection == "ibkr_bars":
                    return [
                        {
                            "environment": "live",
                            "symbol": "AU",
                            "interval": "5m",
                            "bar_time_ms": 1713881100000,
                            "us_time": "2026-04-23 09:45:00",
                            "session_type": "regular",
                        },
                    ]
                if collection == "ibkr_indicators":
                    return [
                        {
                            "environment": "live",
                            "symbol": "AU",
                            "interval": "5",
                            "bar_time_ms": 1713879000000,
                            "us_time": "2026-04-23 09:10:00",
                            "session_type": "regular",
                        }
                    ]
                return []

        events = []
        payload, status_code = build_data_gap_guard_response(
            FakeGapPB(),
            payload={"environment": "live"},
            normalize_environment=lambda value, default="live": str(value or default),
            time_strings=lambda: {"us": "2026-04-23 09:50:00", "cn": "2026-04-23 21:50:00", "date": "2026-04-23"},
            emit_system_event=lambda **kwargs: events.append(kwargs) or {"ok": True, "notified": True},
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["summary"]["market_activity_detected"])
        self.assertTrue(payload["summary"]["has_issue"])
        self.assertEqual(payload["summary"]["indicator_lag_symbols"], ["AU"])
        self.assertEqual(payload["summary"]["indicator_missing_count"], 0)
        self.assertEqual(payload["summary"]["max_indicator_lag_min"], 35)
        self.assertTrue(events)
        detail = events[0]["detail"]
        self.assertEqual(detail["指标周期"], "5m")
        self.assertEqual(detail["指标滞后判定"], "缺当日5m指标 或 5m指标落后最新5m bar >30分钟")
        self.assertEqual(detail["指标最大滞后"], "35分钟")
        self.assertIn("AU(active): 35分钟 09:10->09:45", detail["指标滞后明细"])

    def test_data_gap_guard_alerts_candidate_indicator_missing_with_reason(self):
        class FakeGapPB:
            def __init__(self):
                self.states = {}

            def get_state(self, state_key, environment, date="global"):
                return self.states.get((state_key, environment, date))

            def upsert_state(self, state_key, environment, data, date="global"):
                self.states[(state_key, environment, date)] = {"data": dict(data)}
                return self.states[(state_key, environment, date)]

            def get_records(self, collection, filter=None, sort=None, per_page=200, page=1):
                if collection == "watchlist":
                    return []
                if collection == "ibkr_targets":
                    return [{"symbol": "AAPL", "status": "candidate"}]
                if collection == "ibkr_bars":
                    return [
                        {
                            "environment": "live",
                            "symbol": "AAPL",
                            "interval": "5m",
                            "bar_time_ms": 1713881100000,
                            "us_time": "2026-04-23 09:45:00",
                            "session_type": "regular",
                        },
                    ]
                if collection == "ibkr_indicators":
                    return []
                return []

        events = []
        payload, status_code = build_data_gap_guard_response(
            FakeGapPB(),
            payload={"environment": "live"},
            normalize_environment=lambda value, default="live": str(value or default),
            time_strings=lambda: {"us": "2026-04-23 09:50:00", "cn": "2026-04-23 21:50:00", "date": "2026-04-23"},
            emit_system_event=lambda **kwargs: events.append(kwargs) or {"ok": True, "notified": True},
        )

        self.assertEqual(status_code, 200)
        self.assertTrue(payload["summary"]["has_issue"])
        self.assertEqual(payload["summary"]["indicator_lag_symbols"], ["AAPL"])
        self.assertEqual(payload["summary"]["indicator_missing_symbols"], ["AAPL"])
        self.assertEqual(payload["summary"]["indicator_missing_count"], 1)
        self.assertEqual(payload["summary"]["max_indicator_lag_min"], 0)
        self.assertIn("candidate", payload["summary"]["indicator_lag_reason_hint"])
        self.assertTrue(events)
        detail = events[0]["detail"]
        self.assertEqual(detail["指标最大滞后"], "缺当日5m指标 1")
        self.assertIn("AAPL(candidate): 缺当日5m指标，bar 09:45", detail["指标滞后明细"])
        self.assertIn("candidate", detail["排查提示"])


if __name__ == "__main__":
    unittest.main()

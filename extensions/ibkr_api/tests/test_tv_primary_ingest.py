import copy
import re
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock


SERVICE_SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_api" / "src"
COMPUTE_SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
for src_root in (SERVICE_SRC_ROOT, COMPUTE_SRC_ROOT):
    if str(src_root) not in sys.path:
        sys.path.insert(0, str(src_root))


from ibkr_api.tradingview.tv_primary import TV_EVENT_COLLECTION, process_tv_primary_event
from ibkr_api.signals.ingest import build_signal_ingest_response
from ibkr_compute.market.timeframe_utils import ET


class _FakePB:
    def __init__(self):
        self.records = {
            TV_EVENT_COLLECTION: [],
            "ibkr_targets": [],
            "ibkr_signals": [],
            "ibkr_reverse_signals": [],
            "orders": [],
            "watchlist": [],
        }
        self._counters = {}

    def _next_id(self, collection):
        self._counters[collection] = self._counters.get(collection, 0) + 1
        return f"{collection}-{self._counters[collection]}"

    def create_record(self, collection, data):
        row = copy.deepcopy(data)
        row.setdefault("id", self._next_id(collection))
        self.records.setdefault(collection, []).append(row)
        return copy.deepcopy(row)

    def update_record(self, collection, record_id, patch):
        for row in self.records.setdefault(collection, []):
            if str(row.get("id")) == str(record_id):
                row.update(copy.deepcopy(patch))
                return copy.deepcopy(row)
        raise KeyError(record_id)

    def get_first_record(self, collection, filter=None, sort=None):
        rows = self.get_records(collection, filter=filter, sort=sort, per_page=1, page=1)
        return copy.deepcopy(rows[0]) if rows else None

    def get_records(self, collection, filter=None, sort=None, per_page=200, page=1):
        rows = [copy.deepcopy(row) for row in self.records.get(collection, []) if self._matches(row, filter or "")]
        rows = self._sort(rows, sort)
        start = max(0, int(page or 1) - 1) * int(per_page or 200)
        return rows[start : start + int(per_page or 200)]

    def get_all_records(self, collection, filter=None, sort=None, max_pages=20):
        return self.get_records(collection, filter=filter, sort=sort, per_page=200 * int(max_pages or 20), page=1)

    @staticmethod
    def _matches(row, filter_expr):
        text = str(filter_expr or "")
        for field, value in re.findall(r'([A-Za-z0-9_]+)\s*=\s*"([^"]*)"', text):
            if str(row.get(field) or "") != value:
                return False
        for field, value in re.findall(r'([A-Za-z0-9_]+)\s*=\s*(\d+)', text):
            if str(row.get(field) or "0") != value:
                return False
        return True

    @staticmethod
    def _sort(rows, sort):
        if not sort:
            return rows
        for key in reversed([part.strip() for part in str(sort).split(",") if part.strip()]):
            reverse = key.startswith("-")
            field = key[1:] if reverse else key
            rows.sort(key=lambda row: row.get(field) or "", reverse=reverse)
        return rows


class _LatencyFakePB(_FakePB):
    def create_record(self, collection, data):
        row = copy.deepcopy(data)
        row.setdefault("id", self._next_id(collection))
        if collection == TV_EVENT_COLLECTION:
            row.setdefault("created", "2026-05-29 16:46:07.000Z")
            row.setdefault("updated", "2026-05-29 16:46:07.000Z")
        self.records.setdefault(collection, []).append(row)
        return copy.deepcopy(row)


class _FailingEventPB(_FakePB):
    def create_record(self, collection, data):
        if collection == TV_EVENT_COLLECTION:
            raise RuntimeError("pb down")
        return super().create_record(collection, data)


def _escape(value):
    return str(value or "").replace("\\", "\\\\").replace('"', '\\"')


def _normalize_environment(value, default):
    return str(value or default).strip().lower() or default


def _config_value(key, default, environment):
    overrides = {
        "tv_entry_requires_active_target": "TRUE",
        "tv_entry_allow_self_activate": "FALSE",
        "tv_entry_window_enforce_enabled": "TRUE",
        "tv_max_active_targets": "10",
    }
    return overrides.get(key, default)


def _build_signal_ingest_response(pb, *, payload, **_kwargs):
    row = pb.create_record(
        "ibkr_signals",
        {
            "symbol": payload["symbol"],
            "environment": payload["market_data_mode"],
            "direction": payload["direction"],
            "signal_id": payload["signal_id"],
            "entry": payload["entry"],
            "stop_loss": payload["stop_loss"],
            "take_profit": payload["take_profit"],
            "shares": payload["shares"],
            "status": "pending",
            "extra": payload["extra"],
        },
    )
    return {
        "ok": True,
        "target": "ibkr_signals",
        "id": row["id"],
        "signal_id": row["signal_id"],
        "action": "created",
        "status": "pending",
    }, 200


def _process(pb, payload, *, config_value=_config_value, runtime_wakeup=None, async_route=False, route_executor=None):
    return process_tv_primary_event(
        pb,
        payload,
        normalize_environment=_normalize_environment,
        escape_filter_string=_escape,
        build_signal_ingest_response=_build_signal_ingest_response,
        config_value=config_value,
        runtime_wakeup=runtime_wakeup,
        async_route=async_route,
        route_executor=route_executor,
    )


def _process_with_real_signal_ingest(pb, payload):
    return process_tv_primary_event(
        pb,
        payload,
        normalize_environment=_normalize_environment,
        escape_filter_string=_escape,
        build_signal_ingest_response=build_signal_ingest_response,
        config_value=lambda key, default, environment: (
            "FALSE" if key == "signal_manual_confirm_enabled" else _config_value(key, default, environment)
        ),
    )


def _et_ms(text):
    return int(datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=ET).timestamp() * 1000)


def _create_origin_signal(pb, signal_id, *, environment="live", broker_mode="paper", status="submitted"):
    return pb.create_record(
        "ibkr_signals",
        {
            "signal_id": signal_id,
            "environment": environment,
            "symbol": "AAPL",
            "direction": "long",
            "status": status,
            "extra": {"execution_by_mode": {broker_mode: {"status": status}}},
        },
    )


def _mtf_payload(status="warn", score=72.5, block_reason="none"):
    return {
        "timeframe_stack": "entry=2;confirm=5,15,60;mode=shadow_soft",
        "entry_tf": "2",
        "confirm_tfs": "5,15,60",
        "mtf_status": status,
        "mtf_score": score,
        "mtf_block_reason": block_reason,
        "mtf": {
            "enabled": True,
            "mode": "shadow_soft",
            "entry_tf": "2",
            "confirm_tfs": "5,15,60",
            "status": status,
            "score": score,
            "block_reason": block_reason,
            "states": {
                "tf1": {"tf": "5", "status": "pass"},
                "tf2": {"tf": "15", "status": "warn"},
                "tf3": {"tf": "60", "status": "pass"},
            },
        },
    }


class TvPrimaryIngestTests(unittest.TestCase):
    def test_latency_trace_records_pine_api_pb_and_route_segments(self):
        pb = _LatencyFakePB()
        payload = {
            "source": "tv",
            "event_type": "pre_alert",
            "event_id": "tv-latency-1",
            "symbol": "RIO",
            "direction_bias": "long",
            "activity_score": 88,
            "quality_score": 82,
            "market_date": "2026-05-29",
            "environment": "live",
            "bar_time_ms": 1780073040000,
            "bar_close_ms": 1780073160000,
            "pine_eval_ms": 1780073161000,
            "interval": "2",
        }

        with mock.patch("ibkr_api.tradingview.tv_primary._epoch_ms", return_value=1780073167025):
            result, status_code = process_tv_primary_event(
                pb,
                payload,
                api_received_at_ms=1780073166000,
                normalize_environment=_normalize_environment,
                escape_filter_string=_escape,
                build_signal_ingest_response=_build_signal_ingest_response,
                config_value=_config_value,
            )
        self.assertEqual(status_code, 200)
        self.assertTrue(result["ok"])
        trace = pb.records[TV_EVENT_COLLECTION][0]["extra"]["latency_trace"]
        self.assertEqual(trace["bar_open_ms"], 1780073040000)
        self.assertEqual(trace["bar_close_ms"], 1780073160000)
        self.assertEqual(trace["pine_eval_ms"], 1780073161000)
        self.assertEqual(trace["api_received_at_ms"], 1780073166000)
        self.assertEqual(trace["bar_close_to_pine_eval_ms"], 1000)
        self.assertEqual(trace["pine_eval_to_api_received_ms"], 5000)
        self.assertEqual(trace["api_received_to_pb_created_ms"], 1000)
        self.assertEqual(trace["pb_created_to_route_finished_ms"], 25)

    def test_tv_event_persist_failure_returns_retryable_503(self):
        payload = {
            "source": "tv",
            "event_type": "pre_alert",
            "event_id": "tv-persist-fail",
            "symbol": "RIO",
            "direction_bias": "long",
            "market_date": "2026-05-29",
            "environment": "live",
        }

        result, status_code = _process(_FailingEventPB(), payload)

        self.assertEqual(status_code, 503)
        self.assertTrue(result["retryable"])
        self.assertEqual(result["error"], "tv_event_persist_failed")

    def test_pre_alert_upserts_target_and_dedupes_event(self):
        pb = _FakePB()
        payload = {
            "source": "tv",
            "event_type": "pre_alert",
            "event_id": "tv-pre-1",
            "symbol": "aapl",
            "direction_bias": "long",
            "activity_score": 88,
            "quality_score": 82,
            "market_date": "2026-05-29",
            "environment": "live",
            "pre_alert_stage": "window_activation",
            "activation_window": "upper",
            "interval": "2",
            "us_time": "2026-05-29 09:36:00",
            "bar_time_ms": _et_ms("2026-05-29 09:36:00"),
            **_mtf_payload(),
        }

        response, status = _process(pb, payload)

        self.assertEqual(status, 200)
        self.assertTrue(response["ok"])
        self.assertEqual(response["target"], "ibkr_targets")
        self.assertEqual(pb.records["ibkr_targets"][0]["status"], "candidate")
        self.assertEqual(pb.records["ibkr_targets"][0]["direction_bias"], "long")
        target_extra = pb.records["ibkr_targets"][0]["extra"]
        self.assertEqual(target_extra["activation_source"], "tradingview")
        self.assertEqual(target_extra["first_tv_event_id"], "tv-pre-1")
        self.assertEqual(target_extra["last_tv_event_id"], "tv-pre-1")
        self.assertEqual(target_extra["pre_alert_stage"], "window_activation")
        self.assertEqual(target_extra["activation_window"], "upper")
        self.assertEqual(target_extra["interval"], "2")
        self.assertEqual(target_extra["entry_tf"], "2")
        self.assertEqual(target_extra["confirm_tfs"], "5,15,60")
        self.assertEqual(target_extra["mtf_status"], "warn")
        self.assertEqual(target_extra["mtf_last_status"], "warn")
        self.assertEqual(target_extra["mtf"]["status"], "warn")
        event_extra = pb.records[TV_EVENT_COLLECTION][0]["extra"]
        self.assertEqual(event_extra["activation_window"], "upper")
        self.assertEqual(event_extra["interval"], "2")
        self.assertEqual(event_extra["mtf_status"], "warn")
        self.assertEqual(event_extra["mtf"]["mode"], "shadow_soft")
        self.assertEqual(pb.records[TV_EVENT_COLLECTION][0]["status"], "routed")

        duplicate, duplicate_status = _process(pb, payload)
        self.assertEqual(duplicate_status, 200)
        self.assertTrue(duplicate["skipped"])
        self.assertEqual(duplicate["status"], "duplicate")
        self.assertEqual(duplicate["reason"], "duplicate_tv_event")
        self.assertEqual(duplicate["route_status"], "routed")
        self.assertEqual(len(pb.records[TV_EVENT_COLLECTION]), 1)

    def test_pre_alert_creates_missing_trade_watchlist_for_current_market_date(self):
        pb = _FakePB()
        payload = {
            "source": "tv",
            "event_type": "pre_alert",
            "event_id": "tv-pre-watchlist",
            "symbol": "amd",
            "direction_bias": "long",
            "activity_score": 88,
            "quality_score": 82,
            "market_date": "2026-05-29",
            "environment": "live",
            "us_time": "2026-05-29 09:36:00",
            "bar_time_ms": _et_ms("2026-05-29 09:36:00"),
            **_mtf_payload(),
        }

        with mock.patch("ibkr_api.tradingview.tv_primary._current_et_date", return_value="2026-05-29"):
            response, status = _process(pb, payload)

        self.assertEqual(status, 200)
        self.assertTrue(response["ok"])
        self.assertEqual("created", response["watchlist_sync"]["action"])
        self.assertEqual(1, len(pb.records["watchlist"]))
        self.assertEqual("AMD", pb.records["watchlist"][0]["symbol"])
        self.assertEqual("trade", pb.records["watchlist"][0]["symbol_role"])
        self.assertFalse(pb.records["watchlist"][0]["manual_member"])

    def test_pre_alert_without_direction_leaves_target_direction_bias_empty(self):
        pb = _FakePB()
        payload = {
            "source": "tv",
            "event_type": "pre_alert",
            "event_id": "tv-pre-no-direction",
            "symbol": "TSLA",
            "activity_score": 84,
            "quality_score": 80,
            "market_date": "2026-05-29",
            "environment": "live",
            "us_time": "2026-05-29 09:37:00",
            "bar_time_ms": _et_ms("2026-05-29 09:37:00"),
        }

        response, status = _process(pb, payload)

        self.assertEqual(status, 200)
        self.assertTrue(response["ok"])
        target = pb.records["ibkr_targets"][0]
        self.assertEqual(target["direction_bias"], "")
        self.assertEqual(target["extra"]["direction_bias"], "")
        self.assertEqual(pb.records[TV_EVENT_COLLECTION][0]["direction"], "")

    def test_entry_accepts_same_day_tv_target_without_direction_or_active_rank(self):
        pb = _FakePB()

        def config_value(key, default, environment):
            if key == "tv_max_active_targets":
                return "0"
            return _config_value(key, default, environment)

        pre_alert, pre_alert_status = _process(
            pb,
            {
                "source": "tv",
                "event_type": "pre_alert",
                "event_id": "tv-pre-no-dir-candidate",
                "symbol": "NVDA",
                "activity_score": 88,
                "quality_score": 87,
                "market_date": "2026-05-29",
                "environment": "live",
                "us_time": "2026-05-29 09:36:00",
                "bar_time_ms": _et_ms("2026-05-29 09:36:00"),
            },
            config_value=config_value,
        )
        self.assertEqual(pre_alert_status, 200)
        self.assertTrue(pre_alert["ok"])
        self.assertEqual(pb.records["ibkr_targets"][0]["status"], "candidate")
        self.assertEqual(pb.records["ibkr_targets"][0]["direction_bias"], "")

        response, status = _process(
            pb,
            {
                "source": "tv",
                "event_type": "entry",
                "event_id": "tv-entry-no-dir-target",
                "signal_id": "tv-entry-no-dir-target",
                "symbol": "NVDA",
                "direction": "long",
                "entry_price": 122.50,
                "quantity": 8,
                "stop_loss": 120.40,
                "take_profit": 127.90,
                "market_date": "2026-05-29",
                "environment": "paper",
                "us_time": "2026-05-29 09:45:00",
                "activity_score": 88,
                "quality_score": 90,
                "atr": 1.25,
                "atr_pct": 0.75,
                "profit_space_entry_allowed": True,
                "profit_space_filter_reason": "pass",
                "expected_net_profit": 61.5,
                "expected_net_roi_pct": 1.24,
                "cost_pct_of_reward": 4.2,
                "target_distance_atr": 3.0,
                "min_net_roi_pct_for_entry": 1.0,
                **_mtf_payload(status="pass", score=100.0),
            },
            config_value=config_value,
        )

        self.assertEqual(status, 200)
        self.assertTrue(response["ok"])
        saved = pb.records["ibkr_signals"][0]
        self.assertEqual(saved["symbol"], "NVDA")
        self.assertEqual(saved["direction"], "long")
        self.assertEqual(saved["extra"]["admission_reason"], "same_day_tv_pre_alert_target")
        self.assertTrue(saved["extra"]["has_same_day_tv_target"])
        self.assertEqual(saved["extra"]["atr"], 1.25)
        self.assertEqual(saved["extra"]["atr_pct"], 0.75)
        self.assertTrue(saved["extra"]["profit_space_entry_allowed"])
        self.assertEqual(saved["extra"]["profit_space_filter_reason"], "pass")
        self.assertEqual(saved["extra"]["expected_net_profit"], 61.5)
        self.assertEqual(saved["extra"]["expected_net_roi_pct"], 1.24)
        self.assertEqual(saved["extra"]["cost_pct_of_reward"], 4.2)
        self.assertEqual(saved["extra"]["target_distance_atr"], 3.0)
        self.assertEqual(saved["extra"]["min_net_roi_pct_for_entry"], 1.0)
        self.assertEqual(pb.records[TV_EVENT_COLLECTION][-1]["status"], "routed")

    def test_entry_uses_trade_watchlist_as_authorized_universe_without_active_rank(self):
        pb = _FakePB()
        pb.create_record(
            "watchlist",
            {"symbol": "AMD", "environment": "live", "symbol_role": "trade"},
        )
        pb.create_record(
            "watchlist",
            {"symbol": "SPY", "environment": "live", "symbol_role": "market_monitor"},
        )

        def config_value(key, default, environment):
            values = {
                "tv_entry_requires_active_target": "FALSE",
                "tv_entry_requires_authorized_symbol": "TRUE",
                "tv_entry_window_enforce_enabled": "TRUE",
            }
            return values.get(key, _config_value(key, default, environment))

        response, status = _process(
            pb,
            {
                "source": "tv",
                "event_type": "entry",
                "event_id": "tv-entry-watchlist-authorized",
                "signal_id": "tv-entry-watchlist-authorized",
                "symbol": "AMD",
                "direction": "long",
                "entry_price": 168.25,
                "quantity": 29,
                "stop_loss": 166.80,
                "take_profit": 172.10,
                "market_date": "2026-05-29",
                "environment": "paper",
                "us_time": "2026-05-29 09:45:00",
                "activity_score": 91,
                **_mtf_payload(status="pass", score=100.0),
            },
            config_value=config_value,
        )

        self.assertEqual(status, 200)
        self.assertTrue(response["ok"])
        saved = pb.records["ibkr_signals"][0]
        self.assertEqual(saved["symbol"], "AMD")
        self.assertEqual(saved["extra"]["admission_reason"], "authorized_symbol")
        self.assertEqual(saved["extra"]["authorized_symbol_source"], "watchlist")

        rejected, rejected_status = _process(
            pb,
            {
                "source": "tv",
                "event_type": "entry",
                "event_id": "tv-entry-watchlist-rejected",
                "signal_id": "tv-entry-watchlist-rejected",
                "symbol": "SPY",
                "direction": "long",
                "entry_price": 500.25,
                "quantity": 10,
                "stop_loss": 498.80,
                "take_profit": 506.10,
                "market_date": "2026-05-29",
                "environment": "paper",
                "us_time": "2026-05-29 09:45:00",
                "activity_score": 91,
                **_mtf_payload(status="pass", score=100.0),
            },
            config_value=config_value,
        )

        self.assertEqual(rejected_status, 200)
        self.assertTrue(rejected["rejected"])
        self.assertEqual(rejected["reason"], "symbol_not_authorized_for_tv_entry")

    def test_entry_backfill_creates_missing_trade_watchlist_for_current_market_date(self):
        pb = _FakePB()

        def config_value(key, default, environment):
            values = {
                "tv_entry_requires_active_target": "FALSE",
                "tv_entry_requires_authorized_symbol": "FALSE",
                "tv_entry_window_enforce_enabled": "TRUE",
            }
            return values.get(key, _config_value(key, default, environment))

        with mock.patch("ibkr_api.tradingview.tv_primary._current_et_date", return_value="2026-05-29"):
            response, status = _process(
                pb,
                {
                    "source": "tv",
                    "event_type": "entry",
                    "event_id": "tv-entry-watchlist-sync",
                    "signal_id": "tv-entry-watchlist-sync",
                    "symbol": "AMD",
                    "direction": "long",
                    "entry_price": 122.50,
                    "quantity": 8,
                    "stop_loss": 120.40,
                    "take_profit": 127.90,
                    "market_date": "2026-05-29",
                    "environment": "paper",
                    "us_time": "2026-05-29 09:45:00",
                    "activity_score": 88,
                    "quality_score": 90,
                    **_mtf_payload(status="pass", score=100.0),
                },
                config_value=config_value,
            )

        self.assertEqual(status, 200)
        self.assertTrue(response["ok"])
        self.assertEqual("created", response["target_backfill"]["watchlist_sync"]["action"])
        self.assertEqual("active", pb.records["ibkr_targets"][0]["status"])
        self.assertEqual("AMD", pb.records["watchlist"][0]["symbol"])
        self.assertEqual("trade", pb.records["watchlist"][0]["symbol_role"])

    def test_async_route_persists_received_event_before_routing(self):
        pb = _FakePB()
        jobs = []
        wakeup_calls = []
        payload = {
            "source": "tv",
            "event_type": "pre_alert",
            "event_id": "tv-pre-async",
            "symbol": "TSLA",
            "activity_score": 84,
            "quality_score": 80,
            "market_date": "2026-05-29",
            "environment": "live",
            "us_time": "2026-05-29 09:37:00",
            "bar_time_ms": _et_ms("2026-05-29 09:37:00"),
        }

        response, status = process_tv_primary_event(
            pb,
            payload,
            normalize_environment=_normalize_environment,
            escape_filter_string=_escape,
            build_signal_ingest_response=_build_signal_ingest_response,
            config_value=_config_value,
            runtime_wakeup=lambda payload: wakeup_calls.append(dict(payload)) or {"ok": True, "woke": True},
            async_route=True,
            route_executor=jobs.append,
        )

        self.assertEqual(status, 202)
        self.assertTrue(response["queued"])
        self.assertEqual(pb.records[TV_EVENT_COLLECTION][0]["status"], "received")
        self.assertEqual(pb.records["ibkr_targets"], [])
        self.assertEqual(len(jobs), 1)

        jobs[0]()

        self.assertEqual(pb.records[TV_EVENT_COLLECTION][0]["status"], "routed")
        self.assertEqual(pb.records["ibkr_targets"][0]["symbol"], "TSLA")
        self.assertEqual(wakeup_calls, [])

    def test_async_entry_triggers_runtime_wakeup_after_background_route(self):
        pb = _FakePB()
        jobs = []
        wakeup_calls = []
        pb.create_record(
            "ibkr_targets",
            {
                "symbol": "AAPL",
                "date": "2026-05-29",
                "environment": "live",
                "direction_bias": "long",
                "score": 90,
                "status": "active",
                "extra": {"source": "tradingview", "activity_rank": 1},
            },
        )

        response, status = _process(
            pb,
            {
                "source": "tv",
                "event_type": "entry",
                "event_id": "tv-entry-async-wakeup",
                "signal_id": "tv-entry-async-wakeup",
                "symbol": "AAPL",
                "direction": "long",
                "entry_price": 188.25,
                "quantity": 12,
                "stop_loss": 185.80,
                "take_profit": 193.10,
                "market_date": "2026-05-29",
                "environment": "paper",
                "us_time": "2026-05-29 09:45:00",
                "activity_score": 91,
                **_mtf_payload(status="pass", score=100.0),
            },
            runtime_wakeup=lambda payload: wakeup_calls.append(dict(payload)) or {"ok": True, "woke": True},
            async_route=True,
            route_executor=jobs.append,
        )

        self.assertEqual(status, 202)
        self.assertTrue(response["queued"])
        self.assertTrue(response["runtime_wakeup"]["deferred"])
        self.assertEqual(wakeup_calls, [])

        jobs[0]()

        self.assertEqual(1, len(wakeup_calls))
        self.assertEqual("entry", wakeup_calls[0]["event_type"])
        self.assertEqual("ibkr_signals", wakeup_calls[0]["route_target"])
        self.assertEqual(pb.records["ibkr_signals"][0]["id"], wakeup_calls[0]["route_record_id"])
        event_extra = pb.records[TV_EVENT_COLLECTION][0]["extra"]
        self.assertTrue(event_extra["runtime_wakeup"]["ok"])

    def test_pre_alert_preserves_first_activation_metadata_on_later_updates(self):
        pb = _FakePB()
        first_bar_ms = _et_ms("2026-05-29 09:36:00")
        second_bar_ms = _et_ms("2026-05-29 09:41:00")

        first, first_status = _process(pb, {
            "source": "tv",
            "event_type": "pre_alert",
            "event_id": "tv-pre-first",
            "symbol": "MSFT",
            "direction_bias": "long",
            "activity_score": 80,
            "quality_score": 75,
            "market_date": "2026-05-29",
            "environment": "live",
            "us_time": "2026-05-29 09:36:00",
            "bar_time_ms": first_bar_ms,
            **_mtf_payload(status="warn", score=70.0),
        })
        second, second_status = _process(pb, {
            "source": "tv",
            "event_type": "pre_alert",
            "event_id": "tv-pre-second",
            "symbol": "MSFT",
            "direction_bias": "long",
            "activity_score": 92,
            "quality_score": 90,
            "market_date": "2026-05-29",
            "environment": "live",
            "us_time": "2026-05-29 09:41:00",
            "bar_time_ms": second_bar_ms,
            **_mtf_payload(status="pass", score=100.0),
        })

        self.assertEqual(first_status, 200)
        self.assertEqual(second_status, 200)
        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        self.assertEqual(len(pb.records["ibkr_targets"]), 1)
        extra = pb.records["ibkr_targets"][0]["extra"]
        self.assertEqual(extra["first_tv_event_id"], "tv-pre-first")
        self.assertEqual(extra["first_bar_time_ms"], first_bar_ms)
        self.assertEqual(extra["last_tv_event_id"], "tv-pre-second")
        self.assertEqual(extra["last_bar_time_ms"], second_bar_ms)
        self.assertEqual(extra["mtf_last_status"], "pass")
        self.assertEqual(extra["mtf_last_score"], 100.0)
        self.assertEqual(extra["activity_rank"], 1)
        self.assertEqual(pb.records["ibkr_targets"][0]["status"], "candidate")

    def test_pre_alert_does_not_demote_entry_activated_target(self):
        pb = _FakePB()
        pb.create_record(
            "ibkr_targets",
            {
                "symbol": "WPM",
                "date": "2026-05-29",
                "environment": "live",
                "direction_bias": "long",
                "score": 92,
                "status": "active",
                "extra": {
                    "source": "tradingview",
                    "event_type": "entry",
                    "entry_backfilled_target": True,
                    "entry_signal_id": "wpm-entry-1",
                    "strategy_policy": {"setup_type": "tradingview_entry_backfill", "allowed_sides": ["long"]},
                },
            },
        )

        response, status = _process(pb, {
            "source": "tv",
            "event_type": "pre_alert",
            "event_id": "wpm-pre-after-entry",
            "symbol": "WPM",
            "direction_bias": "long",
            "activity_score": 85,
            "quality_score": 80,
            "market_date": "2026-05-29",
            "environment": "live",
            "us_time": "2026-05-29 10:05:00",
            "bar_time_ms": _et_ms("2026-05-29 10:05:00"),
        })

        self.assertEqual(status, 200)
        self.assertTrue(response["ok"])
        target = pb.records["ibkr_targets"][0]
        self.assertEqual(target["status"], "active")
        self.assertTrue(target["extra"]["entry_backfilled_target"])
        self.assertEqual(target["extra"]["entry_signal_id"], "wpm-entry-1")
        self.assertEqual(target["extra"]["rank_reason"], "tv_entry_active")

    def test_entry_routes_to_ibkr_signals_with_tv_payload_aliases(self):
        pb = _FakePB()
        pb.create_record(
            "ibkr_targets",
            {
                "symbol": "AAPL",
                "date": "2026-05-29",
                "environment": "live",
                "direction_bias": "long",
                "score": 90,
                "status": "active",
                "extra": {"source": "tradingview", "activity_rank": 1},
            },
        )

        response, status = _process(
            pb,
            {
                "source": "tv",
                "event_type": "entry",
                "event_id": "tv-entry-1",
                "signal_id": "tv-entry-1",
                "symbol": "AAPL",
                "direction": "long",
                "entry_price": 188.25,
                "quantity": 12,
                "stop_loss": 185.80,
                "take_profit": 193.10,
                "market_date": "2026-05-29",
                "environment": "paper",
                "us_time": "2026-05-29 09:45:00",
                "activity_score": 91,
                **_mtf_payload(status="pass", score=100.0),
            },
        )

        self.assertEqual(status, 200)
        self.assertTrue(response["ok"])
        self.assertEqual(response["target"], "ibkr_signals")
        saved = pb.records["ibkr_signals"][0]
        self.assertEqual(saved["environment"], "live")
        self.assertEqual(saved["entry"], 188.25)
        self.assertEqual(saved["shares"], 12)
        self.assertEqual(saved["extra"]["source"], "tradingview")
        self.assertEqual(saved["extra"]["trade_group_id"], "tv-entry-1")
        self.assertEqual(saved["extra"]["bracket_group"], "tv-entry-1")
        self.assertEqual(saved["extra"]["entry_order_linkage_policy"], "tv_signal_id_trade_group")
        self.assertEqual(saved["extra"]["mtf_status"], "pass")
        self.assertEqual(saved["extra"]["mtf_score"], 100.0)
        self.assertEqual(saved["extra"]["entry_tf"], "2")
        self.assertEqual(saved["extra"]["confirm_tfs"], "5,15,60")
        self.assertEqual(saved["extra"]["mtf"]["status"], "pass")
        self.assertEqual(pb.records[TV_EVENT_COLLECTION][0]["broker_mode"], "paper")

    def test_entry_route_success_triggers_runtime_wakeup(self):
        pb = _FakePB()
        wakeup_calls = []
        pb.create_record(
            "ibkr_targets",
            {
                "symbol": "AAPL",
                "date": "2026-05-29",
                "environment": "live",
                "direction_bias": "long",
                "score": 90,
                "status": "active",
                "extra": {"source": "tradingview", "activity_rank": 1},
            },
        )

        response, status = _process(
            pb,
            {
                "source": "tv",
                "event_type": "entry",
                "event_id": "tv-entry-wakeup-1",
                "signal_id": "tv-entry-wakeup-1",
                "symbol": "AAPL",
                "direction": "long",
                "entry_price": 188.25,
                "quantity": 12,
                "stop_loss": 185.80,
                "take_profit": 193.10,
                "market_date": "2026-05-29",
                "environment": "paper",
                "us_time": "2026-05-29 09:45:00",
                "activity_score": 91,
                **_mtf_payload(status="pass", score=100.0),
            },
            runtime_wakeup=lambda payload: wakeup_calls.append(dict(payload)) or {"ok": True, "woke": True},
        )

        self.assertEqual(status, 200)
        self.assertTrue(response["ok"])
        self.assertEqual(1, len(wakeup_calls))
        self.assertEqual("tv-entry-wakeup-1", wakeup_calls[0]["tv_event_id"])
        self.assertEqual("entry", wakeup_calls[0]["event_type"])
        self.assertEqual("ibkr_signals", wakeup_calls[0]["route_target"])
        self.assertEqual(pb.records["ibkr_signals"][0]["id"], wakeup_calls[0]["route_record_id"])
        self.assertTrue(response["runtime_wakeup"]["ok"])
        self.assertTrue(pb.records[TV_EVENT_COLLECTION][0]["extra"]["runtime_wakeup"]["ok"])

    def test_runtime_wakeup_failure_does_not_fail_entry_route(self):
        pb = _FakePB()
        pb.create_record(
            "ibkr_targets",
            {
                "symbol": "AAPL",
                "date": "2026-05-29",
                "environment": "live",
                "direction_bias": "long",
                "score": 90,
                "status": "active",
                "extra": {"source": "tradingview", "activity_rank": 1},
            },
        )

        def failing_wakeup(_payload):
            raise RuntimeError("runtime offline")

        with mock.patch("ibkr_api.tradingview.tv_primary.logger.warning") as warning:
            response, status = _process(
                pb,
                {
                    "source": "tv",
                    "event_type": "entry",
                    "event_id": "tv-entry-wakeup-fails",
                    "signal_id": "tv-entry-wakeup-fails",
                    "symbol": "AAPL",
                    "direction": "long",
                    "entry_price": 188.25,
                    "quantity": 12,
                    "stop_loss": 185.80,
                    "take_profit": 193.10,
                    "market_date": "2026-05-29",
                    "environment": "paper",
                    "us_time": "2026-05-29 09:45:00",
                    "activity_score": 91,
                    **_mtf_payload(status="pass", score=100.0),
                },
                runtime_wakeup=failing_wakeup,
            )

        self.assertEqual(status, 200)
        self.assertTrue(response["ok"])
        self.assertEqual("ibkr_signals", response["target"])
        self.assertFalse(response["runtime_wakeup"]["ok"])
        self.assertEqual("runtime_wakeup_failed", response["runtime_wakeup"]["reason"])
        self.assertEqual("routed", pb.records[TV_EVENT_COLLECTION][0]["status"])
        warning.assert_called_once()

    def test_entry_persists_runner_safety_tp_metadata(self):
        pb = _FakePB()
        pb.create_record(
            "ibkr_targets",
            {
                "symbol": "AAPL",
                "date": "2026-05-29",
                "environment": "live",
                "direction_bias": "long",
                "score": 90,
                "status": "active",
                "extra": {"source": "tradingview", "activity_rank": 1},
            },
        )

        response, status = _process(
            pb,
            {
                "source": "tv",
                "event_type": "entry",
                "event_id": "tv-entry-runner-1",
                "signal_id": "tv-entry-runner-1",
                "symbol": "AAPL",
                "direction": "long",
                "entry_price": 188.25,
                "quantity": 12,
                "stop_loss": 185.80,
                "take_profit": 190.70,
                "safety_take_profit": 196.10,
                "runner_activation_price": 190.70,
                "runner_activation_r": 1.0,
                "runner_enabled": True,
                "runner_active": False,
                "target_role": "safety_tp",
                "market_date": "2026-05-29",
                "environment": "paper",
                "us_time": "2026-05-29 09:45:00",
                "activity_score": 91,
                **_mtf_payload(status="pass", score=100.0),
            },
        )

        self.assertEqual(status, 200)
        self.assertTrue(response["ok"])
        saved = pb.records["ibkr_signals"][0]
        self.assertEqual(saved["take_profit"], 196.10)
        self.assertEqual(saved["extra"]["take_profit"], 196.10)
        self.assertEqual(saved["extra"]["safety_take_profit"], 196.10)
        self.assertEqual(saved["extra"]["runner_activation_price"], 190.70)
        self.assertEqual(saved["extra"]["runner_activation_r"], 1.0)
        self.assertTrue(saved["extra"]["runner_enabled"])
        self.assertFalse(saved["extra"]["runner_active"])
        self.assertEqual(saved["extra"]["target_role"], "safety_tp")
        self.assertFalse(saved["extra"]["target_is_hard"])
        self.assertFalse(saved["extra"]["target_checkpoint_is_exit"])

    def test_entry_with_mtf_block_rejects_without_creating_signal(self):
        pb = _FakePB()

        response, status = _process(
            pb,
            {
                "source": "tv",
                "event_type": "entry",
                "event_id": "tv-entry-mtf-block",
                "signal_id": "tv-entry-mtf-block",
                "symbol": "AAPL",
                "direction": "long",
                "entry_price": 188.25,
                "quantity": 12,
                "stop_loss": 185.80,
                "take_profit": 193.10,
                "market_date": "2026-05-29",
                "environment": "paper",
                "us_time": "2026-05-29 09:45:00",
                "activity_score": 91,
                **_mtf_payload(status="block", score=0.0, block_reason="mtf_5m_15m_opposite"),
            },
        )

        self.assertEqual(status, 200)
        self.assertTrue(response["rejected"])
        self.assertEqual(response["reason"], "mtf_blocked")
        self.assertEqual(response["mtf_block_reason"], "mtf_5m_15m_opposite")
        self.assertEqual(len(pb.records["ibkr_signals"]), 0)
        self.assertEqual(pb.records[TV_EVENT_COLLECTION][0]["status"], "rejected")

    def test_entry_without_us_time_uses_bar_time_for_signal_record(self):
        pb = _FakePB()
        pb.create_record(
            "ibkr_targets",
            {
                "symbol": "AAPL",
                "date": "2026-05-29",
                "environment": "live",
                "direction_bias": "long",
                "score": 90,
                "status": "active",
                "extra": {"source": "tradingview", "activity_rank": 1},
            },
        )

        response, status = _process_with_real_signal_ingest(
            pb,
            {
                "source": "tv",
                "event_type": "entry",
                "event_id": "tv-entry-no-time",
                "signal_id": "tv-entry-no-time",
                "symbol": "AAPL",
                "direction": "long",
                "entry_price": 188.25,
                "quantity": 12,
                "stop_loss": 185.80,
                "take_profit": 193.10,
                "market_date": "2026-05-29",
                "environment": "paper",
                "bar_time_ms": _et_ms("2026-05-29 10:40:00"),
                "activity_score": 91,
                "quality_score": 100,
            },
        )

        self.assertEqual(status, 200)
        self.assertTrue(response["ok"])
        saved = pb.records["ibkr_signals"][0]
        self.assertEqual(saved["us_time"], "2026-05-29 10:40:00")
        self.assertEqual(saved["date"], "2026-05-29")

    def test_risk_update_routes_to_adjust_bracket_reverse_signal(self):
        pb = _FakePB()
        wakeup_calls = []
        pb.create_record(
            "orders",
            {
                "signal_id": "tv-entry-1",
                "environment": "paper",
                "role": "stop_loss",
                "status": "Submitted",
                "broker_order_id": "sl-100",
            },
        )
        pb.create_record(
            "orders",
            {
                "signal_id": "tv-entry-1",
                "environment": "paper",
                "role": "take_profit",
                "status": "Submitted",
                "broker_order_id": "tp-100",
            },
        )

        response, status = _process(
            pb,
            {
                "source": "tv",
                "event_type": "risk_update",
                "event_id": "tv-risk-1",
                "symbol": "AAPL",
                "position_side": "long",
                "signal_id": "tv-entry-1",
                "trade_group_id": "tv-entry-1",
                "new_stop_loss": 187.10,
                "new_take_profit": 194.40,
                "risk_update_reason": "breakeven_trail",
                "environment": "paper",
                "market_data_mode": "live",
                "bar_time_ms": 1770001200000,
            },
            runtime_wakeup=lambda payload: wakeup_calls.append(dict(payload)) or {"ok": True, "woke": True},
        )

        self.assertEqual(status, 200)
        self.assertTrue(response["ok"])
        self.assertEqual(1, len(wakeup_calls))
        self.assertEqual("risk_update", wakeup_calls[0]["event_type"])
        self.assertEqual("ibkr_reverse_signals", wakeup_calls[0]["route_target"])
        reverse = pb.records["ibkr_reverse_signals"][0]
        self.assertEqual(reverse["id"], wakeup_calls[0]["route_record_id"])
        self.assertEqual(reverse["source"], "tradingview")
        self.assertEqual(reverse["action_type"], "adjust_bracket")
        self.assertEqual(reverse["environment"], "paper")
        self.assertEqual(reverse["priority"], 9)
        self.assertEqual(reverse["extra"]["origin_signal_id"], "tv-entry-1")
        self.assertEqual(reverse["extra"]["trade_group_id"], "tv-entry-1")
        self.assertEqual(reverse["extra"]["sl_order_id"], "sl-100")
        self.assertEqual(reverse["extra"]["tp_order_id"], "tp-100")
        self.assertEqual(reverse["extra"]["new_sl"], 187.10)
        self.assertTrue(response["runtime_wakeup"]["ok"])
        self.assertTrue(pb.records[TV_EVENT_COLLECTION][0]["extra"]["runtime_wakeup"]["ok"])

    def test_risk_update_uses_origin_signal_alias_without_symbol_only_linkage(self):
        pb = _FakePB()

        response, status = _process(
            pb,
            {
                "source": "tv",
                "event_type": "risk_update",
                "event_id": "tv-risk-origin-alias",
                "symbol": "AAPL",
                "position_side": "long",
                "origin_signal_id": "tv-entry-alias-1",
                "trade_group_id": "tv-entry-alias-1",
                "new_stop_loss": 187.10,
                "risk_update_reason": "breakeven_trail",
                "environment": "paper",
                "market_data_mode": "live",
                "bar_time_ms": 1770001200000,
            },
        )

        self.assertEqual(status, 200)
        self.assertTrue(response["ok"])
        reverse = pb.records["ibkr_reverse_signals"][0]
        self.assertEqual(reverse["action_type"], "adjust_bracket")
        self.assertEqual(reverse["extra"]["origin_signal_id"], "tv-entry-alias-1")
        self.assertEqual(reverse["extra"]["trade_group_id"], "tv-entry-alias-1")
        self.assertNotIn("sl_order_id", reverse["extra"])
        self.assertEqual("expired", reverse["status"])
        self.assertEqual("real_order_preflight", reverse["extra"]["invalidated_by"])
        self.assertTrue(reverse["extra"]["gateway_request_blocked"])

    def test_risk_update_preserves_requested_sides_for_stop_only_runner_trail(self):
        pb = _FakePB()
        pb.create_record(
            "orders",
            {
                "signal_id": "tv-entry-1",
                "environment": "paper",
                "role": "stop_loss",
                "status": "Submitted",
                "broker_order_id": "sl-100",
            },
        )
        pb.create_record(
            "orders",
            {
                "signal_id": "tv-entry-1",
                "environment": "paper",
                "role": "take_profit",
                "status": "Submitted",
                "broker_order_id": "tp-100",
            },
        )

        response, status = _process(
            pb,
            {
                "source": "tv",
                "event_type": "risk_update",
                "event_id": "tv-risk-stop-only",
                "symbol": "AAPL",
                "position_side": "long",
                "signal_id": "tv-entry-1",
                "new_stop_loss": 187.10,
                "new_take_profit": 196.10,
                "requested_sides": ["stop_loss"],
                "risk_update_reason": "runner_trail",
                "runner_enabled": True,
                "runner_active": True,
                "runner_activation_price": 190.70,
                "runner_activation_r": 1.0,
                "safety_take_profit": 196.10,
                "environment": "paper",
                "market_data_mode": "live",
                "bar_time_ms": 1770001200000,
            },
        )

        self.assertEqual(status, 200)
        self.assertTrue(response["ok"])
        reverse = pb.records["ibkr_reverse_signals"][0]
        self.assertEqual(reverse["action_type"], "adjust_bracket")
        self.assertEqual(reverse["extra"]["requested_sides"], ["stop_loss"])
        self.assertEqual(reverse["extra"]["sl_order_id"], "sl-100")
        self.assertEqual(reverse["extra"]["tp_order_id"], "tp-100")
        self.assertEqual(reverse["extra"]["new_sl"], 187.10)
        self.assertNotIn("new_tp", reverse["extra"])
        self.assertTrue(reverse["extra"]["runner_enabled"])
        self.assertTrue(reverse["extra"]["runner_active"])
        self.assertEqual(reverse["extra"]["target_role"], "safety_tp")

    def test_risk_update_preserves_explicit_noop_runner_activation(self):
        pb = _FakePB()
        _create_origin_signal(pb, "tv-entry-1")

        response, status = _process(
            pb,
            {
                "source": "tv",
                "event_type": "risk_update",
                "event_id": "tv-risk-runner-noop",
                "symbol": "AAPL",
                "position_side": "long",
                "signal_id": "tv-entry-1",
                "requested_sides": [],
                "risk_update_reason": "runner_activation",
                "runner_enabled": True,
                "runner_active": True,
                "runner_activation_price": 190.70,
                "runner_activation_r": 1.0,
                "safety_take_profit": 196.10,
                "environment": "paper",
                "market_data_mode": "live",
                "bar_time_ms": 1770001200000,
            },
        )

        self.assertEqual(status, 200)
        self.assertTrue(response["ok"])
        reverse = pb.records["ibkr_reverse_signals"][0]
        self.assertEqual(reverse["action_type"], "adjust_bracket")
        self.assertEqual(reverse["extra"]["requested_sides"], [])
        self.assertNotIn("new_sl", reverse["extra"])
        self.assertNotIn("new_tp", reverse["extra"])
        self.assertTrue(reverse["extra"]["runner_enabled"])
        self.assertTrue(reverse["extra"]["runner_active"])

    def test_risk_update_coalesces_same_type_pending_update(self):
        pb = _FakePB()
        _create_origin_signal(pb, "tv-entry-coalesce-1")

        for event_id, seq, new_stop in (
            ("tv-risk-breakeven-1", 1, 187.10),
            ("tv-risk-breakeven-2", 2, 188.20),
        ):
            response, status = _process(
                pb,
                {
                    "source": "tv",
                    "event_type": "risk_update",
                    "event_id": event_id,
                    "symbol": "AAPL",
                    "position_side": "long",
                    "signal_id": "tv-entry-coalesce-1",
                    "trade_group_id": "tv-entry-coalesce-1",
                    "new_stop_loss": new_stop,
                    "risk_update_reason": "breakeven_trail",
                    "risk_update_seq": seq,
                    "environment": "paper",
                    "market_data_mode": "live",
                },
            )
            self.assertEqual(status, 200)
            self.assertTrue(response["ok"])

        self.assertEqual(len(pb.records["ibkr_reverse_signals"]), 1)
        reverse = pb.records["ibkr_reverse_signals"][0]
        self.assertEqual(reverse["extra"]["risk_update_type"], "breakeven_stop")
        self.assertEqual(reverse["extra"]["risk_update_seq"], 2)
        self.assertEqual(reverse["extra"]["new_sl"], 188.20)
        self.assertTrue(reverse["extra"]["superseded_by_latest_risk_update"])
        self.assertEqual(reverse["extra"]["superseded_updates"][0]["risk_update_seq"], 1)

    def test_risk_update_keeps_different_types_pending(self):
        pb = _FakePB()
        _create_origin_signal(pb, "tv-entry-types-1")

        for event_id, reason, seq in (
            ("tv-risk-breakeven-type", "breakeven_trail", 1),
            ("tv-risk-runner-type", "runner_trail", 2),
        ):
            response, status = _process(
                pb,
                {
                    "source": "tv",
                    "event_type": "risk_update",
                    "event_id": event_id,
                    "symbol": "AAPL",
                    "position_side": "long",
                    "signal_id": "tv-entry-types-1",
                    "trade_group_id": "tv-entry-types-1",
                    "new_stop_loss": 187.10 + seq,
                    "risk_update_reason": reason,
                    "risk_update_seq": seq,
                    "environment": "paper",
                    "market_data_mode": "live",
                },
            )
            self.assertEqual(status, 200)
            self.assertTrue(response["ok"])

        self.assertEqual(len(pb.records["ibkr_reverse_signals"]), 2)
        types = {row["extra"]["risk_update_type"] for row in pb.records["ibkr_reverse_signals"]}
        self.assertEqual(types, {"breakeven_stop", "runner_trail_stop"})

    def test_risk_update_stale_seq_does_not_overwrite_newer_same_type(self):
        pb = _FakePB()
        _create_origin_signal(pb, "tv-entry-stale-1")

        for event_id, seq, new_stop in (
            ("tv-risk-runner-newer", 5, 190.50),
            ("tv-risk-runner-stale", 4, 189.00),
        ):
            response, status = _process(
                pb,
                {
                    "source": "tv",
                    "event_type": "risk_update",
                    "event_id": event_id,
                    "symbol": "AAPL",
                    "position_side": "long",
                    "signal_id": "tv-entry-stale-1",
                    "trade_group_id": "tv-entry-stale-1",
                    "new_stop_loss": new_stop,
                    "risk_update_reason": "runner_trail_stop",
                    "risk_update_seq": seq,
                    "environment": "paper",
                    "market_data_mode": "live",
                },
            )
            self.assertEqual(status, 200)
            self.assertTrue(response["ok"])

        self.assertEqual(len(pb.records["ibkr_reverse_signals"]), 1)
        reverse = pb.records["ibkr_reverse_signals"][0]
        self.assertEqual(reverse["extra"]["risk_update_type"], "runner_trail_stop")
        self.assertEqual(reverse["extra"]["risk_update_seq"], 5)
        self.assertEqual(reverse["extra"]["new_sl"], 190.50)

    def test_exit_routes_to_close_reverse_signal(self):
        pb = _FakePB()
        _create_origin_signal(pb, "tv-entry-1", status="filled")
        response, status = _process(
            pb,
            {
                "source": "tv",
                "event_type": "exit",
                "event_id": "tv-exit-1",
                "symbol": "AAPL",
                "position_side": "long",
                "signal_id": "tv-entry-1",
                "exit_reason": "take_profit",
                "environment": "paper",
                "market_data_mode": "live",
            },
        )

        self.assertEqual(status, 200)
        self.assertTrue(response["ok"])
        reverse = pb.records["ibkr_reverse_signals"][0]
        self.assertEqual(reverse["action_type"], "close")
        self.assertEqual(reverse["priority"], 9)
        self.assertEqual(reverse["extra"]["reverse_kind"], "tv_exit")

    def test_exit_without_real_order_evidence_expires_at_ingest(self):
        pb = _FakePB()
        response, status = _process(
            pb,
            {
                "source": "tv",
                "event_type": "exit",
                "event_id": "tv-exit-no-real-order",
                "symbol": "AAPL",
                "position_side": "long",
                "signal_id": "tv-entry-missing",
                "exit_reason": "take_profit",
                "environment": "paper",
                "market_data_mode": "live",
            },
        )

        self.assertEqual(status, 200)
        self.assertTrue(response["ok"])
        self.assertEqual("expired", response["status"])
        reverse = pb.records["ibkr_reverse_signals"][0]
        self.assertEqual("expired", reverse["status"])
        self.assertIn("real_filled_order_required_for_close", reverse["reason"])
        self.assertEqual("real_order_preflight", reverse["extra"]["invalidated_by"])
        self.assertTrue(reverse["extra"]["gateway_request_blocked"])

    def test_exit_close_reverse_signals_do_not_coalesce(self):
        pb = _FakePB()
        _create_origin_signal(pb, "tv-entry-exit-1", status="filled")

        for event_id in ("tv-exit-no-coalesce-1", "tv-exit-no-coalesce-2"):
            response, status = _process(
                pb,
                {
                    "source": "tv",
                    "event_type": "exit",
                    "event_id": event_id,
                    "symbol": "AAPL",
                    "position_side": "long",
                    "signal_id": "tv-entry-exit-1",
                    "exit_reason": "take_profit",
                    "environment": "paper",
                    "market_data_mode": "live",
                },
            )
            self.assertEqual(status, 200)
            self.assertTrue(response["ok"])

        self.assertEqual(len(pb.records["ibkr_reverse_signals"]), 2)
        self.assertEqual([row["action_type"] for row in pb.records["ibkr_reverse_signals"]], ["close", "close"])

    def test_exit_preserves_exit_reason_and_runner_metadata(self):
        pb = _FakePB()
        _create_origin_signal(pb, "tv-entry-1", status="filled")
        response, status = _process(
            pb,
            {
                "source": "tv",
                "event_type": "exit",
                "event_id": "tv-exit-runner-1",
                "symbol": "AAPL",
                "position_side": "long",
                "signal_id": "tv-entry-1",
                "exit_reason": "runner_stop",
                "exit_fill_role": "stop_loss",
                "runner_enabled": True,
                "runner_active": True,
                "runner_activation_price": 190.70,
                "runner_activation_r": 1.0,
                "safety_take_profit": 196.10,
                "target_role": "safety_tp",
                "environment": "paper",
                "market_data_mode": "live",
            },
        )

        self.assertEqual(status, 200)
        self.assertTrue(response["ok"])
        reverse = pb.records["ibkr_reverse_signals"][0]
        self.assertEqual(reverse["action_type"], "close")
        self.assertEqual(reverse["reason"], "runner_stop")
        self.assertEqual(reverse["extra"]["exit_reason"], "runner_stop")
        self.assertEqual(reverse["extra"]["exit_fill_role"], "stop_loss")
        self.assertTrue(reverse["extra"]["runner_enabled"])
        self.assertTrue(reverse["extra"]["runner_active"])
        self.assertEqual(reverse["extra"]["runner_activation_price"], 190.70)
        self.assertEqual(reverse["extra"]["safety_take_profit"], 196.10)


if __name__ == "__main__":
    unittest.main()

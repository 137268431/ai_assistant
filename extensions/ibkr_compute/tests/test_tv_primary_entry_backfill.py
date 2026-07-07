import sys
import unittest
from pathlib import Path


SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
API_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_api" / "src"
for path in (SRC_ROOT, API_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from ibkr_api.tradingview.tv_primary import TvPrimaryError, _route_entry  # noqa: E402


def _escape(value):
    return str(value or "").replace("\\", "\\\\").replace('"', '\\"')


def _config_value(key, default, environment):
    del environment
    values = {
        "tv_primary_trade_universe_symbols": "WPM",
        "tv_entry_requires_authorized_symbol": "TRUE",
        "tv_entry_window_enforce_enabled": "TRUE",
    }
    return values.get(key, default)


def _entry_payload(symbol="WPM"):
    return {
        "symbol": symbol,
        "exchange": "NYSE",
        "date": "2026-06-01",
        "us_time": "2026-06-01 10:00:00",
        "cn_time": "2026-06-01 22:00:00",
        "bar_time_ms": 1_780_000_000_000,
        "direction": "long",
        "position_side": "long",
        "signal_id": f"{symbol}_entry_1",
        "setup": "mr_sdLower",
        "reason": "SD下轨回归多",
        "entry": 130.0,
        "stop_loss": 128.0,
        "take_profit": 134.0,
        "shares": 10,
        "activity_score": 92,
        "quality_score": 92,
        "script_tag": "Signal_Strategy_Core[Glory]",
    }


class DummyPocketBase:
    def __init__(self, targets=None, watchlist=None):
        self.targets = [dict(row) for row in (targets or [])]
        self.watchlist = [dict(row) for row in (watchlist or [])]
        self.next_id = len(self.targets) + 1

    def _match_target(self, filter_expr):
        for row in self.targets:
            if all(token in filter_expr for token in (
                f'symbol = "{row.get("symbol")}"',
                f'date = "{row.get("date")}"',
                f'environment = "{row.get("environment")}"',
            )):
                return row
        return None

    def get_first_record(self, collection, filter=""):
        if collection != "ibkr_targets":
            return None
        row = self._match_target(filter)
        return dict(row) if row else None

    def get_all_records(self, collection, filter="", sort="", max_pages=1):
        del filter, sort, max_pages
        if collection == "watchlist":
            return [dict(row) for row in self.watchlist]
        if collection == "ibkr_targets":
            return [dict(row) for row in self.targets]
        return []

    def create_record(self, collection, data):
        if collection != "ibkr_targets":
            raise AssertionError(f"unexpected create collection: {collection}")
        record = {**dict(data), "id": f"target-{self.next_id}"}
        self.next_id += 1
        self.targets.append(record)
        return dict(record)

    def update_record(self, collection, record_id, data):
        if collection != "ibkr_targets":
            raise AssertionError(f"unexpected update collection: {collection}")
        for index, row in enumerate(self.targets):
            if str(row.get("id")) == str(record_id):
                self.targets[index] = {**row, **dict(data), "id": row.get("id")}
                return dict(self.targets[index])
        raise AssertionError(f"target not found: {record_id}")


def _route_with_dummy_signal(pb, payload, config_value=_config_value):
    captured = {}

    def build_signal_ingest_response(_pb, *, payload, **kwargs):
        del _pb, kwargs
        captured["payload"] = payload
        return {"ok": True, "target": "ibkr_signals", "id": "signal-1", "signal_id": payload["signal_id"]}, 200

    result, status = _route_entry(
        pb,
        payload,
        event_id=payload["signal_id"],
        event_type="entry",
        environment="live",
        broker_mode="live",
        config_value=config_value,
        escape_filter=_escape,
        build_signal_ingest_response=build_signal_ingest_response,
        normalize_environment=lambda value, default: value or default,
        send_interactive=None,
        update_interactive=None,
        signal_chat_id_fn=None,
        console_base_url="",
    )
    return result, status, captured["payload"]


def _config_without_trade_universe(key, default, environment):
    del environment
    values = {
        "tv_primary_trade_universe_symbols": "",
        "tv_entry_requires_authorized_symbol": "TRUE",
        "tv_entry_window_enforce_enabled": "TRUE",
    }
    return values.get(key, default)


def _etf_rotation_payload(symbol="QQQ"):
    payload = _entry_payload(symbol)
    payload.update(
        {
            "exchange": "NASDAQ" if symbol == "QQQ" else "AMEX",
            "script_tag": "Signal_Strategy_ETF_Rotation_Core[Glory]",
            "strategy_name": "ETF Rotation Top3 Long Only",
            "strategy_group": "etf_rotation_long_only",
            "trade_model": "top3_etf_rotation_orh_confluence_long_only_v2",
            "chart_symbol": symbol,
            "chart_symbol_rank": 2,
            "chart_symbol_score": 82,
            "chart_symbol_in_top3": True,
            "diagnostic_type": "entry_gate",
            "debug_reason": "waiting_reconfirm",
            "gate_spy_ok": True,
            "gate_orh_confluence_ok": True,
            "gate_atr_space_ok": True,
            "candidate_birth_reason": "entry_window_orh_confluence",
            "candidate_birth_time_window": "morning_entry",
            "candidate_birth_spy_ok": True,
            "candidate_birth_support_type": "orh_vwap_confluence",
            "pullback_quality": "orh_vwap_confluence",
            "target_1r_vs_atr": 0.72,
            "rank_1_symbol": "SMH",
            "rank_2_symbol": symbol,
            "rank_3_symbol": "XLK",
        }
    )
    return payload


def _etf_sweep_reclaim_payload(symbol="QQQ"):
    payload = _entry_payload(symbol)
    payload.update(
        {
            "exchange": "NASDAQ" if symbol == "QQQ" else "AMEX",
            "script_tag": "ETF_Sweep_Reclaim_Strategy[Glory]",
            "strategy_name": "ETF DCT Reclaim Long Only",
            "strategy_group": "etf_rotation_long_only",
            "trade_model": "etf_dct_reclaim_mixed_day_trend_fragment_long_only_v2",
            "setup": "etf_dct_reclaim_alpha_long",
            "reason": "sweep_reclaim_breakout",
            "entry": 500.0,
            "stop_loss": 496.0,
            "take_profit": 507.6,
            "shares": 20,
            "activity_score": 86,
            "quality_score": 86,
            "extra": {
                "leader_symbol": symbol,
                "leader_gap": 0.22,
                "leader_rank": 1,
                "chart_symbol_rank": 1,
                "chart_symbol_score": 86,
                "rs_z": 1.42,
                "sweep_z": 0.73,
                "day_move_z": 1.15,
                "cost_zone_low": 497.8,
                "cost_zone_high": 498.2,
                "sweep_low": 496.65,
                "reclaim_high": 499.35,
                "reclaim_bars": 2,
                "reload_state": "none",
                "setup_state": "reclaim_confirmed",
                "alert_action": "ENTRY",
                "trend_score": 78,
                "chop_score": 25,
                "trend_regime": "trend_fragment",
                "etf_above_vwap_count": 6,
                "leader_switch_count": 1,
                "price_efficiency": 0.58,
                "pullback_z": 0.82,
                "mixed_day_state": "trend_fragment",
                "window_name": "morning_main",
                "risk_multiplier": 1.0,
            },
        }
    )
    return payload


class TvPrimaryEntryBackfillTests(unittest.TestCase):
    def test_authorized_entry_creates_active_target_when_pre_alert_was_missing(self):
        pb = DummyPocketBase()

        result, status, signal_payload = _route_with_dummy_signal(pb, _entry_payload())

        self.assertEqual(status, 200)
        self.assertTrue(result["ok"])
        self.assertEqual(1, len(pb.targets))
        target = pb.targets[0]
        self.assertEqual(target["status"], "active")
        self.assertEqual(target["direction_bias"], "long")
        self.assertEqual(target["extra"]["source"], "tradingview")
        self.assertTrue(target["extra"]["entry_backfilled_target"])
        self.assertEqual(target["extra"]["entry_backfill_reason"], "missing_pre_alert_or_candidate")
        self.assertEqual(target["extra"]["target_admission_reason"], "entry_signal_backfill")
        self.assertEqual(signal_payload["extra"]["target_id"], target["id"])
        self.assertTrue(signal_payload["extra"]["target_backfilled"])
        self.assertEqual(signal_payload["extra"]["target_backfill"]["action"], "created")

    def test_authorized_entry_upgrades_existing_candidate_target_to_active(self):
        pb = DummyPocketBase([
            {
                "id": "target-existing",
                "symbol": "WPM",
                "exchange": "NYSE",
                "date": "2026-06-01",
                "environment": "live",
                "direction_bias": "neutral",
                "score": 40,
                "status": "candidate",
                "extra": {"source": "tradingview", "event_type": "pre_alert"},
            }
        ])

        result, status, signal_payload = _route_with_dummy_signal(pb, _entry_payload())

        self.assertEqual(status, 200)
        self.assertTrue(result["ok"])
        self.assertEqual(1, len(pb.targets))
        target = pb.targets[0]
        self.assertEqual(target["id"], "target-existing")
        self.assertEqual(target["status"], "active")
        self.assertEqual(target["direction_bias"], "long")
        self.assertTrue(target["extra"]["entry_backfilled_target"])
        self.assertEqual(target["extra"]["entry_backfill_reason"], "candidate_entry_upgrade")
        self.assertEqual(target["extra"]["entry_signal_id"], "WPM_entry_1")
        self.assertEqual(signal_payload["extra"]["target_id"], "target-existing")
        self.assertTrue(signal_payload["extra"]["target_backfilled"])
        self.assertEqual(signal_payload["extra"]["target_backfill"]["action"], "updated")

    def test_authorized_entry_adds_activation_metadata_to_legacy_active_target(self):
        pb = DummyPocketBase([
            {
                "id": "target-active-prealert",
                "symbol": "WPM",
                "exchange": "NYSE",
                "date": "2026-06-01",
                "environment": "live",
                "direction_bias": "neutral",
                "score": 40,
                "status": "active",
                "extra": {"source": "tradingview", "event_type": "pre_alert"},
            }
        ])

        result, status, signal_payload = _route_with_dummy_signal(pb, _entry_payload())

        self.assertEqual(status, 200)
        self.assertTrue(result["ok"])
        target = pb.targets[0]
        self.assertEqual(target["status"], "active")
        self.assertEqual(target["direction_bias"], "long")
        self.assertTrue(target["extra"]["entry_backfilled_target"])
        self.assertEqual(target["extra"]["entry_backfill_reason"], "active_entry_upgrade")
        self.assertEqual(target["extra"]["entry_signal_id"], "WPM_entry_1")
        self.assertTrue(signal_payload["extra"]["target_backfilled"])
        self.assertEqual(signal_payload["extra"]["target_backfill"]["action"], "updated")

    def test_authorized_entry_does_not_reactivate_removed_target(self):
        pb = DummyPocketBase([
            {
                "id": "target-removed",
                "symbol": "WPM",
                "exchange": "NYSE",
                "date": "2026-06-01",
                "environment": "live",
                "direction_bias": "long",
                "score": 40,
                "status": "removed",
                "extra": {"source": "tradingview", "event_type": "pre_alert"},
            }
        ])

        result, status, signal_payload = _route_with_dummy_signal(pb, _entry_payload())

        self.assertEqual(status, 200)
        self.assertTrue(result["ok"])
        self.assertEqual("removed", pb.targets[0]["status"])
        self.assertFalse(signal_payload["extra"]["target_backfilled"])
        self.assertEqual(signal_payload["extra"]["target_backfill"]["action"], "skipped")
        self.assertEqual(signal_payload["extra"]["target_backfill"]["reason"], "target_status_not_backfilled")

    def test_etf_rotation_allows_qqq_even_when_watchlist_role_is_market_monitor(self):
        pb = DummyPocketBase(
            watchlist=[
                {"symbol": "AAPL", "environment": "live", "symbol_role": "trade"},
                {"symbol": "QQQ", "environment": "global", "symbol_role": "market_monitor"},
                {"symbol": "SPY", "environment": "global", "symbol_role": "market_monitor"},
            ]
        )

        result, status, signal_payload = _route_with_dummy_signal(
            pb,
            _etf_rotation_payload("QQQ"),
            config_value=_config_without_trade_universe,
        )

        self.assertEqual(status, 200)
        self.assertTrue(result["ok"])
        self.assertEqual("QQQ", signal_payload["symbol"])
        self.assertTrue(signal_payload["extra"]["authorized_symbol"])
        self.assertIn("strategy_group:etf_rotation", signal_payload["extra"]["authorized_symbol_source"])
        self.assertEqual("ETF Rotation Top3 Long Only", signal_payload["extra"]["strategy_name"])
        self.assertEqual("waiting_reconfirm", signal_payload["extra"]["debug_reason"])
        self.assertTrue(signal_payload["extra"]["gate_spy_ok"])
        self.assertTrue(signal_payload["extra"]["gate_orh_confluence_ok"])
        self.assertEqual("entry_window_orh_confluence", signal_payload["extra"]["candidate_birth_reason"])
        self.assertEqual("orh_vwap_confluence", signal_payload["extra"]["pullback_quality"])
        self.assertEqual(0.72, signal_payload["extra"]["target_1r_vs_atr"])

    def test_etf_sweep_reclaim_allows_qqq_and_preserves_alpha_diagnostics(self):
        pb = DummyPocketBase(
            watchlist=[
                {"symbol": "AAPL", "environment": "live", "symbol_role": "trade"},
                {"symbol": "QQQ", "environment": "global", "symbol_role": "market_monitor"},
                {"symbol": "SPY", "environment": "global", "symbol_role": "market_monitor"},
            ]
        )

        result, status, signal_payload = _route_with_dummy_signal(
            pb,
            _etf_sweep_reclaim_payload("QQQ"),
            config_value=_config_without_trade_universe,
        )

        self.assertEqual(status, 200)
        self.assertTrue(result["ok"])
        self.assertEqual("QQQ", signal_payload["symbol"])
        self.assertTrue(signal_payload["extra"]["authorized_symbol"])
        self.assertIn("strategy_group:etf_rotation", signal_payload["extra"]["authorized_symbol_source"])
        self.assertEqual("ETF DCT Reclaim Long Only", signal_payload["extra"]["strategy_name"])
        self.assertEqual("etf_rotation_long_only", signal_payload["extra"]["strategy_group"])
        self.assertEqual("etf_dct_reclaim_mixed_day_trend_fragment_long_only_v2", signal_payload["extra"]["trade_model"])
        self.assertEqual("QQQ", signal_payload["extra"]["leader_symbol"])
        self.assertEqual(1, signal_payload["extra"]["chart_symbol_rank"])
        self.assertEqual(1.42, signal_payload["extra"]["rs_z"])
        self.assertEqual(0.73, signal_payload["extra"]["sweep_z"])
        self.assertEqual(1.15, signal_payload["extra"]["day_move_z"])
        self.assertEqual(496.65, signal_payload["extra"]["sweep_low"])
        self.assertEqual(499.35, signal_payload["extra"]["reclaim_high"])
        self.assertEqual(2, signal_payload["extra"]["reclaim_bars"])
        self.assertEqual("ENTRY", signal_payload["extra"]["alert_action"])
        self.assertEqual(78, signal_payload["extra"]["trend_score"])
        self.assertEqual(25, signal_payload["extra"]["chop_score"])
        self.assertEqual("trend_fragment", signal_payload["extra"]["trend_regime"])
        self.assertEqual(0.82, signal_payload["extra"]["pullback_z"])

    def test_market_monitor_qqq_is_still_rejected_without_etf_rotation_context(self):
        pb = DummyPocketBase(
            watchlist=[
                {"symbol": "AAPL", "environment": "live", "symbol_role": "trade"},
                {"symbol": "QQQ", "environment": "global", "symbol_role": "market_monitor"},
            ]
        )

        with self.assertRaises(TvPrimaryError) as raised:
            _route_with_dummy_signal(
                pb,
                _entry_payload("QQQ"),
                config_value=_config_without_trade_universe,
            )

        self.assertEqual("symbol_not_authorized_for_tv_entry", raised.exception.reason)


if __name__ == "__main__":
    unittest.main()

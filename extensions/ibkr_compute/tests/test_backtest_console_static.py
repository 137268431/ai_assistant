import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
BACKTEST_UI_JS = REPO_ROOT / "runtime" / "ibkr_console" / "static" / "assets" / "js" / "pages" / "ibkr_backtests" / "ui.js"
BACKTEST_HTML = REPO_ROOT / "runtime" / "ibkr_console" / "static" / "ibkr_backtests.html"


class BacktestConsoleStaticTests(unittest.TestCase):
    def test_daily_scan_replay_payload_defaults_to_daily_selected_cache(self):
        ui_js = BACKTEST_UI_JS.read_text(encoding="utf-8")

        self.assertIn("const isDailyScanReplay = String(symbolSource || '').trim() === 'daily_scan_replay';", ui_js)
        self.assertIn("daily_selected_only: true", ui_js)
        self.assertIn("daily_selection_cache_enabled: true", ui_js)
        self.assertIn("daily_selection_cache_mode: 'use_or_build'", ui_js)

    def test_max_symbols_copy_marks_daily_scan_as_daily_limit(self):
        html = BACKTEST_HTML.read_text(encoding="utf-8")

        self.assertIn("daily_scan_replay 下表示每日最多入选数", html)

    def test_live_like_defaults_are_selected_in_backtest_form(self):
        html = BACKTEST_HTML.read_text(encoding="utf-8")

        self.assertIn('<option value="daily_scan_replay" selected>daily_scan_replay</option>', html)
        self.assertIn('<option value="daily_selected_fast" selected>Daily selected fast + live SD</option>', html)
        self.assertIn('<input id="positionLimitMax" type="number" min="0" max="100" step="1" value="0">', html)
        self.assertIn('<option value="current_snapshot" selected>current_snapshot</option>', html)
        self.assertIn('<option value="ibkr_us_equity_fixed_v1" selected>IBKR US equity fixed</option>', html)
        self.assertIn('<option value="bar_capped_bps_v1" selected>bar capped bps</option>', html)


if __name__ == "__main__":
    unittest.main()

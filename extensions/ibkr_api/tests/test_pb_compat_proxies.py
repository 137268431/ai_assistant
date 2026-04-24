import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
PB_HOOKS_DIR = REPO_ROOT / "runtime" / "pocketbase" / "pb_hooks"
EXPECTED_NOOP_SHELLS = {
    "feishu.pb.js",
    "ibkr_actions.pb.js",
    "ibkr_backtest.pb.js",
    "ibkr_reverse_signals.pb.js",
    "ibkr_scheduler.pb.js",
    "ibkr_signal_actions.pb.js",
    "ibkr_signal_scheduler.pb.js",
    "ibkr_system_monitor.pb.js",
    "order_manage.pb.js",
    "order_scheduler.pb.js",
    "webhook_tv.pb.js",
}


class PocketBaseCompatHooksTest(unittest.TestCase):
    def test_pb_hooks_only_keeps_noop_shells(self):
        actual_files = {
            path.relative_to(PB_HOOKS_DIR).as_posix()
            for path in PB_HOOKS_DIR.rglob("*")
            if path.is_file()
        }
        self.assertEqual(actual_files, EXPECTED_NOOP_SHELLS)
        self.assertFalse((PB_HOOKS_DIR / "lib").exists())
        self.assertFalse((PB_HOOKS_DIR / "modules").exists())

    def test_noop_shells_do_not_register_routes_or_crons(self):
        for relpath in sorted(EXPECTED_NOOP_SHELLS):
            text = (PB_HOOKS_DIR / relpath).read_text(encoding="utf-8")
            with self.subTest(relpath=relpath):
                self.assertIn('/// <reference path="./pb_data/types.d.ts" />', text)
                self.assertIn("No-op compatibility shell.", text)
                self.assertNotIn("routerAdd(", text)
                self.assertNotIn("cronAdd(", text)
                self.assertNotIn("require(", text)


if __name__ == "__main__":
    unittest.main()

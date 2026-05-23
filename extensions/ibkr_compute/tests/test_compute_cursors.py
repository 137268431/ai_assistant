import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.api.compute import cursors


class _FailingStatePB:
    def __init__(self):
        self.seed_called = False

    def get_state(self, *args, **kwargs):
        raise TimeoutError("state timeout")

    def get_all_records(self, *args, **kwargs):
        self.seed_called = True
        raise AssertionError("indicator seed should be skipped after state load failure")


class ComputeCursorTests(unittest.TestCase):
    def test_load_persisted_cursors_does_not_seed_after_state_timeout(self):
        pb = _FailingStatePB()
        app = SimpleNamespace(
            pb=pb,
            cfg=SimpleNamespace(
                get_bool_for_environment=lambda key, environment, default: default,
            ),
            COMPUTE_CURSOR_STATE_KEY="compute_cursors",
            COMPUTE_CURSOR_STATE_DATE="global",
            last_processed_ms={},
            last_interval_fetch_ms={},
            persistent_cursor_envs_loaded=set(),
        )

        with mock.patch.object(cursors, "_api_app", return_value=app), mock.patch.object(cursors.traceback, "print_exc"):
            applied = cursors.load_persisted_compute_cursors("live")

        self.assertEqual(applied, 0)
        self.assertFalse(pb.seed_called)
        self.assertIn("live", app.persistent_cursor_envs_loaded)


if __name__ == "__main__":
    unittest.main()

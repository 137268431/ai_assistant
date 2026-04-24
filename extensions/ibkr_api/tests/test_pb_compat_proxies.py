import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
PB_HOOKS_DIR = REPO_ROOT / "runtime" / "pocketbase" / "pb_hooks"


class PocketBaseCompatHooksTest(unittest.TestCase):
    def test_pb_hooks_dir_is_now_empty_placeholder(self):
        actual_files = {
            path.relative_to(PB_HOOKS_DIR).as_posix()
            for path in PB_HOOKS_DIR.rglob("*")
            if path.is_file()
        }
        self.assertEqual(actual_files, {".gitkeep"})
        self.assertFalse((PB_HOOKS_DIR / "lib").exists())
        self.assertFalse((PB_HOOKS_DIR / "modules").exists())


if __name__ == "__main__":
    unittest.main()

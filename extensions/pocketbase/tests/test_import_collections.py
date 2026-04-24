from __future__ import annotations

import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_ROOT = REPO_ROOT / "extensions" / "pocketbase" / "scripts" / "schema"
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from import_collections import normalize_collection  # noqa: E402


class NormalizeCollectionTest(unittest.TestCase):
    def test_null_rules_become_open_rules(self) -> None:
        collection = normalize_collection(
            {
                "name": "config",
                "type": "base",
                "fields": [],
                "listRule": None,
                "viewRule": None,
                "createRule": None,
                "updateRule": None,
                "deleteRule": None,
            }
        )

        self.assertEqual(collection["listRule"], "")
        self.assertEqual(collection["viewRule"], "")
        self.assertEqual(collection["createRule"], "")
        self.assertEqual(collection["updateRule"], "")
        self.assertEqual(collection["deleteRule"], "")


if __name__ == "__main__":
    unittest.main()

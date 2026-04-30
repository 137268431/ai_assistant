from __future__ import annotations

import json
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

    def test_system_events_accepts_ibkr_api_source(self) -> None:
        schema_path = REPO_ROOT / "extensions" / "pocketbase" / "schema" / "pb_table" / "schema_system_events.json"
        collection = json.loads(schema_path.read_text())[0]
        source_field = next(field for field in collection["fields"] if field.get("name") == "source")

        self.assertIn("ibkr_api", source_field["values"])


if __name__ == "__main__":
    unittest.main()

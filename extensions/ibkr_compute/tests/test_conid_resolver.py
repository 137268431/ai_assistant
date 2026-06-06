import sys
import threading
import time
import unittest
from pathlib import Path


SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.market.conid_resolver import ConidResolver  # noqa: E402


class _PB:
    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.requests = []

    def get_records(self, collection, **kwargs):
        self.requests.append((collection, dict(kwargs)))
        if collection == "ibkr_bars":
            return list(self.rows)
        return []

    def create_record(self, *_args, **_kwargs):
        return {}


class _Broker:
    def __init__(self, conid=0, delay_s=0.0):
        self.conid = int(conid or 0)
        self.delay_s = float(delay_s or 0.0)
        self.calls = []

    def resolve_contract(self, **kwargs):
        self.calls.append(dict(kwargs))
        if self.delay_s:
            time.sleep(self.delay_s)
        return {"symbol": kwargs.get("symbol"), "conid": self.conid} if self.conid else None


class ConidResolverTest(unittest.TestCase):
    def test_resolve_uses_recent_bar_conid_before_live_contract_lookup(self):
        pb = _PB([{"symbol": "AAPL", "extra": '{"conid": 265598, "exchange": "NASDAQ"}'}])
        broker = _Broker(conid=999)
        resolver = ConidResolver(pb_client=pb, broker=broker)

        self.assertEqual(265598, resolver.resolve("AAPL"))
        self.assertEqual([], broker.calls)
        self.assertEqual(265598, resolver.resolve("AAPL"))
        self.assertEqual([], broker.calls)

    def test_resolve_serializes_same_symbol_live_lookup_and_reuses_cache(self):
        pb = _PB([])
        broker = _Broker(conid=272093, delay_s=0.05)
        resolver = ConidResolver(pb_client=pb, broker=broker)
        results = []

        threads = [threading.Thread(target=lambda: results.append(resolver.resolve("MSFT"))) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=1.0)

        self.assertEqual([272093, 272093, 272093], sorted(results))
        self.assertEqual(1, len(broker.calls))


if __name__ == "__main__":
    unittest.main()

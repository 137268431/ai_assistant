import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.market import data_writer as data_writer_mod


class _FakeConfig:
    def __init__(self, bools=None):
        self.bools = dict(bools or {})

    def get_int_for_environment(self, key, environment, fallback):
        return fallback

    def get_float_for_environment(self, key, environment, fallback):
        if key == "ibkr_bar_flush_interval":
            return 3600.0
        return fallback

    def get_bool_for_environment(self, key, environment, fallback):
        return self.bools.get(key, fallback)


class _FakePBClient:
    def __init__(self):
        self.state = {}
        self.upserted_states = []
        self.batches = []

    def upsert_bars(self, batch):
        self.batches.append(list(batch))
        return {"ok": True, "created": len(batch), "updated": 0, "skipped": 0}

    def get_state(self, state_key, environment, date="global"):
        return self.state.get((state_key, environment, date))

    def upsert_state(self, state_key, environment, data, date="global"):
        record = {"data": data}
        self.state[(state_key, environment, date)] = record
        self.upserted_states.append((state_key, environment, date, data))
        return record


def _bar(symbol="AAPL", bar_time_ms=1713797100000):
    return {
        "symbol": symbol,
        "interval": "5m",
        "bar_time_ms": bar_time_ms,
        "open": 180.1,
        "high": 181.4,
        "low": 179.8,
        "close": 181.0,
        "volume": 1234,
        "us_time": "2026-04-22 09:35:00",
        "cn_time": "2026-04-22 21:35:00",
        "source": "ibkr_runtime",
        "environment": "paper",
    }


class DataWriterQueueTest(unittest.TestCase):
    def test_flush_persists_ingest_cursor_and_clears_disk_queue(self):
        pb = _FakePBClient()
        config = _FakeConfig({"ibkr_bar_direct_sqlite_enabled": False})

        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.object(data_writer_mod, "BAR_PENDING_QUEUE_DIR", tmpdir):
                writer = data_writer_mod.DataWriter(pb, config=config, environment="paper")
                try:
                    self.assertTrue(writer.write_bar(_bar()))
                    self.assertTrue(writer.flush())
                    status = writer.status()
                    self.assertEqual(status["pending_batch"], 0)
                    self.assertEqual(status["inflight_batch"], 0)
                    cursor = pb.state[(data_writer_mod.BAR_INGEST_CURSOR_STATE_KEY, "paper", "global")]["data"]
                    self.assertEqual(cursor["intervals"]["5m"]["latest_bar_time_ms"], 1713797100000)
                    self.assertEqual(cursor["intervals"]["5m"]["latest_compute_ingest_bar_time_ms"], 1713797100000)
                    queue_payload = json.loads(Path(status["pending_queue_path"]).read_text(encoding="utf-8"))
                    self.assertEqual(queue_payload["pending"], [])
                    self.assertEqual(queue_payload["inflight"], [])
                finally:
                    writer.close()

    def test_backfill_only_flush_does_not_advance_compute_ingest_cursor(self):
        pb = _FakePBClient()
        config = _FakeConfig({"ibkr_bar_direct_sqlite_enabled": False})

        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.object(data_writer_mod, "BAR_PENDING_QUEUE_DIR", tmpdir):
                writer = data_writer_mod.DataWriter(pb, config=config, environment="paper")
                try:
                    row = _bar("MSFT", 1713797400000)
                    row["source"] = "backfill"
                    row["extra"] = {"source": "ibkr_history_backfill"}
                    self.assertTrue(writer.write_bar(row))
                    self.assertTrue(writer.flush())
                    cursor = pb.state[(data_writer_mod.BAR_INGEST_CURSOR_STATE_KEY, "paper", "global")]["data"]
                    interval = cursor["intervals"]["5m"]
                    self.assertEqual(interval["latest_bar_time_ms"], 1713797400000)
                    self.assertEqual(interval["latest_sources"], ["ibkr_history_backfill"])
                    self.assertEqual(interval["latest_compute_ingest_bar_time_ms"], 0)
                    self.assertEqual(interval["latest_compute_ingest_sources"], [])
                finally:
                    writer.close()

    def test_mixed_flush_tracks_latest_compute_eligible_bar_separately(self):
        pb = _FakePBClient()
        config = _FakeConfig({"ibkr_bar_direct_sqlite_enabled": False})

        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.object(data_writer_mod, "BAR_PENDING_QUEUE_DIR", tmpdir):
                writer = data_writer_mod.DataWriter(pb, config=config, environment="paper")
                try:
                    compute_row = _bar("AAPL", 1713797100000)
                    compute_row["extra"] = {"source": "ibkr_history_close"}
                    backfill_row = _bar("MSFT", 1713797400000)
                    backfill_row["source"] = "backfill"
                    backfill_row["extra"] = {"source": "ibkr_history_backfill"}
                    self.assertTrue(writer.write_bar(compute_row))
                    self.assertTrue(writer.write_bar(backfill_row))
                    self.assertTrue(writer.flush())
                    cursor = pb.state[(data_writer_mod.BAR_INGEST_CURSOR_STATE_KEY, "paper", "global")]["data"]
                    interval = cursor["intervals"]["5m"]
                    self.assertEqual(interval["latest_bar_time_ms"], 1713797400000)
                    self.assertEqual(interval["latest_compute_ingest_bar_time_ms"], 1713797100000)
                    self.assertEqual(interval["latest_compute_ingest_sources"], ["ibkr_history_close"])
                finally:
                    writer.close()

    def test_writer_environment_overrides_bar_broker_environment(self):
        pb = _FakePBClient()
        config = _FakeConfig({"ibkr_bar_direct_sqlite_enabled": False})

        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.object(data_writer_mod, "BAR_PENDING_QUEUE_DIR", tmpdir):
                writer = data_writer_mod.DataWriter(pb, config=config, environment="live")
                try:
                    row = _bar("AAPL", 1713797700000)
                    row["environment"] = "paper"
                    self.assertTrue(writer.write_bar(row))
                    self.assertTrue(writer.flush())
                    self.assertEqual(pb.batches[0][0]["environment"], "live")
                    cursor = pb.state[(data_writer_mod.BAR_INGEST_CURSOR_STATE_KEY, "live", "global")]["data"]
                    self.assertEqual(cursor["environment"], "live")
                    self.assertNotIn((data_writer_mod.BAR_INGEST_CURSOR_STATE_KEY, "paper", "global"), pb.state)
                finally:
                    writer.close()

    def test_reloads_pending_and_inflight_items_from_disk(self):
        pb = _FakePBClient()
        config = _FakeConfig({"ibkr_bar_direct_sqlite_enabled": False})

        with tempfile.TemporaryDirectory() as tmpdir:
            queue_path = Path(tmpdir) / "ibkr_bar_pending_paper.json"
            queue_path.write_text(
                json.dumps({"pending": [_bar("MSFT", 1713797400000)], "inflight": [_bar("AAPL", 1713797100000)]}),
                encoding="utf-8",
            )
            with mock.patch.object(data_writer_mod, "BAR_PENDING_QUEUE_DIR", tmpdir):
                writer = data_writer_mod.DataWriter(pb, config=config, environment="paper")
                try:
                    status = writer.status()
                    self.assertEqual(status["pending_batch"], 2)
                    self.assertEqual(status["inflight_batch"], 0)
                finally:
                    writer.close()

    def test_flush_prefers_direct_sqlite_batch_write(self):
        pb = _FakePBClient()
        config = _FakeConfig()
        direct_batches = []

        def fake_upsert_bars(conn, batch):
            direct_batches.append(list(batch))
            return len(batch)

        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.object(data_writer_mod, "BAR_PENDING_QUEUE_DIR", tmpdir), mock.patch.object(
                data_writer_mod,
                "open_pb_sqlite",
            ) as open_sqlite, mock.patch.object(data_writer_mod, "upsert_bars", side_effect=fake_upsert_bars):
                open_sqlite.return_value.__enter__.return_value = object()
                writer = data_writer_mod.DataWriter(pb, config=config, environment="paper")
                try:
                    self.assertTrue(writer.write_bar(_bar()))
                    self.assertTrue(writer.flush())
                    self.assertEqual(len(direct_batches), 1)
                    self.assertEqual(pb.batches, [])
                    status = writer.status()
                    self.assertEqual(status["direct_sqlite_batch_writes"], 1)
                    self.assertEqual(status["pocketbase_api_batch_writes"], 0)
                    self.assertEqual(status["direct_sqlite_fallbacks"], 0)
                finally:
                    writer.close()

    def test_flush_falls_back_to_api_when_direct_sqlite_fails(self):
        pb = _FakePBClient()
        config = _FakeConfig()

        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.object(data_writer_mod, "BAR_PENDING_QUEUE_DIR", tmpdir), mock.patch.object(
                data_writer_mod,
                "open_pb_sqlite",
                side_effect=RuntimeError("sqlite busy"),
            ):
                writer = data_writer_mod.DataWriter(pb, config=config, environment="paper")
                try:
                    self.assertTrue(writer.write_bar(_bar()))
                    self.assertTrue(writer.flush())
                    self.assertEqual(len(pb.batches), 1)
                    status = writer.status()
                    self.assertEqual(status["direct_sqlite_batch_writes"], 0)
                    self.assertEqual(status["pocketbase_api_batch_writes"], 1)
                    self.assertEqual(status["direct_sqlite_fallbacks"], 1)
                finally:
                    writer.close()


if __name__ == "__main__":
    unittest.main()

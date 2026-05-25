import sys
from pathlib import Path

import pytest

SRC_ROOT = Path(__file__).resolve().parents[3] / "runtime" / "ibkr_compute" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ibkr_compute.order_flow import (  # noqa: E402
    CandidateQueueManager,
    ExecutionPoolManager,
    OrderFlowAggregator,
    OrderFlowCandidate,
    OrderFlowTick,
)


def test_order_flow_aggregator_closes_aligned_cvd_bars():
    aggregator = OrderFlowAggregator()

    assert aggregator.update(OrderFlowTick("aapl", 0, 10.0, 100, "buy", sequence=1)) == []
    assert aggregator.update(OrderFlowTick("AAPL", 5_000, 11.0, 40, "sell", sequence=2)) == []

    closed = aggregator.update(OrderFlowTick("AAPL", 10_000, 12.0, 25, "buy", sequence=3))

    assert [bar.interval_seconds for bar in closed] == [10]
    bar = closed[0]
    assert bar.symbol == "AAPL"
    assert bar.start_ms == 0
    assert bar.end_ms == 10_000
    assert bar.open == 10.0
    assert bar.high == 11.0
    assert bar.low == 10.0
    assert bar.close == 11.0
    assert bar.volume == 140
    assert bar.buy_volume == 100
    assert bar.sell_volume == 40
    assert bar.delta == 60
    assert bar.cvd_open == 0
    assert bar.cvd_high == 100
    assert bar.cvd_low == 0
    assert bar.cvd_close == 60

    remaining = aggregator.close_all("AAPL")
    assert [(bar.interval_seconds, bar.start_ms, bar.cvd_open, bar.cvd_close) for bar in remaining] == [
        (10, 10_000, 60, 85),
        (30, 0, 0, 85),
        (60, 0, 0, 85),
    ]
    assert aggregator.current_cvd("aapl") == 85


def test_order_flow_aggregator_rejects_out_of_order_ticks():
    aggregator = OrderFlowAggregator()

    aggregator.update(OrderFlowTick("MSFT", 2_000, 20.0, 10, "buy"))

    with pytest.raises(ValueError, match="out-of-order"):
        aggregator.update(OrderFlowTick("MSFT", 1_999, 20.1, 5, "sell"))


def test_candidate_queue_merges_conflicts_expires_and_ranks():
    queue = CandidateQueueManager(default_ttl_ms=1_000)

    first = queue.upsert(
        OrderFlowCandidate("AAPL", "long", 0.80, created_at_ms=0, priority=1, payload={"first": True}),
        now_ms=0,
    )
    assert first.accepted
    assert first.action == "queued"

    merged = queue.upsert(
        OrderFlowCandidate("AAPL", "long", 0.75, created_at_ms=100, priority=2, payload={"second": True}),
        now_ms=100,
    )
    assert merged.action == "merged"
    assert merged.candidate.score == 0.80
    assert merged.candidate.priority == 2
    assert dict(merged.candidate.payload) == {"first": True, "second": True}

    rejected = queue.upsert(OrderFlowCandidate("AAPL", "short", 0.79, created_at_ms=200), now_ms=200)
    assert rejected.action == "rejected_conflict"
    assert len(queue) == 1
    assert queue.ranked()[0].side == "long"

    replaced = queue.upsert(OrderFlowCandidate("AAPL", "short", 0.95, created_at_ms=300), now_ms=300)
    assert replaced.action == "replaced_conflict"
    assert [candidate.side for candidate in replaced.replaced] == ["long"]

    queue.upsert(OrderFlowCandidate("MSFT", "long", 0.90, created_at_ms=350, priority=5), now_ms=350)
    assert [candidate.symbol for candidate in queue.ranked(now_ms=400)] == ["AAPL", "MSFT"]

    expired = queue.purge_expired(1_400)
    assert {candidate.symbol for candidate in expired} == {"AAPL", "MSFT"}
    assert queue.ranked() == []


def test_execution_pool_reserves_new_entry_and_releases_cancelled_fill_watch():
    pool = ExecutionPoolManager(max_symbols=3, max_position_slots=1)

    first = pool.reserve_new_entry("aapl", side="long", candidate_id="c1", now_ms=0)
    assert first.accepted
    assert first.slot.symbol == "AAPL"
    assert pool.status()["pending_entry_slots"] == 1

    blocked = pool.reserve_new_entry("MSFT", side="long", candidate_id="c2", now_ms=1)
    assert not blocked.accepted
    assert blocked.reason == "position_slots_full"

    watch = pool.start_fill_watch(first.slot.reservation_id, order_id="order-1", now_ms=2)
    assert watch.accepted
    assert pool.status()["fill_watch_slots"] == 1

    released = pool.release_fill_watch("order-1", outcome="cancelled", now_ms=3)
    assert released.accepted
    assert released.reason == "released_without_position"
    assert pool.status()["used_position_slots"] == 0
    assert pool.reserve_new_entry("MSFT", side="long", candidate_id="c2", now_ms=4).accepted


def test_execution_pool_fill_watch_filled_opens_position_until_position_release():
    pool = ExecutionPoolManager(max_symbols=3, max_position_slots=1)

    reserved = pool.reserve_new_entry("TSLA", side="short", candidate_id="short-1", now_ms=10)
    watch = pool.start_fill_watch(reserved.slot.reservation_id, order_id="order-2", now_ms=11)
    filled = pool.release_fill_watch(watch.slot.reservation_id, outcome="filled", now_ms=12)

    assert filled.accepted
    assert filled.slot.state == "open"
    assert pool.status()["pending_entry_slots"] == 0
    assert pool.status()["open_position_slots"] == 1
    assert pool.status()["used_position_slots"] == 1
    assert pool.reserve_new_entry("NVDA", side="long", now_ms=13).reason == "position_slots_full"

    closed = pool.release_position("TSLA", now_ms=14)
    assert closed.accepted
    assert pool.reserve_new_entry("NVDA", side="long", now_ms=15).accepted


def test_execution_pool_enforces_symbol_cap_when_position_capacity_allows():
    pool = ExecutionPoolManager(max_symbols=3, max_position_slots=5)

    assert pool.reserve_new_entry("AAPL", side="long", now_ms=0).accepted
    assert pool.reserve_new_entry("MSFT", side="long", now_ms=0).accepted
    assert pool.reserve_new_entry("NVDA", side="long", now_ms=0).accepted

    rejected = pool.reserve_new_entry("TSLA", side="long", now_ms=0)
    assert not rejected.accepted
    assert rejected.reason == "symbol_pool_full"
    assert pool.status()["active_symbols"] == ["AAPL", "MSFT", "NVDA"]

from __future__ import annotations

from .runtime_support import *
from .runtime_records import BacktestRuntimeRecordsMixin
from .analysis import BacktestAnalysisMixin
from .orchestration import BacktestOrchestrationMixin
from .market_data import BacktestMarketDataMixin
from .scan_replay import BacktestScanReplayMixin
from .portfolio import BacktestPortfolioMixin
from .symbol_rows import BacktestSymbolRowsMixin
from .tv_parity import BacktestTvParityMixin
from .persistence import BacktestPersistenceMixin


class BacktestService(
    BacktestRuntimeRecordsMixin,
    BacktestAnalysisMixin,
    BacktestOrchestrationMixin,
    BacktestMarketDataMixin,
    BacktestScanReplayMixin,
    BacktestPortfolioMixin,
    BacktestSymbolRowsMixin,
    BacktestTvParityMixin,
    BacktestPersistenceMixin,
):
    def __init__(self, pb_client: PBClient, account_snapshot_provider: Callable[..., dict] | None = None):
        self.pb = pb_client
        self.account_snapshot_provider = account_snapshot_provider
        self._history_broker = self._build_history_broker() if pb_client else None
        self.data_backfill = DataBackfill(broker=self._history_broker) if pb_client else None
        self.conid_resolver = ConidResolver(pb_client=pb_client, broker=self._history_broker) if pb_client else None
        if self.conid_resolver is not None:
            try:
                self.conid_resolver.load_cache_from_pb()
            except Exception:
                pass
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._cancel_event = threading.Event()
        self._active_run_id = ""
        self._active_batch_id = ""
        self._active_payload: Dict[str, Any] = {}
        self._progress = {
            "status": "idle",
            "run_id": "",
            "batch_id": "",
            "mode": "single",
            "progress": 0,
            "stage": "idle",
            "message": "",
            "updated_at_ms": 0,
        }

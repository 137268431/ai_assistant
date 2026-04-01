"""
每日标的扫描器 — 盘前7:00-10:00扫描全部watchlist标的

扫描逻辑 (初版, 纯技术面):
  1. 读取 watchlist 全部标的
  2. 对每个标的取多TF指标快照
  3. 评分: SD位置 + DTP方向/phase + EMA排列 + ATR波动 + 盘前涨跌幅
  4. 写入 daily_targets

TODO P4: 完整实现评分逻辑
"""

from qc_compute.integrations.pb_client import PBClient


class DailyScanner:
    def __init__(self, pb_client: PBClient, engines: dict):
        self.pb_client = pb_client
        self.engines = engines

    def run_scan(self, date: str) -> dict:
        """
        执行盘前扫描

        返回: {scanned: int, candidates: int, errors: int}
        """
        watchlist = self._get_watchlist()
        scanned = 0
        candidates = 0
        errors = 0

        for item in watchlist:
            symbol = item.get("symbol", "").upper()
            if not symbol:
                continue
            scanned += 1

            try:
                result = self.evaluate_symbol(symbol, date)
                if result and result.get("score", 0) > 0:
                    self.pb_client.upsert_scan({
                        "symbol": symbol,
                        "exchange": item.get("exchange", ""),
                        "date": date,
                        "direction_bias": result.get("direction_bias", "neutral"),
                        "score": result.get("score", 0),
                        "scan_reason": result.get("reason", ""),
                        "status": "candidate",
                        "extra": result.get("extra", {}),
                    })
                    candidates += 1
            except Exception as e:
                errors += 1
                print(f"[Scanner] {symbol} error: {e}")

        return {"scanned": scanned, "candidates": candidates, "errors": errors}

    def evaluate_symbol(self, symbol: str, date: str) -> dict:
        """
        单标的评分

        TODO P4: 实现完整评分逻辑
        """
        # 收集各TF引擎快照
        snapshots = {}
        for (sym, tf), engine in self.engines.items():
            if sym == symbol and engine.is_ready():
                snapshots[tf] = engine.get_snapshot()

        if not snapshots:
            return None

        # TODO P4: 基于指标快照计算评分
        return {
            "score": 0,
            "direction_bias": "neutral",
            "reason": "",
            "extra": {"timeframes_ready": list(snapshots.keys())},
        }

    def _get_watchlist(self) -> list:
        try:
            return self.pb_client.get_records("watchlist", per_page=500)
        except Exception as e:
            print(f"[Scanner] get watchlist error: {e}")
            return []

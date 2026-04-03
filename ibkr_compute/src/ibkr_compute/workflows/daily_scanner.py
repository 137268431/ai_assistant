"""
每日标的扫描器 — 盘前7:00-10:00扫描全部watchlist标的

扫描逻辑 (初版, 纯技术面):
  1. 读取 watchlist 全部标的
  2. 对每个标的取多TF指标快照
  3. 评分: SD位置 + DTP方向/phase + EMA排列 + ATR波动 + 盘前涨跌幅
  4. 写入 ibkr_targets

TODO P4: 完整实现评分逻辑
"""

from ibkr_compute.integrations.pb_client import PBClient


class DailyScanner:
    def __init__(self, pb_client: PBClient, engines: dict):
        self.pb_client = pb_client
        self.engines = engines

    def run_scan(self, date: str, environments=None) -> dict:
        """
        执行盘前扫描

        返回: {scanned: int, candidates: int, errors: int}
        """
        scanned = 0
        candidates = 0
        errors = 0

        runtime_environments = environments or ["live", "paper"]

        for environment in runtime_environments:
            watchlist = self._get_watchlist(environment)
            for item in watchlist:
                symbol = item.get("symbol", "").upper()
                if not symbol:
                    continue
                scanned += 1

                try:
                    result = self.evaluate_symbol(symbol, date, environment)
                    if result and result.get("score", 0) > 0:
                        self.pb_client.upsert_scan({
                            "environment": environment,
                            "symbol": symbol,
                            "exchange": item.get("exchange", ""),
                            "date": date,
                            "direction_bias": result.get("direction_bias", "neutral"),
                            "score": result.get("score", 0),
                            "scan_reason": result.get("reason", ""),
                            "status": "candidate",
                            "extra": {"environment": environment, **result.get("extra", {})},
                        })
                        candidates += 1
                except Exception as e:
                    errors += 1
                    print(f"[Scanner] {environment}/{symbol} error: {e}")

        return {"scanned": scanned, "candidates": candidates, "errors": errors}

    def evaluate_symbol(self, symbol: str, date: str, environment: str) -> dict:
        """
        单标的评分

        先提供一个稳定的轻量评分，避免 scan 永远产出 0 个候选。
        """
        snapshots = {}
        for (runtime_environment, sym, tf), engine in self.engines.items():
            if runtime_environment == environment and sym == symbol and engine.is_ready():
                snapshots[tf] = engine.get_snapshot()

        if not snapshots:
            return None

        score = 0
        long_votes = 0
        short_votes = 0
        reasons = []

        for tf, snapshot in snapshots.items():
            if not isinstance(snapshot, dict):
                continue

            trend_dir = snapshot.get("trend_dir")
            dtp_dir = snapshot.get("dtp_dir")
            ema_bullish = bool(snapshot.get("ema_bullish"))
            ema_bearish = bool(snapshot.get("ema_bearish"))
            fractal_bull = bool(snapshot.get("fractal_bull"))
            fractal_bear = bool(snapshot.get("fractal_bear"))
            crsi_os = bool(snapshot.get("crsi_os"))
            crsi_ob = bool(snapshot.get("crsi_ob"))
            sd_lower = bool(snapshot.get("sd_lower"))
            sd_upper = bool(snapshot.get("sd_upper"))

            if trend_dir == 1 or dtp_dir == 1 or ema_bullish:
                long_votes += 1
                score += 2
            if trend_dir == -1 or dtp_dir == -1 or ema_bearish:
                short_votes += 1
                score += 2

            if fractal_bull or crsi_os or sd_lower:
                long_votes += 1
                score += 1
            if fractal_bear or crsi_ob or sd_upper:
                short_votes += 1
                score += 1

            if fractal_bull:
                reasons.append(f"{tf}:fractal_bull")
            if fractal_bear:
                reasons.append(f"{tf}:fractal_bear")
            if ema_bullish:
                reasons.append(f"{tf}:ema_bullish")
            if ema_bearish:
                reasons.append(f"{tf}:ema_bearish")
            if sd_lower:
                reasons.append(f"{tf}:sd_lower")
            if sd_upper:
                reasons.append(f"{tf}:sd_upper")

        if long_votes == short_votes:
            direction_bias = "neutral"
        elif long_votes > short_votes:
            direction_bias = "long"
        else:
            direction_bias = "short"

        if direction_bias == "neutral":
            return {
                "score": 0,
                "direction_bias": direction_bias,
                "reason": "vote_tie",
                "extra": {
                    "environment": environment,
                    "timeframes_ready": sorted(snapshots.keys()),
                    "long_votes": long_votes,
                    "short_votes": short_votes,
                },
            }

        score += len(snapshots)

        return {
            "score": score,
            "direction_bias": direction_bias,
            "reason": ", ".join(reasons[:6]),
            "extra": {
                "environment": environment,
                "timeframes_ready": sorted(snapshots.keys()),
                "long_votes": long_votes,
                "short_votes": short_votes,
            },
        }

    def _get_watchlist(self, environment: str) -> list:
        try:
            records = self.pb_client.get_records(
                "watchlist",
                filter=f'environment = "{environment}" || environment = "global" || environment = ""',
                per_page=500
            )
            merged = {}
            priority = {"": 0, "global": 1, environment: 2}
            applied = {}
            for item in records:
                symbol = str(item.get("symbol", "")).upper()
                if not symbol:
                    continue
                env = str(item.get("environment", "") or "").strip().lower()
                rank = priority.get(env, -1)
                if symbol in applied and applied[symbol] > rank:
                    continue
                applied[symbol] = rank
                merged[symbol] = item
            return list(merged.values())
        except Exception as e:
            print(f"[Scanner] get watchlist error: {e}")
            return []

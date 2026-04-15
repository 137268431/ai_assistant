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

WATCHLIST_SYMBOL_ROLE_TRADE = "trade"
DAILY_SCAN_PRIMARY_WEIGHT = 2
DAILY_SCAN_SECONDARY_WEIGHT = 1
DAILY_SCAN_READY_TIMEFRAME_BONUS = 1

DAILY_SCAN_LONG_PRIMARY_RULES = (
    ("trend_dir", 1, "trend_dir=1"),
    ("dtp_dir", 1, "dtp_dir=1"),
    ("ema_bullish", True, "ema_bullish=true"),
)
DAILY_SCAN_SHORT_PRIMARY_RULES = (
    ("trend_dir", -1, "trend_dir=-1"),
    ("dtp_dir", -1, "dtp_dir=-1"),
    ("ema_bearish", True, "ema_bearish=true"),
)
DAILY_SCAN_LONG_SECONDARY_RULES = (
    ("fractal_bull", True, "fractal_bull=true"),
    ("crsi_os", True, "crsi_os=true"),
    ("sd_lower", True, "sd_lower=true"),
)
DAILY_SCAN_SHORT_SECONDARY_RULES = (
    ("fractal_bear", True, "fractal_bear=true"),
    ("crsi_ob", True, "crsi_ob=true"),
    ("sd_upper", True, "sd_upper=true"),
)
DAILY_SCAN_REASON_RULES = (
    ("fractal_bull", "fractal_bull"),
    ("fractal_bear", "fractal_bear"),
    ("ema_bullish", "ema_bullish"),
    ("ema_bearish", "ema_bearish"),
    ("sd_lower", "sd_lower"),
    ("sd_upper", "sd_upper"),
)


def _daily_scan_rule_matches(snapshot: dict, rule: tuple[str, object, str]) -> bool:
    key, expected, _ = rule
    value = snapshot.get(key)
    if isinstance(expected, bool):
        return bool(value) is expected
    return value == expected


def _daily_scan_matches_any(snapshot: dict, rules) -> bool:
    return any(_daily_scan_rule_matches(snapshot, rule) for rule in rules)


def build_daily_scan_rule_summary() -> dict:
    return {
        "primary_weight": DAILY_SCAN_PRIMARY_WEIGHT,
        "secondary_weight": DAILY_SCAN_SECONDARY_WEIGHT,
        "ready_timeframe_bonus": DAILY_SCAN_READY_TIMEFRAME_BONUS,
        "long_primary": [label for _, _, label in DAILY_SCAN_LONG_PRIMARY_RULES],
        "short_primary": [label for _, _, label in DAILY_SCAN_SHORT_PRIMARY_RULES],
        "long_secondary": [label for _, _, label in DAILY_SCAN_LONG_SECONDARY_RULES],
        "short_secondary": [label for _, _, label in DAILY_SCAN_SHORT_SECONDARY_RULES],
        "tie_behavior": "long_votes == short_votes => direction_bias=neutral, score=0",
        "final_bonus": "direction_bias 非 neutral 时额外加上 ready_timeframes_count",
        "reason_fields": [label for _, label in DAILY_SCAN_REASON_RULES],
    }


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

            if _daily_scan_matches_any(snapshot, DAILY_SCAN_LONG_PRIMARY_RULES):
                long_votes += 1
                score += DAILY_SCAN_PRIMARY_WEIGHT
            if _daily_scan_matches_any(snapshot, DAILY_SCAN_SHORT_PRIMARY_RULES):
                short_votes += 1
                score += DAILY_SCAN_PRIMARY_WEIGHT

            if _daily_scan_matches_any(snapshot, DAILY_SCAN_LONG_SECONDARY_RULES):
                long_votes += 1
                score += DAILY_SCAN_SECONDARY_WEIGHT
            if _daily_scan_matches_any(snapshot, DAILY_SCAN_SHORT_SECONDARY_RULES):
                short_votes += 1
                score += DAILY_SCAN_SECONDARY_WEIGHT

            for rule_key, rule_label in DAILY_SCAN_REASON_RULES:
                if snapshot.get(rule_key):
                    reasons.append(f"{tf}:{rule_label}")

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

        score += len(snapshots) * DAILY_SCAN_READY_TIMEFRAME_BONUS

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
                filter=(
                    f'(environment = "{environment}" || environment = "global" || environment = "") '
                    f'&& (symbol_role = "{WATCHLIST_SYMBOL_ROLE_TRADE}" || symbol_role = "")'
                ),
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

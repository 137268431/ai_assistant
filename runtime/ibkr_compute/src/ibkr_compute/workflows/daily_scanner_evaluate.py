"""Symbol evaluation for the daily IBKR scanner."""

from __future__ import annotations

from ibkr_compute.market.timeframe_utils import normalize_interval
from ibkr_compute.universe.dynamic_admission import (
    evaluate_dynamic_admission,
    normalize_admission_bool,
)

from .daily_scanner_constants import (
    DAILY_SCAN_LONG_PRIMARY_RULES,
    DAILY_SCAN_LONG_SECONDARY_RULES,
    DAILY_SCAN_PRIMARY_WEIGHT,
    DAILY_SCAN_READY_TIMEFRAME_BONUS,
    DAILY_SCAN_REASON_RULES,
    DAILY_SCAN_SECONDARY_WEIGHT,
    DAILY_SCAN_SHORT_PRIMARY_RULES,
    DAILY_SCAN_SHORT_SECONDARY_RULES,
    DEFAULT_MIN_ABS_DAY_CHANGE_PCT,
    DEFAULT_MIN_ATR_PCT,
    DEFAULT_MIN_AVG_10D_VOLUME,
    DEFAULT_MIN_PREMARKET_VOLUME,
    REJECTION_BUCKET_ATR,
    REJECTION_BUCKET_AVG_10D,
    REJECTION_BUCKET_DAY_CHANGE,
    REJECTION_BUCKET_NO_SNAPSHOT,
    REJECTION_BUCKET_PREMARKET,
    REJECTION_BUCKET_VOTE_TIE,
)
from .daily_scanner_support import (
    _build_stocks_in_play_bonus,
    _daily_scan_matches_any,
    _format_metric_value,
    _format_threshold,
    _metric_rank_bonus,
    _safe_float,
)


class DailyScannerEvaluateMixin:
    def evaluate_symbol(
        self,
        symbol: str,
        date: str,
        environment: str,
        *,
        metrics: dict | None = None,
        settings: dict | None = None,
        stored_snapshots: dict | None = None,
    ) -> dict | None:
        """
        单标的评分：
        先看 multi-TF 方向，再叠加日内波动型质量门。
        """
        del date
        runtime_environment = str(environment or "live").strip().lower() or "live"
        settings = settings or self._load_scan_settings(runtime_environment)
        snapshots = {}
        for (engine_environment, sym, tf), engine in self.engines.items():
            if engine_environment == runtime_environment and sym == symbol and engine.is_ready():
                snapshots[tf] = engine.get_snapshot()
        for tf, snapshot in (stored_snapshots or {}).items():
            normalized_tf = normalize_interval(tf)
            if normalized_tf not in snapshots and isinstance(snapshot, dict):
                snapshots[normalized_tf] = dict(snapshot)

        if not snapshots:
            return {
                "symbol": symbol,
                "score": 0,
                "technical_score": 0,
                "direction_bias": "neutral",
                "reason": "no_snapshot",
                "quality_gate_passed": False,
                "admission_score": 0.0,
                "symbol_profile": {},
                "dynamic_thresholds": {},
                "failed_gates": [],
                "strategy_policy": {
                    "risk_profile": "avoid",
                    "setup_type": "no_snapshot",
                    "avoid_new_entries": True,
                },
                "rejection_examples": [
                    {
                        "bucket": REJECTION_BUCKET_NO_SNAPSHOT,
                        "symbol": symbol,
                        "actual": "ready_timeframes=0",
                        "threshold": ">=1 ready timeframe",
                        "note": "缺少可用技术快照",
                    }
                ],
                "extra": {
                    "environment": runtime_environment,
                    "timeframes_ready": [],
                    "long_votes": 0,
                    "short_votes": 0,
                },
            }

        technical_score = 0
        long_votes = 0
        short_votes = 0
        technical_reasons = []

        for tf, snapshot in snapshots.items():
            if not isinstance(snapshot, dict):
                continue

            if _daily_scan_matches_any(snapshot, DAILY_SCAN_LONG_PRIMARY_RULES):
                long_votes += 1
                technical_score += DAILY_SCAN_PRIMARY_WEIGHT
            if _daily_scan_matches_any(snapshot, DAILY_SCAN_SHORT_PRIMARY_RULES):
                short_votes += 1
                technical_score += DAILY_SCAN_PRIMARY_WEIGHT

            if _daily_scan_matches_any(snapshot, DAILY_SCAN_LONG_SECONDARY_RULES):
                long_votes += 1
                technical_score += DAILY_SCAN_SECONDARY_WEIGHT
            if _daily_scan_matches_any(snapshot, DAILY_SCAN_SHORT_SECONDARY_RULES):
                short_votes += 1
                technical_score += DAILY_SCAN_SECONDARY_WEIGHT

            for rule_key, rule_label in DAILY_SCAN_REASON_RULES:
                if snapshot.get(rule_key):
                    technical_reasons.append(f"{tf}:{rule_label}")

        if long_votes == short_votes:
            direction_bias = "neutral"
        elif long_votes > short_votes:
            direction_bias = "long"
        else:
            direction_bias = "short"

        if direction_bias == "neutral":
            return {
                "symbol": symbol,
                "score": 0,
                "technical_score": 0,
                "direction_bias": direction_bias,
                "reason": "vote_tie",
                "quality_gate_passed": False,
                "admission_score": 0.0,
                "symbol_profile": {},
                "dynamic_thresholds": {},
                "failed_gates": [],
                "strategy_policy": {
                    "risk_profile": "avoid",
                    "setup_type": "vote_tie",
                    "avoid_new_entries": True,
                },
                "rejection_examples": [
                    {
                        "bucket": REJECTION_BUCKET_VOTE_TIE,
                        "symbol": symbol,
                        "actual": f"long_votes={long_votes}, short_votes={short_votes}",
                        "threshold": "long_votes != short_votes",
                        "note": "技术投票平票",
                    }
                ],
                "extra": {
                    "environment": runtime_environment,
                    "timeframes_ready": sorted(snapshots.keys()),
                    "long_votes": long_votes,
                    "short_votes": short_votes,
                },
            }

        technical_score += len(snapshots) * DAILY_SCAN_READY_TIMEFRAME_BONUS
        metric_row = dict(metrics or {})
        avg_10d_volume = _safe_float(metric_row.get("avg_10d_volume"))
        premarket_volume = _safe_float(metric_row.get("premarket_volume"))
        atr_pct = abs(_safe_float(metric_row.get("atr_pct")))
        day_change_pct = _safe_float(metric_row.get("day_change_pct"))

        dynamic_enabled = normalize_admission_bool(settings.get("dynamic_admission_enabled"), False)
        admission_score = 0.0
        admission_score_components = {}
        symbol_profile = {}
        dynamic_thresholds = {
            "mode": "legacy",
            "avg_10d_volume_gte": _safe_float(settings.get("min_avg_10d_volume"), DEFAULT_MIN_AVG_10D_VOLUME),
            "premarket_volume_gte": _safe_float(settings.get("min_premarket_volume"), DEFAULT_MIN_PREMARKET_VOLUME),
            "atr_pct_gte": _safe_float(settings.get("min_atr_pct"), DEFAULT_MIN_ATR_PCT),
            "abs_day_change_pct_gte": _safe_float(
                settings.get("min_abs_day_change_pct"),
                DEFAULT_MIN_ABS_DAY_CHANGE_PCT,
            ),
        }
        strategy_policy = {
            "risk_profile": "legacy",
            "setup_type": "fixed_quality_gates",
            "allowed_sides": [direction_bias],
            "signal_confirmation": "standard",
            "position_size_multiplier": 1.0,
            "avoid_new_entries": False,
        }
        failed_gates = []
        rejection_examples = []
        if dynamic_enabled:
            admission = evaluate_dynamic_admission(
                symbol,
                metrics=metric_row,
                settings=settings,
                direction_bias=direction_bias,
            )
            quality_gate_passed = bool(admission.get("quality_gate_passed"))
            admission_score = _safe_float(admission.get("admission_score"))
            admission_score_components = dict(admission.get("admission_score_components") or {})
            symbol_profile = dict(admission.get("symbol_profile") or {})
            dynamic_thresholds = dict(admission.get("dynamic_thresholds") or {})
            strategy_policy = dict(admission.get("strategy_policy") or {})
            failed_gates = list(admission.get("failed_gates") or [])
            rejection_examples = failed_gates
            gate_reasons = list(admission.get("reason_tags") or [])
        else:
            min_avg_10d_volume = _safe_float(settings.get("min_avg_10d_volume"), DEFAULT_MIN_AVG_10D_VOLUME)
            min_premarket_volume = _safe_float(settings.get("min_premarket_volume"), DEFAULT_MIN_PREMARKET_VOLUME)
            min_atr_pct = _safe_float(settings.get("min_atr_pct"), DEFAULT_MIN_ATR_PCT)
            min_abs_day_change_pct = _safe_float(
                settings.get("min_abs_day_change_pct"),
                DEFAULT_MIN_ABS_DAY_CHANGE_PCT,
            )
            quality_gate_passed = (
                avg_10d_volume >= min_avg_10d_volume
                and premarket_volume >= min_premarket_volume
                and atr_pct >= min_atr_pct
                and abs(day_change_pct) >= min_abs_day_change_pct
            )

            gate_reasons = [
                f"10d>={_format_threshold(min_avg_10d_volume)}",
                f"pre>={_format_threshold(min_premarket_volume)}",
                f"atr>={_format_threshold(min_atr_pct)}",
                f"|day|>={_format_threshold(min_abs_day_change_pct)}",
            ]
            if avg_10d_volume < min_avg_10d_volume:
                rejection_examples.append(
                    {
                        "bucket": REJECTION_BUCKET_AVG_10D,
                        "symbol": symbol,
                        "actual": _format_metric_value(avg_10d_volume),
                        "threshold": _format_threshold(min_avg_10d_volume),
                        "note": "10 日均量不足",
                    }
                )
            if premarket_volume < min_premarket_volume:
                rejection_examples.append(
                    {
                        "bucket": REJECTION_BUCKET_PREMARKET,
                        "symbol": symbol,
                        "actual": _format_metric_value(premarket_volume),
                        "threshold": _format_threshold(min_premarket_volume),
                        "note": "盘前量不足",
                    }
                )
            if atr_pct < min_atr_pct:
                rejection_examples.append(
                    {
                        "bucket": REJECTION_BUCKET_ATR,
                        "symbol": symbol,
                        "actual": _format_metric_value(atr_pct),
                        "threshold": _format_threshold(min_atr_pct),
                        "note": "ATR 不足",
                    }
                )
            if abs(day_change_pct) < min_abs_day_change_pct:
                rejection_examples.append(
                    {
                        "bucket": REJECTION_BUCKET_DAY_CHANGE,
                        "symbol": symbol,
                        "actual": _format_metric_value(abs(day_change_pct)),
                        "threshold": _format_threshold(min_abs_day_change_pct),
                        "note": "日内涨跌幅不足",
                    }
                )
            failed_gates = list(rejection_examples)
            admission_score = 100.0 if quality_gate_passed else 0.0
            symbol_profile = {
                "symbol": symbol,
                "exchange": str(metric_row.get("exchange", "") or "").strip().upper(),
                "avg_10d_volume": round(avg_10d_volume, 2),
                "premarket_volume": round(premarket_volume, 2),
                "atr_pct": round(atr_pct, 4),
                "day_change_pct": round(day_change_pct, 2),
            }
            strategy_policy["avoid_new_entries"] = not quality_gate_passed

        stocks_in_play_bonus, stocks_in_play_reasons, stocks_in_play_details = _build_stocks_in_play_bonus(
            metric_row,
            snapshots,
            direction_bias,
        )
        rank_bonus = _metric_rank_bonus(metric_row)
        admission_bonus = (admission_score / 10.0) if dynamic_enabled else 0.0
        final_score = technical_score + rank_bonus + stocks_in_play_bonus + admission_bonus
        reason_items = list(dict.fromkeys(technical_reasons[:4] + stocks_in_play_reasons + gate_reasons))
        reason_text = ", ".join(reason_items[:8]).strip()
        if not reason_text:
            reason_text = (
                f"dir={direction_bias}, tech={technical_score}, pre={round(premarket_volume)}, "
                f"avg10d={round(avg_10d_volume)}, atr={round(atr_pct, 4)}, day={round(day_change_pct, 2)}"
            )

        return {
            "symbol": symbol,
            "score": round(final_score, 3),
            "technical_score": round(technical_score, 3),
            "direction_bias": direction_bias,
            "reason": reason_text,
            "quality_gate_passed": quality_gate_passed,
            "rejection_examples": rejection_examples,
            "failed_gates": failed_gates,
            "admission_score": round(admission_score, 3),
            "symbol_profile": symbol_profile,
            "dynamic_thresholds": dynamic_thresholds,
            "strategy_policy": strategy_policy,
            "exchange": str(metric_row.get("exchange", "") or "").strip().upper(),
            "avg_10d_volume": round(avg_10d_volume, 2),
            "premarket_volume": round(premarket_volume, 2),
            "atr_pct": round(atr_pct, 4),
            "day_change_pct": round(day_change_pct, 2),
            "extra": {
                "environment": runtime_environment,
                "timeframes_ready": sorted(snapshots.keys()),
                "long_votes": long_votes,
                "short_votes": short_votes,
                "rank_bonus": rank_bonus,
                "dynamic_admission_enabled": dynamic_enabled,
                "admission_score": round(admission_score, 3),
                "admission_score_components": admission_score_components,
                "admission_bonus": round(admission_bonus, 3),
                "symbol_profile": symbol_profile,
                "dynamic_thresholds": dynamic_thresholds,
                "failed_gates": failed_gates,
                "strategy_policy": strategy_policy,
                **stocks_in_play_details,
            },
        }

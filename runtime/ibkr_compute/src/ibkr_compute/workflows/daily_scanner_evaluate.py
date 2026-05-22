"""Symbol evaluation for the daily IBKR scanner."""

from __future__ import annotations

from ibkr_compute.core.active_window_admission import signal_pressure_from_item
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
    DEFAULT_DAY_GAIN_TRIGGER_PCT,
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
    _first_present_value,
    _format_metric_value,
    _format_threshold,
    _metric_rank_bonus,
    _safe_float,
    _snapshot_best_float,
    _snapshot_first_text,
    _truthy_scan_value,
)
from .daily_scanner_target_reasons import build_daily_scan_trigger_timeline


_LONG_CYCLE_TIMEFRAMES = ("1d", "4h")
_STRUCTURE_TIMEFRAMES = ("1h", "30m")
_TRIGGER_TIMEFRAMES = ("15m", "5m")


def _snapshot_has_long(snapshot: dict) -> bool:
    return _daily_scan_matches_any(snapshot, DAILY_SCAN_LONG_PRIMARY_RULES) or _daily_scan_matches_any(
        snapshot,
        DAILY_SCAN_LONG_SECONDARY_RULES,
    )


def _snapshot_has_short(snapshot: dict) -> bool:
    return _daily_scan_matches_any(snapshot, DAILY_SCAN_SHORT_PRIMARY_RULES) or _daily_scan_matches_any(
        snapshot,
        DAILY_SCAN_SHORT_SECONDARY_RULES,
    )


def _side_set_from_snapshots(snapshots: dict[str, dict], timeframes: tuple[str, ...]) -> set[str]:
    sides: set[str] = set()
    for tf in timeframes:
        snapshot = snapshots.get(tf)
        if not isinstance(snapshot, dict):
            continue
        if _snapshot_has_long(snapshot):
            sides.add("long")
        if _snapshot_has_short(snapshot):
            sides.add("short")
    return sides


def _trigger_sides(snapshots: dict[str, dict], metric_row: dict) -> set[str]:
    sides = _side_set_from_snapshots(snapshots, _TRIGGER_TIMEFRAMES)
    trigger_snapshots = {
        tf: snapshot
        for tf, snapshot in snapshots.items()
        if tf in _TRIGGER_TIMEFRAMES and isinstance(snapshot, dict)
    }
    orb_text = str(_first_present_value(metric_row.get("orb_breakout"), "") or "").strip().lower()
    orb_up = _truthy_scan_value(metric_row.get("orb_breakout_up")) or _truthy_scan_value(
        _snapshot_first_text(trigger_snapshots, "orb_breakout_up")
    )
    orb_down = _truthy_scan_value(metric_row.get("orb_breakout_down")) or _truthy_scan_value(
        _snapshot_first_text(trigger_snapshots, "orb_breakout_down")
    )
    if orb_text in {"up", "long", "breakout_up"}:
        orb_up = True
    elif orb_text in {"down", "short", "breakout_down"}:
        orb_down = True
    if orb_up:
        sides.add("long")
    if orb_down:
        sides.add("short")

    vwap_alignment = str(
        _first_present_value(metric_row.get("vwap_alignment"), _snapshot_first_text(trigger_snapshots, "vwap_alignment"))
        or ""
    ).strip().lower()
    if vwap_alignment in {"above", "bullish", "long"}:
        sides.add("long")
    elif vwap_alignment in {"below", "bearish", "short"}:
        sides.add("short")
    return sides


def _sides_with_signal(
    snapshots: dict[str, dict],
    timeframes: tuple[str, ...],
    *,
    long_keys: tuple[str, ...],
    short_keys: tuple[str, ...],
) -> set[str]:
    sides: set[str] = set()
    for tf in timeframes:
        snapshot = snapshots.get(tf)
        if not isinstance(snapshot, dict):
            continue
        if any(bool(snapshot.get(key)) for key in long_keys):
            sides.add("long")
        if any(bool(snapshot.get(key)) for key in short_keys):
            sides.add("short")
    return sides


def _signal_pressure_from_snapshots(snapshots: dict[str, dict], timeframes: tuple[str, ...]) -> dict:
    sides: set[str] = set()
    keys: list[str] = []
    by_timeframe: dict[str, dict] = {}
    for tf in timeframes:
        snapshot = snapshots.get(tf)
        if not isinstance(snapshot, dict):
            continue
        pressure = signal_pressure_from_item(snapshot)
        by_timeframe[tf] = pressure
        if not pressure.get("signal_pressure_passed"):
            continue
        for side in pressure.get("signal_pressure_sides") or []:
            if side in {"long", "short"}:
                sides.add(side)
        for key in pressure.get("signal_pressure_keys") or []:
            labeled = f"{tf}:{key}"
            if labeled not in keys:
                keys.append(labeled)
    return {
        "signal_pressure_passed": bool(sides),
        "signal_pressure_sides": sorted(sides),
        "signal_pressure_keys": keys,
        "signal_pressure_by_timeframe": by_timeframe,
    }


def _build_context_gate(
    *,
    snapshots: dict[str, dict],
    metric_row: dict,
    direction_bias: str,
    quality_gate_passed: bool,
    day_gain_triggered: bool,
) -> dict:
    long_cycle_sides = _side_set_from_snapshots(snapshots, _LONG_CYCLE_TIMEFRAMES)
    structure_sides = _side_set_from_snapshots(snapshots, _STRUCTURE_TIMEFRAMES)
    context_trigger_sides = _trigger_sides(snapshots, metric_row)
    pressure = _signal_pressure_from_snapshots(
        snapshots,
        _TRIGGER_TIMEFRAMES + _STRUCTURE_TIMEFRAMES,
    )
    trigger_sides = set(pressure["signal_pressure_sides"])
    direction = str(direction_bias or "").strip().lower()
    direction_sides = {direction} if direction in {"long", "short"} else {"long", "short"}
    effective_structure_sides = structure_sides or context_trigger_sides or trigger_sides
    effective_regime_sides = long_cycle_sides or effective_structure_sides
    trend_sides = effective_regime_sides & effective_structure_sides & trigger_sides & direction_sides

    sd_sides = _sides_with_signal(
        snapshots,
        _TRIGGER_TIMEFRAMES + _STRUCTURE_TIMEFRAMES,
        long_keys=("sd_lower",),
        short_keys=("sd_upper",),
    )
    exhaustion_sides = _sides_with_signal(
        snapshots,
        _TRIGGER_TIMEFRAMES + _STRUCTURE_TIMEFRAMES,
        long_keys=("crsi_os", "crsi_bull_div", "obv_bull_div", "fractal_bull"),
        short_keys=("crsi_ob", "crsi_bear_div", "obv_bear_div", "fractal_bear"),
    )
    reversal_sides: set[str] = set()
    if "long" in sd_sides and "long" in exhaustion_sides and "short" in long_cycle_sides:
        reversal_sides.add("long")
    if "short" in sd_sides and "short" in exhaustion_sides and "long" in long_cycle_sides:
        reversal_sides.add("short")
    reversal_sides &= (structure_sides | trigger_sides)
    reversal_sides &= trigger_sides

    hot_sides = set()
    rvol_20 = max(_safe_float(metric_row.get("rvol_20")), _snapshot_best_float(snapshots, "rvol_20", 0.0))

    setup_family = ""
    family_score = 0.0
    family_reason = ""
    allowed_sides: list[str] = []
    if reversal_sides:
        allowed_sides = sorted(reversal_sides)
        setup_family = "exhaustion_reversal"
        family_score = 22.0
        family_reason = "quality + trade-window SD/cRSI pressure + exhaustion reversal"
    elif hot_sides:
        allowed_sides = sorted(hot_sides)
        setup_family = "hot_momentum"
        family_score = 18.0
        family_reason = "quality + regime/structure context + 15m/5m hot trigger"
    elif trigger_sides:
        aligned_sides = trigger_sides & direction_sides
        if not aligned_sides and direction not in {"long", "short"}:
            aligned_sides = set(trigger_sides)
        allowed_sides = sorted(aligned_sides or trigger_sides)
        if trend_sides and structure_sides:
            setup_family = "signal_window_trend"
            family_score = 20.0
            family_reason = "quality + multi-timeframe context + trade-window SD/cRSI pressure"
        else:
            setup_family = "signal_pressure"
            family_score = 16.0
            family_reason = "quality + trade-window SD/cRSI pressure"
    elif trend_sides and structure_sides:
        allowed_sides = sorted(trend_sides)
        if sd_sides & trend_sides:
            setup_family = "sd_touch"
            family_score = 14.0
            family_reason = "quality + regime/structure context + 15m/5m SD touch"
        elif day_gain_triggered or rvol_20 >= 3.0:
            setup_family = "hot_momentum"
            family_score = 18.0
            family_reason = "quality + regime/structure context + 15m/5m hot trigger"
        else:
            setup_family = "trend_follow"
            family_score = 12.0
            family_reason = "quality + regime/structure context + 15m/5m trend trigger"

    soft_missing = []
    if not long_cycle_sides:
        soft_missing.append("1d/4h_regime")
    if not structure_sides:
        soft_missing.append("1h/30m_structure")
    if setup_family and soft_missing:
        family_reason = f"{family_reason}; soft_missing={','.join(soft_missing)}"

    signal_pressure_passed = bool(pressure["signal_pressure_passed"])
    context_gate_passed = bool(quality_gate_passed and signal_pressure_passed and allowed_sides and setup_family)
    missing = []
    if not quality_gate_passed:
        missing.append("quality")
    if not signal_pressure_passed:
        missing.append("signal_pressure")
    if not allowed_sides and not missing:
        missing.append("timeframe_alignment")
    context_score = family_score + len(long_cycle_sides) * 2.0 + len(structure_sides) * 1.5 + len(trigger_sides)
    return {
        "context_gate_passed": context_gate_passed,
        "signal_window_gate_passed": context_gate_passed,
        "signal_pressure_passed": signal_pressure_passed,
        "signal_pressure_keys": list(pressure["signal_pressure_keys"]),
        "signal_pressure_sides": sorted(trigger_sides),
        "admission_trigger_family": setup_family or "none",
        "admission_gate_version": "signal_window_v1",
        "setup_family": setup_family or "none",
        "allowed_sides": allowed_sides,
        "context_score": round(context_score if context_gate_passed else 0.0, 3),
        "context_reason": family_reason if context_gate_passed else f"context_missing={','.join(missing)}",
        "context_tiers": {
            "long_cycle_timeframes": [tf for tf in _LONG_CYCLE_TIMEFRAMES if tf in snapshots],
            "structure_timeframes": [tf for tf in _STRUCTURE_TIMEFRAMES if tf in snapshots],
            "trigger_timeframes": [tf for tf in _TRIGGER_TIMEFRAMES if tf in snapshots],
            "long_cycle_sides": sorted(long_cycle_sides),
            "structure_sides": sorted(structure_sides),
            "trigger_sides": sorted(trigger_sides),
            "context_trigger_sides": sorted(context_trigger_sides),
            "trend_sides": sorted(trend_sides),
            "reversal_sides": sorted(reversal_sides),
            "soft_missing": soft_missing,
        },
        "signal_pressure_by_timeframe": pressure["signal_pressure_by_timeframe"],
    }


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

        metric_row = dict(metrics or {})
        day_change_pct = _safe_float(metric_row.get("day_change_pct"))
        day_gain_trigger_enabled = normalize_admission_bool(
            settings.get("day_gain_trigger_enabled"),
            True,
        )
        day_gain_trigger_pct = max(
            0.0,
            _safe_float(settings.get("day_gain_trigger_pct"), DEFAULT_DAY_GAIN_TRIGGER_PCT),
        )
        day_gain_triggered = bool(
            day_gain_trigger_enabled
            and day_gain_trigger_pct > 0
            and day_change_pct >= day_gain_trigger_pct
        )
        if day_gain_triggered:
            technical_score += DAILY_SCAN_SECONDARY_WEIGHT
            technical_reasons.append(f"day_gain>={_format_threshold(day_gain_trigger_pct)}%")

        if long_votes == short_votes:
            direction_bias = "neutral"
        elif long_votes > short_votes:
            direction_bias = "long"
        else:
            direction_bias = "short"

        if direction_bias == "neutral" and not day_gain_triggered:
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
        avg_10d_volume = _safe_float(metric_row.get("avg_10d_volume"))
        premarket_volume = _safe_float(metric_row.get("premarket_volume"))
        atr_pct = abs(_safe_float(metric_row.get("atr_pct")))

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
            "setup_type": "day_gain_watch" if day_gain_triggered else "fixed_quality_gates",
            "allowed_sides": [direction_bias] if direction_bias in {"long", "short"} else ["long", "short"],
            "signal_confirmation": "standard",
            "entry_style": "wait_for_pullback_or_exhaustion" if day_gain_triggered else "standard",
            "position_size_multiplier": 1.0,
            "avoid_new_entries": False,
        }
        failed_gates = []
        rejection_examples = []
        gate_reasons = []
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

        if day_gain_triggered:
            allowed_sides = strategy_policy.get("allowed_sides")
            if not isinstance(allowed_sides, list) or not allowed_sides:
                allowed_sides = ["long", "short"]
            if direction_bias == "neutral":
                allowed_sides = ["long", "short"]
            strategy_policy.update(
                {
                    "selection_trigger": "day_gain",
                    "setup_type": (
                        "day_gain_watch"
                        if direction_bias == "neutral"
                        else str(strategy_policy.get("setup_type") or "day_gain_watch")
                    ),
                    "allowed_sides": allowed_sides,
                    "entry_style": "wait_for_pullback_or_exhaustion",
                    "signal_confirmation": (
                        str(strategy_policy.get("signal_confirmation") or "standard").strip()
                        or "standard"
                    ),
                    "recommended_signal_profile": str(
                        strategy_policy.get("recommended_signal_profile") or "intraday_sd_v1"
                    ).strip() or "intraday_sd_v1",
                }
            )

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
        trigger_timeline = build_daily_scan_trigger_timeline(
            metric_row=metric_row,
            snapshots=snapshots,
            day_gain_triggered=day_gain_triggered,
            day_gain_trigger_pct=day_gain_trigger_pct,
        )
        context_gate = _build_context_gate(
            snapshots=snapshots,
            metric_row=metric_row,
            direction_bias=direction_bias,
            quality_gate_passed=quality_gate_passed,
            day_gain_triggered=day_gain_triggered,
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
            "reason_tags": gate_reasons,
            "context_gate_passed": context_gate["context_gate_passed"],
            "setup_family": context_gate["setup_family"],
            "allowed_sides": context_gate["allowed_sides"],
            "context_score": context_gate["context_score"],
            "context_reason": context_gate["context_reason"],
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
                "day_gain_triggered": day_gain_triggered,
                "day_gain_trigger_pct": day_gain_trigger_pct,
                "selection_triggers": ["day_gain"] if day_gain_triggered else [],
                "rank_bonus": rank_bonus,
                "dynamic_admission_enabled": dynamic_enabled,
                "admission_score": round(admission_score, 3),
                "admission_score_components": admission_score_components,
                "admission_bonus": round(admission_bonus, 3),
                "symbol_profile": symbol_profile,
                "dynamic_thresholds": dynamic_thresholds,
                "failed_gates": failed_gates,
                "strategy_policy": strategy_policy,
                "reason_tags": gate_reasons,
                "trigger_timeline": trigger_timeline,
                **context_gate,
                **stocks_in_play_details,
            },
        }

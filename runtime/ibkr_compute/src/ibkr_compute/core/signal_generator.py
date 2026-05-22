"""
信号生成器 — MR窗口状态机 + 信号组装

移植 Pine Script Signal_Alert_Core[Glory] 中的完整信号逻辑:
  1. SD通道触及 → 开启 MR 窗口
  2. 窗口内收集组件: EMA触及 + Fractal + 背离
  3. 反向信号重置
  4. 信号组装 (4种类型) + 过滤
  5. 消费/清除逻辑
  6. 仓位计算
"""

import logging
from copy import deepcopy
from datetime import datetime

from .exit_policy import apply_exit_policy_to_position
from .indicator_engine import params_for_interval
from .position_sizing import calc_marketable_limit_position
from .setup_registry import build_setup_metadata
from .time_utils import ET

logger = logging.getLogger(__name__)
DEFAULT_MARKET_MONITOR_SYMBOLS = ""


def _param_bool(value, default: bool = False) -> bool:
    if value is None or value == "":
        return bool(default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


class SignalGenerator:
    def __init__(self, symbol: str, interval: str, params: dict = None):
        self.symbol = symbol
        self.interval = interval
        self.params = {}
        self.market_monitor_symbols = set()
        self.signal_enabled_symbols = set()
        self.symbol_is_market_monitor = False
        self.set_params(params)

        # MR 窗口状态
        self.sd_lower_mr_active = False
        self.sd_upper_mr_active = False
        self.sd_lower_mr_used = False
        self.sd_upper_mr_used = False
        self.sd_lower_touched = False
        self.sd_upper_touched = False
        self._bar_index = 0
        self._sd_lower_activated_bar = 0
        self._sd_upper_activated_bar = 0

        # 上轨窗口组件
        self.sd_upper_bull_touch_seen = False
        self.sd_upper_bull_touch_line = ""
        self.sd_upper_bull_fractal_seen = False
        self.sd_upper_bear_fractal_seen = False

        # 下轨窗口组件
        self.sd_lower_bull_fractal_seen = False
        self.sd_lower_bear_touch_seen = False
        self.sd_lower_bear_touch_line = ""
        self.sd_lower_bear_fractal_seen = False

        # 背离收集
        self.bull_crsi_div_seen = False
        self.bear_crsi_div_seen = False
        self.bull_obv_div_seen = False
        self.bear_obv_div_seen = False

        # 信号消费
        self.buy_consumed = False
        self.sell_consumed = False

        # 前一根 bar 的 SD 触及状态 (用于边缘检测)
        self._prev_sd_lower = False
        self._prev_sd_upper = False

        # 前一根信号状态 (去重)
        self._prev_buy_signal = False
        self._prev_sell_signal = False
        self._prev_intraday_raw_key = ""
        self._recent_squeeze_bars = 0
        self._intraday_last_signal_day_by_key: dict[str, str] = {}
        self._intraday_last_signal_bar_by_key: dict[str, int] = {}
        self.last_trace = self._empty_trace()

    def set_params(self, params: dict = None) -> None:
        self.params = params_for_interval(params or {}, self.interval)
        raw_market_monitor_symbols = (
            self.params.get("market_monitor_symbols")
            or DEFAULT_MARKET_MONITOR_SYMBOLS
        )
        self.market_monitor_symbols = {
            str(item or "").strip().upper()
            for item in str(raw_market_monitor_symbols or DEFAULT_MARKET_MONITOR_SYMBOLS).split(",")
            if str(item or "").strip()
        }
        raw_signal_enabled_symbols = self.params.get("signal_enabled_symbols") or ""
        self.signal_enabled_symbols = {
            str(item or "").strip().upper()
            for item in str(raw_signal_enabled_symbols).split(",")
            if str(item or "").strip()
        }
        symbol_text = str(self.symbol or "").strip().upper()
        allow_market_monitor_signals = _param_bool(self.params.get("allow_market_monitor_signals"), False)
        self.symbol_is_market_monitor = symbol_text in self.market_monitor_symbols and not allow_market_monitor_signals
        self.strategy_profile = str(
            self.params.get("ibkr_signal_strategy_profile")
            or self.params.get("signal_strategy_profile")
            or "legacy"
        ).strip().lower()

    def _is_intraday_sd_v1(self) -> bool:
        return self.strategy_profile == "intraday_sd_v1"

    def _legacy_signals_enabled(self) -> bool:
        if not self._is_intraday_sd_v1():
            return True
        if "intraday_include_legacy_signals" not in self.params:
            return True
        return self._intraday_param_bool("intraday_include_legacy_signals", True)

    def update(self, snapshot: dict) -> dict:
        """根据最新指标快照更新 MR 窗口状态, 检测信号。
        返回 None (无信号) 或 signal dict。
        """
        if not snapshot:
            self.last_trace = self._empty_trace()
            return None
        self._bar_index += 1
        if self.symbol_is_market_monitor:
            self.last_trace = self._build_trace_payload(
                snapshot,
                stage="none",
                filters=["市场监控标的不生成信号"],
            )
            return None

        sd_lower = snapshot.get("sd_lower", False)
        sd_upper = snapshot.get("sd_upper", False)
        events: list[str] = []

        # ── 1. SD 窗口激活 (新触及边缘检测) ──
        if sd_lower and not self._prev_sd_lower:
            self._activate_window("lower")
            events.append("SD下轨触发，开启下轨窗口")
        if sd_upper and not self._prev_sd_upper:
            self._activate_window("upper")
            events.append("SD上轨触发，开启上轨窗口")
        self._prev_sd_lower = sd_lower
        self._prev_sd_upper = sd_upper
        self._expire_stale_windows(events)

        # 窗口有效性 (上一根 bar 激活 + 未消费)
        sd_lower_valid = self.sd_lower_mr_active and not self.sd_lower_mr_used
        sd_upper_valid = self.sd_upper_mr_active and not self.sd_upper_mr_used

        # ── 2. 组件收集 ──
        ema_bull_touch = snapshot.get("ema_bull_touch", False)
        ema_bear_touch = snapshot.get("ema_bear_touch", False)
        fractal_bull = snapshot.get("fractal_bull", False)
        fractal_bear = snapshot.get("fractal_bear", False)
        crsi_bull_div = snapshot.get("crsi_bull_div", False) or snapshot.get("crsi_hid_bull", False)
        crsi_bear_div = snapshot.get("crsi_bear_div", False) or snapshot.get("crsi_hid_bear", False)
        obv_bull_div = snapshot.get("obv_bull_div", False) or snapshot.get("obv_hid_bull", False)
        obv_bear_div = snapshot.get("obv_bear_div", False) or snapshot.get("obv_hid_bear", False)

        # EMA 触及收集
        if sd_upper_valid and ema_bull_touch:
            self.sd_upper_bull_touch_seen = True
            self.sd_upper_bull_touch_line = self._resolve_touch_line(snapshot, "bull")
            events.append(f"上轨窗口记录 EMA 多头触及[{self.sd_upper_bull_touch_line}]")
        if sd_lower_valid and ema_bear_touch:
            self.sd_lower_bear_touch_seen = True
            self.sd_lower_bear_touch_line = self._resolve_touch_line(snapshot, "bear")
            events.append(f"下轨窗口记录 EMA 空头触及[{self.sd_lower_bear_touch_line}]")

        # Fractal 收集
        if sd_upper_valid and fractal_bull:
            self.sd_upper_bull_fractal_seen = True
            events.append("上轨窗口记录多头分形")
        if sd_lower_valid and fractal_bull:
            self.sd_lower_bull_fractal_seen = True
            events.append("下轨窗口记录多头分形")
        if sd_upper_valid and fractal_bear:
            self.sd_upper_bear_fractal_seen = True
            events.append("上轨窗口记录空头分形")
        if sd_lower_valid and fractal_bear:
            self.sd_lower_bear_fractal_seen = True
            events.append("下轨窗口记录空头分形")

        # 背离收集 (任一窗口有效时)
        if sd_upper_valid or sd_lower_valid:
            if crsi_bull_div:
                self.bull_crsi_div_seen = True
                events.append("记录 cRSI 多头背离")
            if crsi_bear_div:
                self.bear_crsi_div_seen = True
                events.append("记录 cRSI 空头背离")
            if obv_bull_div:
                self.bull_obv_div_seen = True
                events.append("记录 OBV 多头背离")
            if obv_bear_div:
                self.bear_obv_div_seen = True
                events.append("记录 OBV 空头背离")

        # ── 3. 反向信号重置 ──
        bull_components_exist = (
            self.sd_upper_bull_touch_seen or self.sd_upper_bull_fractal_seen
            or self.sd_lower_bull_fractal_seen
            or self.bull_crsi_div_seen or self.bull_obv_div_seen
        )
        bear_components_exist = (
            self.sd_lower_bear_touch_seen or self.sd_lower_bear_fractal_seen
            or self.sd_upper_bear_fractal_seen
            or self.bear_crsi_div_seen or self.bear_obv_div_seen
        )

        if bull_components_exist and (fractal_bear or crsi_bear_div or obv_bear_div):
            self._clear_bull_components()
            events.append("出现反向空头组件，清空多头窗口组件")
        if bear_components_exist and (fractal_bull or crsi_bull_div or obv_bull_div):
            self._clear_bear_components()
            events.append("出现反向多头组件，清空空头窗口组件")

        # ── 4. 信号组装 ──
        bull_div_seen = self.bull_crsi_div_seen or self.bull_obv_div_seen
        bear_div_seen = self.bear_crsi_div_seen or self.bear_obv_div_seen

        # 做多: 类型1 (sdUpper+EMA触及+Fractal↑+背离↑) | 类型2 (sdLower+Fractal↑+背离↑)
        buy_component = (
            (sd_upper_valid and self.sd_upper_bull_touch_seen
             and self.sd_upper_bull_fractal_seen and bull_div_seen)
            or (sd_lower_valid and self.sd_lower_bull_fractal_seen and bull_div_seen)
        )

        # 做空: 类型3 (sdUpper+Fractal↓+背离↓) | 类型4 (sdLower+EMA触及+Fractal↓+背离↓)
        sell_component = (
            (sd_upper_valid and self.sd_upper_bear_fractal_seen and bear_div_seen)
            or (sd_lower_valid and self.sd_lower_bear_touch_seen
                and self.sd_lower_bear_fractal_seen and bear_div_seen)
        )

        buy_raw = buy_component and not self.buy_consumed
        sell_raw = sell_component and not self.sell_consumed

        # ── 5. 过滤 ──
        block_mr_long = snapshot.get("block_mr_long", False)
        block_mr_short = snapshot.get("block_mr_short", False)
        block_ema_trend = snapshot.get("block_ema_trend", False)
        block_all = snapshot.get("block_all_signals", False)

        should_filter_buy, filter_reason_buy = self._check_buy_filter(
            snapshot, sd_upper_valid, sd_lower_valid, bull_div_seen,
            block_mr_long, block_ema_trend, block_all,
        )
        should_filter_sell, filter_reason_sell = self._check_sell_filter(
            snapshot, sd_upper_valid, sd_lower_valid, bear_div_seen,
            block_mr_short, block_ema_trend, block_all,
        )

        legacy_signals_enabled = self._legacy_signals_enabled()

        # Track raw legacy state for backwards-compatible trace/debounce behavior.
        self._prev_buy_signal = buy_raw
        self._prev_sell_signal = sell_raw

        filters = self._build_filter_labels(
            block_mr_long=block_mr_long,
            block_mr_short=block_mr_short,
            block_ema_trend=block_ema_trend,
            block_all=block_all,
            filter_reason_buy=filter_reason_buy,
            filter_reason_sell=filter_reason_sell,
        )
        setup_state = self._build_intraday_setup_state(snapshot, block_all=block_all)
        legacy_candidates = self._build_legacy_sd_candidates(
            legacy_signals_enabled=legacy_signals_enabled,
            buy_raw=buy_raw,
            sell_raw=sell_raw,
            should_filter_buy=should_filter_buy,
            should_filter_sell=should_filter_sell,
            filter_reason_buy=filter_reason_buy,
            filter_reason_sell=filter_reason_sell,
            sd_upper_valid=sd_upper_valid,
            sd_lower_valid=sd_lower_valid,
        )
        if legacy_candidates:
            setup_state.setdefault("candidates", [])
            setup_state["candidates"].extend(legacy_candidates)
        selection = self._select_signal_candidate(setup_state.get("candidates") or [])
        selected_candidate = selection.get("selected")
        if selected_candidate:
            setup_state["selected"] = selected_candidate
            setup_state["selected_setup"] = selected_candidate.get("setup", "")
            setup_state["selected_direction"] = selected_candidate.get("direction", "")
        elif selection.get("ambiguous"):
            setup_state.pop("selected", None)
            setup_state["selected_setup"] = ""
            setup_state["selected_direction"] = ""
            setup_state["ambiguous_opposite_directions"] = True
            setup_state["ambiguous_setups"] = list(selection.get("ambiguous_setups") or [])

        # ── 6. 生成信号预览 ──
        signal = None
        stage = "none"
        preview_signal = None
        if selection.get("ambiguous"):
            stage = "blocked"
            events.append("多空候选同时触发，阻止本根K线信号")
            self._prev_intraday_raw_key = ""
        elif selected_candidate:
            candidate_day = self._snapshot_market_date(snapshot) or ""
            raw_key = (
                f"{candidate_day}:"
                f"{selected_candidate.get('source', '')}:"
                f"{selected_candidate.get('direction', '')}:"
                f"{selected_candidate.get('setup', '')}"
            )
            candidate_once = raw_key != self._prev_intraday_raw_key
            preview_signal = self._build_candidate_signal(
                snapshot,
                selected_candidate,
                sd_upper_valid=sd_upper_valid,
                sd_lower_valid=sd_lower_valid,
            )
            quality_score = self._signal_quality_score(preview_signal)
            quality_min = self._intraday_min_signal_quality_score()
            if selected_candidate.get("filters_pass") and quality_min > 0 and quality_score < quality_min:
                stage = "blocked"
                selected_candidate["filters_pass"] = False
                selected_candidate["filter_reason"] = "signal_quality_below_threshold"
                selected_candidate.setdefault("filter_checks", {})
                selected_candidate["filter_checks"]["signal_quality_min"] = False
                self._prev_intraday_raw_key = ""
            elif selected_candidate.get("filters_pass"):
                self._prev_intraday_raw_key = raw_key
                stage = "confirmed" if candidate_once else "candidate"
                if candidate_once:
                    signal = preview_signal
                    self._mark_intraday_candidate_confirmed(selected_candidate, snapshot)
                    events.append(f"确认 {selected_candidate.get('setup')} setup")
            else:
                stage = "blocked"
                self._prev_intraday_raw_key = ""
        else:
            self._prev_intraday_raw_key = ""

        trace_component_flags = {
            "sd_upper_bull_touch_seen": self.sd_upper_bull_touch_seen,
            "sd_upper_bull_fractal_seen": self.sd_upper_bull_fractal_seen,
            "sd_upper_bear_fractal_seen": self.sd_upper_bear_fractal_seen,
            "sd_lower_bull_fractal_seen": self.sd_lower_bull_fractal_seen,
            "sd_lower_bear_touch_seen": self.sd_lower_bear_touch_seen,
            "sd_lower_bear_fractal_seen": self.sd_lower_bear_fractal_seen,
            "bull_crsi_div_seen": self.bull_crsi_div_seen,
            "bear_crsi_div_seen": self.bear_crsi_div_seen,
            "bull_obv_div_seen": self.bull_obv_div_seen,
            "bear_obv_div_seen": self.bear_obv_div_seen,
            "buy_raw": buy_raw,
            "sell_raw": sell_raw,
            "legacy_signals_enabled": legacy_signals_enabled,
        }

        # ── 7. 窗口消费 ──
        legacy_buy_confirmed = bool(
            signal
            and selected_candidate
            and selected_candidate.get("source") == "legacy_sd"
            and selected_candidate.get("direction") == "long"
        )
        legacy_sell_confirmed = bool(
            signal
            and selected_candidate
            and selected_candidate.get("source") == "legacy_sd"
            and selected_candidate.get("direction") == "short"
        )
        if legacy_buy_confirmed:
            if sd_upper_valid and self.sd_upper_bull_touch_seen and self.sd_upper_bull_fractal_seen and bull_div_seen:
                self.sd_upper_mr_used = True
            if sd_lower_valid and self.sd_lower_bull_fractal_seen and bull_div_seen:
                self.sd_lower_mr_used = True
        if legacy_sell_confirmed:
            if sd_upper_valid and self.sd_upper_bear_fractal_seen and bear_div_seen:
                self.sd_upper_mr_used = True
            if sd_lower_valid and self.sd_lower_bear_touch_seen and self.sd_lower_bear_fractal_seen and bear_div_seen:
                self.sd_lower_mr_used = True

        # ── 8. 组件清除 ──
        if legacy_signals_enabled and buy_raw and should_filter_buy:
            self._clear_bull_components()
            events.append(f"多头候选被过滤: {filter_reason_buy or '未知原因'}")
        if legacy_signals_enabled and sell_raw and should_filter_sell:
            self._clear_bear_components()
            events.append(f"空头候选被过滤: {filter_reason_sell or '未知原因'}")
        if legacy_buy_confirmed:
            self.buy_consumed = True
            self._clear_bull_components()
            events.append("多头信号确认并消费窗口")
        if legacy_sell_confirmed:
            self.sell_consumed = True
            self._clear_bear_components()
            events.append("空头信号确认并消费窗口")

        trace_filter_reason = ""
        if selection.get("ambiguous"):
            trace_filter_reason = "ambiguous_opposite_directions"
        elif stage == "blocked" and preview_signal and preview_signal.get("direction") == "long":
            trace_filter_reason = filter_reason_buy
        elif stage == "blocked" and preview_signal and preview_signal.get("direction") == "short":
            trace_filter_reason = filter_reason_sell
        if stage == "blocked" and selected_candidate:
            failed = [
                name for name, passed in dict(selected_candidate.get("filter_checks") or {}).items()
                if not passed
            ]
            trace_filter_reason = (
                str(selected_candidate.get("filter_reason") or "")
                or ",".join(failed)
                or trace_filter_reason
            )

        self.last_trace = self._build_trace_payload(
            snapshot,
            stage=stage,
            signal=preview_signal,
            filter_reason=trace_filter_reason,
            events=events,
            filters=filters,
            window_flags={
                "sd_upper_valid": sd_upper_valid,
                "sd_lower_valid": sd_lower_valid,
                "sd_upper_active": self.sd_upper_mr_active,
                "sd_lower_active": self.sd_lower_mr_active,
                "sd_upper_used": self.sd_upper_mr_used,
                "sd_lower_used": self.sd_lower_mr_used,
                "sd_upper_age_bars": self._window_age_bars("upper") if self.sd_upper_mr_active else 0,
                "sd_lower_age_bars": self._window_age_bars("lower") if self.sd_lower_mr_active else 0,
            },
            component_flags=trace_component_flags,
            setup_state=setup_state,
        )

        return signal

    # ── 窗口激活 ──

    def _activate_window(self, side: str):
        """SD 触及 → 开启窗口, 重置所有组件"""
        if side == "lower":
            self.sd_lower_mr_active = True
            self.sd_lower_mr_used = False
            self.sd_lower_touched = True
            self._sd_lower_activated_bar = self._bar_index
        else:
            self.sd_upper_mr_active = True
            self.sd_upper_mr_used = False
            self.sd_upper_touched = True
            self._sd_upper_activated_bar = self._bar_index

        self._clear_bull_components()
        self._clear_bear_components()
        self.buy_consumed = False
        self.sell_consumed = False

    def _window_age_bars(self, side: str) -> int:
        activated = self._sd_lower_activated_bar if side == "lower" else self._sd_upper_activated_bar
        if activated <= 0:
            return 0
        return max(0, self._bar_index - activated)

    def _signal_window_max_bars(self) -> int:
        try:
            return max(0, int(self.params.get("signal_window_max_bars", 12) or 0))
        except Exception:
            return 12

    def _expire_stale_windows(self, events: list[str]):
        max_bars = self._signal_window_max_bars()
        if max_bars <= 0:
            return
        expired = []
        if self.sd_lower_mr_active and not self.sd_lower_mr_used and self._window_age_bars("lower") > max_bars:
            self.sd_lower_mr_active = False
            self.sd_lower_mr_used = False
            self._sd_lower_activated_bar = 0
            expired.append("下轨")
        if self.sd_upper_mr_active and not self.sd_upper_mr_used and self._window_age_bars("upper") > max_bars:
            self.sd_upper_mr_active = False
            self.sd_upper_mr_used = False
            self._sd_upper_activated_bar = 0
            expired.append("上轨")
        if expired:
            self._clear_bull_components()
            self._clear_bear_components()
            self.buy_consumed = False
            self.sell_consumed = False
            events.append(f"SD{'/'.join(expired)}窗口超过{max_bars}根K线，清空陈旧组件")

    # ── 组件清除 ──

    def _clear_bull_components(self):
        self.sd_upper_bull_touch_seen = False
        self.sd_upper_bull_touch_line = ""
        self.sd_upper_bull_fractal_seen = False
        self.sd_lower_bull_fractal_seen = False
        self.bull_crsi_div_seen = False
        self.bull_obv_div_seen = False

    def _clear_bear_components(self):
        self.sd_upper_bear_fractal_seen = False
        self.sd_lower_bear_touch_seen = False
        self.sd_lower_bear_touch_line = ""
        self.sd_lower_bear_fractal_seen = False
        self.bear_crsi_div_seen = False
        self.bear_obv_div_seen = False

    # ── 过滤判断 ──

    def _check_buy_filter(self, snap, sd_upper_valid, sd_lower_valid, bull_div,
                          block_mr_long, block_ema_trend, block_all):
        dtp_dir = snap.get("dtp_dir", 0)
        dtp_phase = snap.get("dtp_phase", "neutral")

        # 类型1: sdUpper+顺势做多 → blockEmaTrend
        if (sd_upper_valid and self.sd_upper_bull_touch_seen
                and self.sd_upper_bull_fractal_seen and bull_div and block_ema_trend):
            reason = "EMA平缓/DTP频繁切换"
            return True, reason

        # 类型2: sdLower+均值回归做多 → blockMrLong (DTP红初中或confirmed)
        if (sd_lower_valid and self.sd_lower_bull_fractal_seen and bull_div):
            dtp_early_bars = int(self.params.get("dtp_early_bars", 12))
            dtp_phase_bars = snap.get("dtp_phase_bars", 0)
            if dtp_dir == -1 and (dtp_phase_bars <= dtp_early_bars or dtp_phase == "confirmed"):
                return True, "DTP红初中"

        if block_all:
            return True, "震荡市"
        return False, ""

    def _check_sell_filter(self, snap, sd_upper_valid, sd_lower_valid, bear_div,
                           block_mr_short, block_ema_trend, block_all):
        dtp_dir = snap.get("dtp_dir", 0)
        dtp_phase = snap.get("dtp_phase", "neutral")

        # 类型3: sdUpper+均值回归做空 → blockMrShort (DTP蓝初中或confirmed)
        if (sd_upper_valid and self.sd_upper_bear_fractal_seen and bear_div):
            dtp_early_bars = int(self.params.get("dtp_early_bars", 12))
            dtp_phase_bars = snap.get("dtp_phase_bars", 0)
            if dtp_dir == 1 and (dtp_phase_bars <= dtp_early_bars or dtp_phase == "confirmed"):
                return True, "DTP蓝初中"

        # 类型4: sdLower+顺势做空 → blockEmaTrend
        if (sd_lower_valid and self.sd_lower_bear_touch_seen
                and self.sd_lower_bear_fractal_seen and bear_div and block_ema_trend):
            return True, "EMA平缓/DTP频繁切换"

        if block_all:
            return True, "震荡市"
        return False, ""

    # ── EMA 触及线判定 ──

    def _resolve_touch_line(self, snapshot: dict, direction: str) -> str:
        touch_key = self._resolve_touch_line_key(snapshot, direction)
        if touch_key == "fast":
            return "ema的快线"
        if touch_key == "slow":
            return "ema的慢线"
        return "ema的快线"

    def _resolve_touch_line_key(self, snapshot: dict, direction: str) -> str:
        touch_type = self.params.get("ema_touch_type", "slow")
        if direction == "bull":
            if snapshot.get("bull_touch_fast", False) and (touch_type in ("fast", "both")):
                return "fast"
            if snapshot.get("bull_touch_slow", False) and (touch_type in ("slow", "both")):
                return "slow"
        else:
            if snapshot.get("bear_touch_fast", False) and (touch_type in ("fast", "both")):
                return "fast"
            if snapshot.get("bear_touch_slow", False) and (touch_type in ("slow", "both")):
                return "slow"
        return "none"

    # ── intraday_sd_v1 setups ──

    def _build_intraday_setup_state(self, snapshot: dict, *, block_all: bool = False) -> dict:
        state = {
            "strategy_profile": self.strategy_profile,
            "enabled": self._is_intraday_sd_v1(),
            "selected_setup": "",
            "selected_direction": "",
            "candidates": [],
        }
        if not self._is_intraday_sd_v1():
            return state

        if snapshot.get("sd_squeeze_active", False):
            self._recent_squeeze_bars = 3
        else:
            self._recent_squeeze_bars = max(0, self._recent_squeeze_bars - 1)
        recent_squeeze = snapshot.get("sd_squeeze_active", False) or self._recent_squeeze_bars > 0

        close = float(snapshot.get("close", 0) or 0)
        low = float(snapshot.get("low", close) or close)
        high = float(snapshot.get("high", close) or close)
        vwap = float(snapshot.get("vwap", close) or close)
        vwap_upper1 = float(snapshot.get("vwap_upper1", vwap) or vwap)
        vwap_lower1 = float(snapshot.get("vwap_lower1", vwap) or vwap)
        vwap_pullback_tolerance = self._intraday_vwap_pullback_tolerance(snapshot, vwap)
        vwap_pullback_require_trend_walk = self._intraday_vwap_pullback_require_trend_walk()
        session_type = str(snapshot.get("session_type") or "regular").lower()
        entry_window_pass = self._intraday_entry_window_pass(snapshot)
        min_rvol_20 = self._intraday_param_float("intraday_min_rvol_20", 0.0)
        min_atr_pct = self._intraday_param_float("intraday_min_atr_pct", 0.0)
        max_atr_pct = self._intraday_param_float("intraday_max_atr_pct", 0.0)
        base_filter_checks = {
            "block_all_pass": not block_all,
            "session_regular_or_unknown": session_type in ("regular", ""),
            "intraday_entry_window": entry_window_pass,
            "rvol_20_min": self._intraday_min_pass(snapshot.get("rvol_20"), min_rvol_20),
            "atr_pct_min": self._intraday_min_pass(snapshot.get("atr_pct"), min_atr_pct),
            "atr_pct_max": self._intraday_max_pass(snapshot.get("atr_pct"), max_atr_pct),
        }

        squeeze_long_checks = {
            "sd_breakout_up": bool(snapshot.get("sd_breakout_up", False)),
            "recent_sd_squeeze": bool(recent_squeeze),
            "above_vwap": close >= vwap,
            "not_orb_breakout_down": not bool(snapshot.get("orb_breakout_down", False)),
        }
        squeeze_short_checks = {
            "sd_breakout_down": bool(snapshot.get("sd_breakout_down", False)),
            "recent_sd_squeeze": bool(recent_squeeze),
            "below_vwap": close <= vwap,
            "not_orb_breakout_up": not bool(snapshot.get("orb_breakout_up", False)),
        }
        trend_long_checks = {
            "sd_trend_walk_up": bool(snapshot.get("sd_trend_walk_up", False)),
            "vwap_bullish": bool(snapshot.get("vwap_bullish", close >= vwap)),
            "pullback_touched_vwap": low <= vwap + vwap_pullback_tolerance,
            "close_above_vwap": close >= vwap,
            "close_not_extended_above_vwap_band": close <= vwap_upper1,
            "trend_walk_regime": (
                not vwap_pullback_require_trend_walk
                or str(snapshot.get("sd_regime", "") or "").strip().lower() == "trend_walk_up"
            ),
        }
        trend_short_checks = {
            "sd_trend_walk_down": bool(snapshot.get("sd_trend_walk_down", False)),
            "vwap_bearish": not bool(snapshot.get("vwap_bullish", close > vwap)),
            "pullback_touched_vwap": high >= vwap - vwap_pullback_tolerance,
            "close_below_vwap": close <= vwap,
            "close_not_extended_below_vwap_band": close >= vwap_lower1,
            "trend_walk_regime": (
                not vwap_pullback_require_trend_walk
                or str(snapshot.get("sd_regime", "") or "").strip().lower() == "trend_walk_down"
            ),
        }

        candidates = [
            self._intraday_candidate(
                "sd_squeeze_breakout_long",
                "long",
                "breakout",
                squeeze_long_checks,
                self._intraday_filter_checks(snapshot, "long", "sd_squeeze_breakout_long", base_filter_checks),
                "SD squeeze released upward with price holding above VWAP.",
                priority=100,
            ),
            self._intraday_candidate(
                "sd_squeeze_breakout_short",
                "short",
                "breakout",
                squeeze_short_checks,
                self._intraday_filter_checks(snapshot, "short", "sd_squeeze_breakout_short", base_filter_checks),
                "SD squeeze released downward with price holding below VWAP.",
                priority=100,
            ),
            self._intraday_candidate(
                "vwap_trend_pullback_long",
                "long",
                "trend_pullback",
                trend_long_checks,
                self._intraday_filter_checks(snapshot, "long", "vwap_trend_pullback_long", base_filter_checks),
                "SD trend-walk up remains intact after a pullback into the VWAP band.",
                priority=80,
            ),
            self._intraday_candidate(
                "vwap_trend_pullback_short",
                "short",
                "trend_pullback",
                trend_short_checks,
                self._intraday_filter_checks(snapshot, "short", "vwap_trend_pullback_short", base_filter_checks),
                "SD trend-walk down remains intact after a pullback into the VWAP band.",
                priority=80,
            ),
        ]
        state["candidates"] = candidates
        selected = next((item for item in candidates if item["triggered"]), None)
        if selected:
            state["selected"] = selected
            state["selected_setup"] = selected["setup"]
            state["selected_direction"] = selected["direction"]
        return state

    @staticmethod
    def _intraday_candidate(
        setup: str,
        direction: str,
        signal_mode: str,
        trigger_checks: dict,
        filter_checks: dict,
        technical_description: str,
        priority: int = 0,
        source: str = "intraday",
        filter_reason: str = "",
    ) -> dict:
        filters_pass = all(filter_checks.values())
        setup_meta = build_setup_metadata(
            setup,
            direction=direction,
            signal_mode=signal_mode,
            setup_priority=priority,
        )
        return {
            "setup": setup,
            "direction": direction,
            "signal_mode": signal_mode,
            "setup_label": setup_meta.get("setup_label", ""),
            "setup_family": setup_meta.get("setup_family", ""),
            "exit_policy_type": setup_meta.get("exit_policy_type", ""),
            "source": source,
            "priority": int(priority or 0),
            "trigger_checks": dict(trigger_checks),
            "filter_checks": dict(filter_checks),
            "triggered": all(trigger_checks.values()),
            "filters_pass": bool(filters_pass),
            "filter_reason": str(filter_reason or ""),
            "technical_description": technical_description,
        }

    def _build_legacy_sd_candidates(
        self,
        *,
        legacy_signals_enabled: bool,
        buy_raw: bool,
        sell_raw: bool,
        should_filter_buy: bool,
        should_filter_sell: bool,
        filter_reason_buy: str,
        filter_reason_sell: str,
        sd_upper_valid: bool,
        sd_lower_valid: bool,
    ) -> list[dict]:
        if not legacy_signals_enabled:
            return []
        candidates: list[dict] = []
        if buy_raw:
            setup, signal_mode, description = self._legacy_setup_context(
                "long", sd_upper_valid, sd_lower_valid
            )
            candidates.append(
                self._intraday_candidate(
                    setup,
                    "long",
                    signal_mode,
                    {"legacy_sd_buy_raw": True},
                    {"legacy_filters_pass": not should_filter_buy},
                    description,
                    priority=60 if signal_mode.startswith("trend") else 50,
                    source="legacy_sd",
                    filter_reason=filter_reason_buy,
                )
            )
        if sell_raw:
            setup, signal_mode, description = self._legacy_setup_context(
                "short", sd_upper_valid, sd_lower_valid
            )
            candidates.append(
                self._intraday_candidate(
                    setup,
                    "short",
                    signal_mode,
                    {"legacy_sd_sell_raw": True},
                    {"legacy_filters_pass": not should_filter_sell},
                    description,
                    priority=60 if signal_mode.startswith("trend") else 50,
                    source="legacy_sd",
                    filter_reason=filter_reason_sell,
                )
            )
        return candidates

    def _legacy_setup_context(
        self,
        direction: str,
        sd_upper_valid: bool,
        sd_lower_valid: bool,
    ) -> tuple[str, str, str]:
        if direction == "long":
            if sd_upper_valid and self.sd_upper_bull_touch_seen:
                return (
                    "sd_trend_continuation_long",
                    "trend_continuation",
                    f"SD上轨→顺势做多(fractal↑+EMA-touch↑[{self.sd_upper_bull_touch_line}]+div↑)",
                )
            return "sd_mr_reversal_long", "mr_reversal", "SD下轨→均值回归做多(fractal↑+div↑)"
        if sd_lower_valid and self.sd_lower_bear_touch_seen:
            return (
                "sd_trend_continuation_short",
                "trend_continuation",
                f"SD下轨→顺势做空(fractal↓+EMA-touch↓[{self.sd_lower_bear_touch_line}]+div↓)",
            )
        return "sd_mr_reversal_short", "mr_reversal", "SD上轨→均值回归做空(fractal↓+div↓)"

    @staticmethod
    def _select_signal_candidate(candidates: list[dict]) -> dict:
        triggered = [item for item in candidates if item.get("triggered")]
        if not triggered:
            return {"selected": None, "ambiguous": False}
        viable = [item for item in triggered if item.get("filters_pass")]
        selection_pool = viable or triggered
        directions = {str(item.get("direction") or "").strip().lower() for item in selection_pool}
        directions.discard("")
        if len(directions) > 1:
            return {
                "selected": None,
                "ambiguous": True,
                "ambiguous_setups": [str(item.get("setup") or "") for item in selection_pool],
            }
        selected = max(
            enumerate(selection_pool),
            key=lambda pair: (int(pair[1].get("priority") or 0), -pair[0]),
        )[1]
        return {"selected": selected, "ambiguous": False}

    def _build_candidate_signal(
        self,
        snapshot: dict,
        candidate: dict,
        *,
        sd_upper_valid: bool,
        sd_lower_valid: bool,
    ) -> dict:
        if candidate.get("source") == "legacy_sd":
            signal = self._build_signal(
                str(candidate.get("direction") or ""),
                snapshot,
                sd_upper_valid,
                sd_lower_valid,
            )
            return self._with_candidate_setup_metadata(signal, candidate)
        return self._build_intraday_signal(snapshot, candidate)

    def _with_candidate_setup_metadata(self, signal: dict, candidate: dict) -> dict:
        if not signal:
            return signal
        extra = dict(signal.get("extra") or {})
        setup = str(extra.get("setup") or signal.get("setup") or signal.get("signal") or candidate.get("setup") or "")
        signal_mode = str(extra.get("signal_mode") or candidate.get("signal_mode") or "")
        setup_meta = build_setup_metadata(
            setup,
            fallback_signal=signal.get("signal", ""),
            direction=signal.get("direction") or candidate.get("direction") or "",
            signal_mode=signal_mode,
            strategy_profile=self.strategy_profile,
            setup_priority=candidate.get("priority"),
            exit_policy_type=extra.get("exit_policy_type") or candidate.get("exit_policy_type") or "",
        )
        extra.update(setup_meta)
        signal["extra"] = extra
        signal["setup"] = setup_meta.get("setup", setup)
        signal["setup_label"] = setup_meta.get("setup_label", "")
        signal["setup_family"] = setup_meta.get("setup_family", "")
        signal["signal_mode"] = setup_meta.get("signal_mode", signal_mode)
        return signal

    def _build_intraday_signal(self, snapshot: dict, candidate: dict) -> dict:
        direction = str(candidate.get("direction") or "")
        close = snapshot.get("close", 0)
        atr = snapshot.get("atr", 0)
        pos = calc_marketable_limit_position(close, atr, self.params, direction)
        setup = str(candidate.get("setup") or "")
        signal_mode = str(candidate.get("signal_mode") or "")
        pos, exit_meta = apply_exit_policy_to_position(
            pos,
            direction=direction,
            close=close,
            atr=atr,
            params=self.params,
            setup=setup,
            signal_mode=signal_mode,
        )
        reason = str(candidate.get("technical_description") or setup)
        setup_meta = build_setup_metadata(
            setup,
            direction=direction,
            signal_mode=signal_mode,
            strategy_profile=self.strategy_profile,
            setup_priority=candidate.get("priority"),
            exit_policy_type=exit_meta.get("exit_policy_type", ""),
        )
        extra = {
            **setup_meta,
            "sd_regime": snapshot.get("sd_regime", ""),
            "entry_order_type": "limit",
            "entry_limit_mode": pos.get("entry_limit_mode", "passive_limit_dynamic"),
            "entry_price_plan": pos.get("entry_limit_mode", "passive_limit_dynamic"),
            "entry_limit_offset": pos.get("entry_limit_offset", 0),
            "validity_minutes": self._intraday_validity_minutes(),
            "entry_window_start_time": self._intraday_entry_window_start_time(),
            "entry_window_end_time": self._intraday_entry_window_end_time(),
            "trigger_checks": dict(candidate.get("trigger_checks") or {}),
            "filter_checks": dict(candidate.get("filter_checks") or {}),
            "technical_description": reason,
            "sd_zone": self._zone_str(snapshot),
            "sd_trend": self._trend_str(snapshot.get("sd_trend", 0)),
            "signal_window": "intraday",
            "signal_mode": signal_mode,
            "atr": snapshot.get("atr", 0),
            "atr_raw": snapshot.get("atr_raw", 0),
            "atr_pct": snapshot.get("atr_pct", 0),
            "sl_dist_pct": pos.get("sl_dist_pct", 0),
            "sl_atr_ratio": pos.get("sl_atr_ratio", 0),
            "vwap": snapshot.get("vwap"),
            "rvol_20": snapshot.get("rvol_20"),
            "dollar_volume": snapshot.get("dollar_volume"),
            "source": "ibkr_compute",
            **exit_meta,
        }
        quality_meta = self._build_signal_quality_metadata(
            snapshot,
            direction=direction,
            setup=setup,
            signal_mode=signal_mode,
            candidate=candidate,
            entry=pos.get("entry", close),
        )
        extra.update(quality_meta)
        if isinstance(self.params.get("target_strategy_policy"), dict):
            extra["target_strategy_policy"] = dict(self.params.get("target_strategy_policy") or {})
        if isinstance(self.params.get("target_symbol_profile"), dict):
            extra["target_symbol_profile"] = dict(self.params.get("target_symbol_profile") or {})
        return {
            "symbol": self.symbol,
            "direction": direction,
            "signal": setup,
            "entry": round(pos["entry"], 2),
            "stop_loss": round(pos["stop_loss"], 2),
            "take_profit": round(pos["take_profit"], 2),
            "shares": pos["shares"],
            "rr": pos["rr"],
            "risk_r": pos.get("risk_r", 0),
            "initial_stop_loss": round(pos.get("initial_stop_loss", pos["stop_loss"]), 2),
            "initial_take_profit": round(pos.get("initial_take_profit", pos["take_profit"]), 2),
            "exit_policy": pos.get("exit_policy", ""),
            "reason": reason,
            "interval": self.interval,
            "extra": extra,
            "setup": setup_meta.get("setup", setup),
            "setup_label": setup_meta.get("setup_label", ""),
            "setup_family": setup_meta.get("setup_family", ""),
            "signal_mode": setup_meta.get("signal_mode", signal_mode),
            "signal_quality_score": quality_meta.get("signal_quality_score", 0),
        }

    def _intraday_validity_minutes(self) -> int:
        try:
            return max(1, int(self.params.get("intraday_signal_validity_minutes", 15) or 15))
        except Exception:
            return 15

    def _intraday_min_signal_quality_score(self) -> float:
        return self._intraday_param_float("intraday_min_signal_quality_score", 70.0)

    @staticmethod
    def _signal_quality_score(signal: dict | None) -> float:
        if not isinstance(signal, dict):
            return 0.0
        extra = signal.get("extra") if isinstance(signal.get("extra"), dict) else {}
        try:
            return float(extra.get("signal_quality_score") or signal.get("signal_quality_score") or 0.0)
        except Exception:
            return 0.0

    def _build_signal_quality_metadata(
        self,
        snapshot: dict,
        *,
        direction: str,
        setup: str,
        signal_mode: str,
        candidate: dict | None,
        entry: float,
    ) -> dict:
        close = self._intraday_value_float(snapshot.get("close"), 0.0)
        atr_pct = abs(self._intraday_value_float(snapshot.get("atr_pct"), 0.0))
        rvol_20 = self._intraday_value_float(snapshot.get("rvol_20"), 0.0)
        trigger_checks = dict((candidate or {}).get("trigger_checks") or {})
        filter_checks = dict((candidate or {}).get("filter_checks") or {})
        signal_mode_text = str(signal_mode or "").strip().lower()
        setup_text = str(setup or "").strip().lower()

        hard_pressure = 0.0
        pressure_keys: list[str] = []
        if snapshot.get("sd_lower") or snapshot.get("sd_upper") or any("sd_" in key and bool(value) for key, value in trigger_checks.items()):
            hard_pressure = 35.0
            pressure_keys.append("sd_pressure")
        if snapshot.get("crsi_os") or snapshot.get("crsi_ob"):
            hard_pressure = max(hard_pressure, 35.0)
            pressure_keys.append("crsi_pressure")
        if signal_mode_text in {"breakout", "trend_pullback", "trend_continuation", "mr_reversal"}:
            hard_pressure = max(hard_pressure, 35.0)
            pressure_keys.append(signal_mode_text)

        direction_matches_trend = self._intraday_sd_trend_matches(snapshot, direction)
        multi_timeframe = 12.0
        if direction_matches_trend:
            multi_timeframe += 5.0
        if filter_checks.get("target_direction_alignment", True):
            multi_timeframe += 3.0
        multi_timeframe = min(20.0, multi_timeframe)

        check_values = list(trigger_checks.values()) + list(filter_checks.values())
        pass_ratio = (
            sum(1 for value in check_values if bool(value)) / len(check_values)
            if check_values
            else 1.0
        )
        confirmation = 10.0 + pass_ratio * 10.0
        if snapshot.get("crsi_bull_div") or snapshot.get("crsi_bear_div") or snapshot.get("obv_bull_div") or snapshot.get("obv_bear_div"):
            confirmation += 3.0
        if snapshot.get("fractal_bull") or snapshot.get("fractal_bear"):
            confirmation += 2.0
        adx = self._intraday_value_float(snapshot.get("adx"), 0.0)
        plus_di = self._intraday_value_float(snapshot.get("plus_di"), 0.0)
        minus_di = self._intraday_value_float(snapshot.get("minus_di"), 0.0)
        if adx >= 20.0 and ((direction == "long" and plus_di >= minus_di) or (direction == "short" and minus_di >= plus_di)):
            confirmation += 5.0
        mfi = self._intraday_value_float(snapshot.get("mfi"), 50.0)
        if (direction == "long" and mfi <= 35.0) or (direction == "short" and mfi >= 65.0):
            confirmation += 3.0
        confirmation = min(25.0, confirmation)

        entry_quality = 8.0
        if close > 0 and entry > 0:
            improvement_bps = abs(close - entry) / close * 10000.0
            entry_quality = min(10.0, 5.0 + min(5.0, improvement_bps / 6.0))
        else:
            improvement_bps = 0.0

        liquidity = 6.0
        min_rvol = self._intraday_param_float("intraday_min_rvol_20", 1.2)
        if rvol_20 <= 0 or rvol_20 >= min_rvol:
            liquidity += 2.0
        min_atr = self._intraday_param_float("intraday_min_atr_pct", 0.08)
        max_atr = self._intraday_param_float("intraday_max_atr_pct", 1.20)
        if (min_atr <= 0 or atr_pct >= min_atr) and (max_atr <= 0 or atr_pct <= max_atr):
            liquidity += 2.0
        liquidity = min(10.0, liquidity)

        components = {
            "signal_pressure": round(hard_pressure, 3),
            "multi_timeframe": round(multi_timeframe, 3),
            "confirmation": round(confirmation, 3),
            "entry_quality": round(entry_quality, 3),
            "liquidity_freshness": round(liquidity, 3),
        }
        score = min(100.0, sum(components.values()))
        entry_plan = self._build_entry_plan_metadata(
            snapshot,
            direction=direction,
            setup=setup_text,
            signal_mode=signal_mode_text,
            entry=entry,
            close=close,
            quality_score=score,
            price_improvement_bps=improvement_bps,
        )
        return {
            "signal_quality_score": round(score, 3),
            "signal_quality_components": components,
            "signal_quality_min": self._intraday_min_signal_quality_score(),
            "signal_pressure_keys": list(dict.fromkeys(pressure_keys)),
            "entry_quality_score": round(entry_quality, 3),
            **entry_plan,
        }

    def _build_entry_plan_metadata(
        self,
        snapshot: dict,
        *,
        direction: str,
        setup: str,
        signal_mode: str,
        entry: float,
        close: float,
        quality_score: float,
        price_improvement_bps: float,
    ) -> dict:
        if "breakout" in setup or signal_mode == "breakout":
            anchor = "breakout_close"
            timeout_bars = 2
        elif "pullback" in setup or signal_mode in {"trend_pullback", "trend_continuation"}:
            anchor = "vwap_pullback" if self._intraday_value_float(snapshot.get("vwap"), 0.0) > 0 else "ema_pullback"
            timeout_bars = 3
        else:
            anchor = "sd_crsi_reversion"
            timeout_bars = 6
        breakout_quality_min = self._intraday_param_float("entry_breakout_marketable_quality_min", 80.0)
        entry_aggression = "marketable_limit" if signal_mode == "breakout" and quality_score >= breakout_quality_min else "passive_limit"
        return {
            "entry_plan_version": str(self.params.get("entry_plan_version") or "entry_plan_v2"),
            "entry_anchor": anchor,
            "entry_limit_price": round(float(entry or 0.0), 4),
            "entry_timeout_bars": timeout_bars,
            "reprice_policy": str(self.params.get("entry_reprice_policy") or "single_reprice_then_cancel"),
            "price_improvement_bps": round(float(price_improvement_bps or 0.0), 3),
            "entry_aggression": entry_aggression,
            "entry_reference_price": round(float(close or 0.0), 4),
        }

    def _intraday_param_float(self, key: str, default: float = 0.0) -> float:
        try:
            value = self.params.get(key, default)
            if value is None or value == "":
                return default
            return float(value)
        except Exception:
            return default

    def _intraday_param_int(self, key: str, default: int = 0) -> int:
        try:
            value = self.params.get(key, default)
            if value is None or value == "":
                return default
            return int(value)
        except Exception:
            return default

    def _intraday_param_bool(self, key: str, default: bool = False) -> bool:
        value = self.params.get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        text = str(value or "").strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("0", "false", "no", "off", ""):
            return False
        return default

    def _intraday_filter_checks(self, snapshot: dict, direction: str, setup: str, base_filter_checks: dict) -> dict:
        checks = dict(base_filter_checks)
        checks["directional_day_change_max"] = self._intraday_directional_day_change_pass(snapshot, direction)
        checks["trend_mismatch_day_change_guard"] = self._intraday_trend_mismatch_pass(snapshot, direction)
        checks["target_direction_alignment"] = self._intraday_target_direction_alignment_pass(direction)
        checks["setup_daily_limit"] = self._intraday_setup_daily_limit_pass(snapshot, setup, direction)
        checks["setup_cooldown"] = self._intraday_setup_cooldown_pass(snapshot, setup, direction)
        return checks

    def _intraday_vwap_pullback_tolerance(self, snapshot: dict, vwap: float) -> float:
        atr_basis = self._intraday_value_float(snapshot.get("atr_raw"), 0.0)
        if atr_basis <= 0:
            atr_basis = self._intraday_value_float(snapshot.get("atr"), 0.0)
        atr_mult = self._intraday_param_float("intraday_vwap_pullback_atr_mult", 0.15)
        max_bps = self._intraday_param_float("intraday_vwap_pullback_max_bps", 10.0)
        price_ref = abs(vwap) if vwap else abs(self._intraday_value_float(snapshot.get("close"), 0.0))
        candidates: list[float] = []
        if atr_basis > 0 and atr_mult > 0:
            candidates.append(atr_basis * atr_mult)
        if price_ref > 0 and max_bps > 0:
            candidates.append(price_ref * max_bps / 10000.0)
        return max(0.0, min(candidates) if candidates else 0.0)

    def _intraday_vwap_pullback_require_trend_walk(self) -> bool:
        if "intraday_vwap_pullback_require_trend_walk" in self.params:
            return self._intraday_param_bool("intraday_vwap_pullback_require_trend_walk", True)
        return self._intraday_param_bool("intraday_vwap_pullback_long_require_trend_walk", True)

    def _intraday_target_direction_alignment_pass(self, direction: str) -> bool:
        if not self._intraday_param_bool("intraday_require_target_direction_alignment", False):
            return True
        normalized_direction = str(direction or "").strip().lower()
        if normalized_direction not in {"long", "short"}:
            return False

        allowed_sides = self._intraday_allowed_target_sides()
        if allowed_sides and normalized_direction not in allowed_sides:
            return False

        target_direction = self._intraday_target_direction_bias()
        if target_direction in {"long", "short"}:
            return normalized_direction == target_direction
        return bool(allowed_sides)

    def _intraday_allowed_target_sides(self) -> set[str]:
        policy = self.params.get("target_strategy_policy")
        if not isinstance(policy, dict):
            return set()
        raw_sides = policy.get("allowed_sides")
        if not isinstance(raw_sides, (list, tuple, set)):
            return set()
        return {
            str(item or "").strip().lower()
            for item in raw_sides
            if str(item or "").strip().lower() in {"long", "short"}
        }

    def _intraday_target_direction_bias(self) -> str:
        for source in (
            self.params,
            self.params.get("target_symbol_profile") if isinstance(self.params.get("target_symbol_profile"), dict) else {},
            self.params.get("target_strategy_policy") if isinstance(self.params.get("target_strategy_policy"), dict) else {},
        ):
            if not isinstance(source, dict):
                continue
            value = str(source.get("target_direction_bias") or source.get("direction_bias") or "").strip().lower()
            if value:
                return value
        return ""

    def _intraday_setup_daily_limit_pass(self, snapshot: dict, setup: str, direction: str) -> bool:
        limit = self._intraday_param_int("intraday_setup_daily_limit", 1)
        if limit <= 0:
            return True
        day = self._snapshot_market_date(snapshot)
        if not day:
            return True
        key = self._intraday_setup_state_key(setup, direction)
        return self._intraday_last_signal_day_by_key.get(key) != day

    def _intraday_setup_cooldown_pass(self, snapshot: dict, setup: str, direction: str) -> bool:
        cooldown_bars = self._intraday_param_int("intraday_setup_cooldown_bars", 6)
        if cooldown_bars <= 0:
            return True
        key = self._intraday_setup_state_key(setup, direction)
        last_bar = int(self._intraday_last_signal_bar_by_key.get(key, 0) or 0)
        if last_bar <= 0:
            return True
        day = self._snapshot_market_date(snapshot)
        last_day = self._intraday_last_signal_day_by_key.get(key)
        if day and last_day and day != last_day:
            return True
        return self._bar_index - last_bar > cooldown_bars

    def _mark_intraday_candidate_confirmed(self, candidate: dict, snapshot: dict) -> None:
        setup = str(candidate.get("setup") or "").strip()
        direction = str(candidate.get("direction") or "").strip().lower()
        key = self._intraday_setup_state_key(setup, direction)
        if not key:
            return
        day = self._snapshot_market_date(snapshot)
        if day:
            self._intraday_last_signal_day_by_key[key] = day
        self._intraday_last_signal_bar_by_key[key] = self._bar_index

    @staticmethod
    def _intraday_setup_state_key(setup: str, direction: str) -> str:
        normalized_setup = str(setup or "").strip()
        normalized_direction = str(direction or "").strip().lower()
        if not normalized_setup or normalized_direction not in {"long", "short"}:
            return ""
        return f"{normalized_direction}:{normalized_setup}"

    def _intraday_directional_day_change_pass(self, snapshot: dict, direction: str) -> bool:
        threshold = self._intraday_param_float("intraday_max_directional_day_change_pct", 0.0)
        if threshold <= 0:
            return True
        day_change_pct = self._intraday_value_float(snapshot.get("day_change_pct"), 0.0)
        directional_change = day_change_pct if direction == "long" else -day_change_pct
        return directional_change <= threshold

    def _intraday_trend_mismatch_pass(self, snapshot: dict, direction: str) -> bool:
        threshold = self._intraday_param_float("intraday_trend_mismatch_max_abs_day_change_pct", 0.0)
        if threshold <= 0:
            return True
        if self._intraday_sd_trend_matches(snapshot, direction):
            return True
        day_change_pct = abs(self._intraday_value_float(snapshot.get("day_change_pct"), 0.0))
        return day_change_pct < threshold

    @staticmethod
    def _intraday_value_float(value, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _intraday_sd_trend_matches(snapshot: dict, direction: str) -> bool:
        trend = snapshot.get("sd_trend", 0)
        if isinstance(trend, str):
            text = trend.strip().lower()
            if direction == "long":
                return text in ("1", "up", "bull", "bullish")
            return text in ("-1", "down", "bear", "bearish")
        try:
            trend_value = int(float(trend))
        except (TypeError, ValueError):
            trend_value = 0
        return trend_value > 0 if direction == "long" else trend_value < 0

    @staticmethod
    def _intraday_min_pass(value, threshold: float) -> bool:
        if threshold <= 0:
            return True
        try:
            return float(value) >= threshold
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _intraday_max_pass(value, threshold: float) -> bool:
        if threshold <= 0:
            return True
        try:
            return float(value) <= threshold
        except (TypeError, ValueError):
            return False

    def _intraday_entry_window_start_time(self) -> str:
        return self._normalize_hhmm_text(self.params.get("intraday_entry_window_start_time"), "09:35")

    def _intraday_entry_window_end_time(self) -> str:
        return self._normalize_hhmm_text(self.params.get("intraday_entry_window_end_time"), "10:30")

    def _intraday_entry_window_pass(self, snapshot: dict) -> bool:
        current = self._snapshot_hhmm_tuple(snapshot)
        if current is None:
            return True
        start = self._parse_hhmm_tuple(self._intraday_entry_window_start_time(), "09:35")
        end = self._parse_hhmm_tuple(self._intraday_entry_window_end_time(), "10:30")
        return start <= current <= end

    @staticmethod
    def _normalize_hhmm_text(value, default: str) -> str:
        hour, minute = SignalGenerator._parse_hhmm_tuple(value, default)
        return f"{hour:02d}:{minute:02d}"

    @staticmethod
    def _parse_hhmm_tuple(value, default: str) -> tuple[int, int]:
        text = str(value or default or "00:00").strip()
        try:
            hour_text, minute_text = text.split(":", 1)
            hour = int(hour_text)
            minute = int(minute_text)
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return hour, minute
        except Exception:
            pass
        fallback_hour, fallback_minute = str(default or "00:00").split(":", 1)
        return int(fallback_hour), int(fallback_minute)

    @staticmethod
    def _snapshot_hhmm_tuple(snapshot: dict) -> tuple[int, int] | None:
        us_time = str(snapshot.get("us_time", "") or "").strip()
        if len(us_time) >= 16:
            try:
                return int(us_time[11:13]), int(us_time[14:16])
            except Exception:
                pass
        bar_time_ms = int(snapshot.get("bar_time_ms", 0) or 0)
        if bar_time_ms > 0:
            try:
                et_time = datetime.fromtimestamp(bar_time_ms / 1000.0, ET)
                return et_time.hour, et_time.minute
            except (OSError, TypeError, ValueError):
                return None
        return None

    @staticmethod
    def _snapshot_market_date(snapshot: dict) -> str:
        us_time = str(snapshot.get("us_time", "") or "").strip()
        if len(us_time) >= 10:
            return us_time[:10]
        bar_time_ms = int(snapshot.get("bar_time_ms", 0) or 0)
        if bar_time_ms > 0:
            try:
                return datetime.fromtimestamp(bar_time_ms / 1000.0, ET).strftime("%Y-%m-%d")
            except (OSError, TypeError, ValueError):
                return ""
        return ""

    # ── 信号构建 ──

    def _build_signal(self, direction: str, snapshot: dict,
                      sd_upper_valid: bool, sd_lower_valid: bool) -> dict:
        close = snapshot.get("close", 0)
        atr = snapshot.get("atr", 0)
        signal_window = "sd_upper"
        signal_mode = "mr"
        ema_touch_line_key = "none"

        if direction == "long":
            pos = calc_marketable_limit_position(close, atr, self.params, direction)
            if sd_upper_valid and self.sd_upper_bull_touch_seen:
                signal_type = "sd_trend_continuation_long"
                signal_window = "sd_upper"
                signal_mode = "trend_continuation"
                ema_touch_line_key = self._resolve_touch_line_key(snapshot, "bull")
                reason = f"SD上轨→顺势做多(fractal↑+EMA-touch↑[{self.sd_upper_bull_touch_line}]+div↑)"
            else:
                signal_type = "sd_mr_reversal_long"
                signal_window = "sd_lower"
                signal_mode = "mr_reversal"
                reason = "SD下轨→均值回归做多(fractal↑+div↑)"
        else:
            pos = calc_marketable_limit_position(close, atr, self.params, direction)
            if sd_lower_valid and self.sd_lower_bear_touch_seen:
                signal_type = "sd_trend_continuation_short"
                signal_window = "sd_lower"
                signal_mode = "trend_continuation"
                ema_touch_line_key = self._resolve_touch_line_key(snapshot, "bear")
                reason = f"SD下轨→顺势做空(fractal↓+EMA-touch↓[{self.sd_lower_bear_touch_line}]+div↓)"
            else:
                signal_type = "sd_mr_reversal_short"
                signal_window = "sd_upper"
                signal_mode = "mr_reversal"
                reason = "SD上轨→均值回归做空(fractal↓+div↓)"

        div_source = self._get_div_source(direction)
        if div_source:
            reason = reason.replace("div↑", f"div↑[{div_source}]").replace("div↓", f"div↓[{div_source}]")

        pos, exit_meta = apply_exit_policy_to_position(
            pos,
            direction=direction,
            close=close,
            atr=atr,
            params=self.params,
            setup=signal_type,
            signal_mode=signal_mode,
        )

        setup_meta = build_setup_metadata(
            signal_type,
            direction=direction,
            signal_mode=signal_mode,
            strategy_profile=self.strategy_profile,
            exit_policy_type=exit_meta.get("exit_policy_type", ""),
        )
        extra = {
            **setup_meta,
            "sd_zone": self._zone_str(snapshot),
            "sd_trend": self._trend_str(snapshot.get("sd_trend", 0)),
            "signal_window": signal_window,
            "signal_mode": signal_mode,
            "ema_touch_line": ema_touch_line_key,
            "div_source": "both" if div_source == "cRSI+OBV" else ("crsi" if div_source == "cRSI" else ("obv" if div_source == "OBV" else "none")),
            "dtp_dir": self._dtp_str(snapshot.get("dtp_dir", 0)),
            "dtp_phase": snapshot.get("dtp_phase", "neutral"),
            "crsi_state": "overbought" if snapshot.get("crsi_ob") else ("oversold" if snapshot.get("crsi_os") else "normal"),
            "atr": snapshot.get("atr", 0),
            "atr_raw": snapshot.get("atr_raw", 0),
            "atr_pct": snapshot.get("atr_pct", 0),
            "sl_dist_pct": pos.get("sl_dist_pct", 0),
            "sl_atr_ratio": pos.get("sl_atr_ratio", 0),
            "window_age_bars": self._window_age_bars("upper" if signal_window == "sd_upper" else "lower"),
            "source": "ibkr_compute",
            "entry_order_type": "limit",
            "entry_limit_mode": pos.get("entry_limit_mode", "passive_limit_dynamic"),
            "entry_price_plan": pos.get("entry_limit_mode", "passive_limit_dynamic"),
            "entry_limit_offset": pos.get("entry_limit_offset", 0),
            **exit_meta,
        }
        quality_meta = self._build_signal_quality_metadata(
            snapshot,
            direction=direction,
            setup=signal_type,
            signal_mode=signal_mode,
            candidate={
                "trigger_checks": {
                    "legacy_mr_window_valid": bool(sd_upper_valid or sd_lower_valid),
                    "divergence_confirmed": bool(div_source),
                },
                "filter_checks": {"legacy_filters_pass": True},
            },
            entry=pos.get("entry", close),
        )
        extra.update(quality_meta)
        if self._is_intraday_sd_v1():
            extra.update({
                **setup_meta,
                "sd_regime": snapshot.get("sd_regime", ""),
                "validity_minutes": self._intraday_validity_minutes(),
                "trigger_checks": {
                    "legacy_mr_window_valid": bool(sd_upper_valid or sd_lower_valid),
                    "divergence_confirmed": bool(div_source),
                },
                "filter_checks": {"legacy_filters_pass": True},
                "technical_description": reason,
            })
        if isinstance(self.params.get("target_strategy_policy"), dict):
            extra["target_strategy_policy"] = dict(self.params.get("target_strategy_policy") or {})
        if isinstance(self.params.get("target_symbol_profile"), dict):
            extra["target_symbol_profile"] = dict(self.params.get("target_symbol_profile") or {})

        return {
            "symbol": self.symbol,
            "direction": direction,
            "signal": signal_type,
            "entry": round(pos["entry"], 2),
            "stop_loss": round(pos["stop_loss"], 2),
            "take_profit": round(pos["take_profit"], 2),
            "shares": pos["shares"],
            "rr": pos["rr"],
            "risk_r": pos.get("risk_r", 0),
            "initial_stop_loss": round(pos.get("initial_stop_loss", pos["stop_loss"]), 2),
            "initial_take_profit": round(pos.get("initial_take_profit", pos["take_profit"]), 2),
            "exit_policy": pos.get("exit_policy", ""),
            "reason": reason,
            "interval": self.interval,
            "extra": extra,
            "setup": setup_meta.get("setup", signal_type),
            "setup_label": setup_meta.get("setup_label", ""),
            "setup_family": setup_meta.get("setup_family", ""),
            "signal_mode": setup_meta.get("signal_mode", signal_mode),
            "signal_quality_score": quality_meta.get("signal_quality_score", 0),
        }

    def _empty_trace(self) -> dict:
        return {
            "signal_state": {
                "stage": "none",
                "direction": "",
                "signal": "",
                "label": "无信号",
                "reason": "",
                "filter_reason": "",
                "signal_window": "",
                "signal_mode": "",
                "ema_touch_line": "",
                "div_source": "",
                "setup": "",
                "setup_label": "",
                "setup_family": "",
                "strategy_profile": "",
                "entry_order_type": "",
                "entry_limit_mode": "",
                "entry_price_plan": "",
                "technical_description": "",
                "trigger_checks": {},
                "filter_checks": {},
                "signal_payload": None,
            },
            "events": [],
            "filters": [],
            "window_flags": {},
            "component_flags": {},
            "setup_state": {},
        }

    def _build_filter_labels(
        self,
        *,
        block_mr_long: bool,
        block_mr_short: bool,
        block_ema_trend: bool,
        block_all: bool,
        filter_reason_buy: str = "",
        filter_reason_sell: str = "",
    ) -> list[str]:
        labels: list[str] = []
        if block_all:
            labels.append("震荡市过滤")
        if block_ema_trend:
            labels.append("EMA 趋势过滤")
        if block_mr_long:
            labels.append("MR Long 过滤")
        if block_mr_short:
            labels.append("MR Short 过滤")
        if filter_reason_buy:
            labels.append(f"多头过滤: {filter_reason_buy}")
        if filter_reason_sell:
            labels.append(f"空头过滤: {filter_reason_sell}")
        return labels

    def _build_signal_state_label(self, signal: dict | None, stage: str) -> str:
        if not signal:
            return "无信号"
        extra = dict(signal.get("extra") or {})
        direction = str(signal.get("direction", "") or "").strip().lower()
        signal_window = str(extra.get("signal_window", "") or "").strip().lower()
        signal_mode = str(extra.get("signal_mode", "") or "").strip().lower()
        if direction == "long":
            setup = str(extra.get("setup", "") or "")
            if setup == "sd_squeeze_breakout_long":
                base = "SD挤压突破多"
            elif setup == "vwap_trend_pullback_long":
                base = "VWAP趋势回踩多"
            elif setup == "sd_trend_continuation_long":
                base = "SD顺势延续多"
            elif setup == "sd_mr_reversal_long":
                base = "SD均值回归多"
            else:
                base = "顺势多" if signal_mode in {"trend", "trend_continuation"} and signal_window == "sd_upper" else "回归多"
        elif direction == "short":
            setup = str(extra.get("setup", "") or "")
            if setup == "sd_squeeze_breakout_short":
                base = "SD挤压突破空"
            elif setup == "vwap_trend_pullback_short":
                base = "VWAP趋势回踩空"
            elif setup == "sd_trend_continuation_short":
                base = "SD顺势延续空"
            elif setup == "sd_mr_reversal_short":
                base = "SD均值回归空"
            else:
                base = "顺势空" if signal_mode in {"trend", "trend_continuation"} and signal_window == "sd_lower" else "回归空"
        else:
            base = str(signal.get("signal") or "信号")
        suffix_map = {
            "confirmed": "已确认",
            "candidate": "候选",
            "blocked": "已过滤",
        }
        suffix = suffix_map.get(str(stage or "").strip().lower())
        return f"{base}{suffix}" if suffix else base

    def _build_trace_payload(
        self,
        snapshot: dict,
        *,
        stage: str,
        signal: dict | None = None,
        filter_reason: str = "",
        events: list[str] | None = None,
        filters: list[str] | None = None,
        window_flags: dict | None = None,
        component_flags: dict | None = None,
        setup_state: dict | None = None,
    ) -> dict:
        signal_copy = deepcopy(signal) if signal else None
        signal_extra = dict((signal_copy or {}).get("extra") or {})
        return {
            "signal_state": {
                "stage": str(stage or "none"),
                "direction": str((signal_copy or {}).get("direction", "") or ""),
                "signal": str((signal_copy or {}).get("signal", "") or ""),
                "label": self._build_signal_state_label(signal_copy, stage),
                "reason": str((signal_copy or {}).get("reason", "") or ""),
                "filter_reason": str(filter_reason or ""),
                "signal_window": str(signal_extra.get("signal_window", "") or ""),
                "signal_mode": str(signal_extra.get("signal_mode", "") or ""),
                "ema_touch_line": str(signal_extra.get("ema_touch_line", "") or ""),
                "div_source": str(signal_extra.get("div_source", "") or ""),
                "setup": str(signal_extra.get("setup", "") or ""),
                "setup_label": str(signal_extra.get("setup_label", "") or ""),
                "setup_family": str(signal_extra.get("setup_family", "") or ""),
                "strategy_profile": str(signal_extra.get("strategy_profile", "") or ""),
                "entry_order_type": str(signal_extra.get("entry_order_type", "") or ""),
                "entry_limit_mode": str(signal_extra.get("entry_limit_mode", "") or ""),
                "entry_price_plan": str(signal_extra.get("entry_price_plan", "") or ""),
                "technical_description": str(signal_extra.get("technical_description", "") or ""),
                "trigger_checks": dict(signal_extra.get("trigger_checks") or {}),
                "filter_checks": dict(signal_extra.get("filter_checks") or {}),
                "signal_payload": signal_copy,
            },
            "events": list(events or []),
            "filters": list(filters or []),
            "window_flags": dict(window_flags or {}),
            "component_flags": dict(component_flags or {}),
            "setup_state": dict(setup_state or {}),
        }

    def get_trace_snapshot(self) -> dict:
        return deepcopy(self.last_trace or self._empty_trace())

    def _get_div_source(self, direction: str) -> str:
        if direction == "long":
            crsi = self.bull_crsi_div_seen
            obv = self.bull_obv_div_seen
        else:
            crsi = self.bear_crsi_div_seen
            obv = self.bear_obv_div_seen
        if crsi and obv:
            return "cRSI+OBV"
        if crsi:
            return "cRSI"
        if obv:
            return "OBV"
        return ""

    @staticmethod
    def _zone_str(snap):
        z = snap.get("sd_zone", 0)
        return "overbought" if z == 1 else "oversold" if z == -1 else "normal"

    @staticmethod
    def _trend_str(v):
        return "up" if v == 1 else "down" if v == -1 else "flat"

    @staticmethod
    def _dtp_str(v):
        return "bullish" if v == 1 else "bearish" if v == -1 else "neutral"

    # ── 每日重置 ──

    def daily_reset(self):
        self.sd_lower_mr_active = False
        self.sd_upper_mr_active = False
        self.sd_lower_mr_used = False
        self.sd_upper_mr_used = False
        self.sd_lower_touched = False
        self.sd_upper_touched = False
        self._bar_index = 0
        self._sd_lower_activated_bar = 0
        self._sd_upper_activated_bar = 0
        self._clear_bull_components()
        self._clear_bear_components()
        self.buy_consumed = False
        self.sell_consumed = False
        self._prev_sd_lower = False
        self._prev_sd_upper = False
        self._prev_buy_signal = False
        self._prev_sell_signal = False
        self._prev_intraday_raw_key = ""
        self._recent_squeeze_bars = 0
        self._intraday_last_signal_day_by_key.clear()
        self._intraday_last_signal_bar_by_key.clear()
        self.last_trace = self._empty_trace()

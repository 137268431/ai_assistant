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
from .position_sizing import calc_long_position, calc_marketable_limit_position, calc_short_position
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
        self.last_trace = self._empty_trace()

    def set_params(self, params: dict = None) -> None:
        self.params = params or {}
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
        return self._intraday_param_bool("intraday_include_legacy_signals", False)

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

        # 去重: buy_once / sell_once
        buy_once = legacy_signals_enabled and buy_raw and not self._prev_buy_signal and not should_filter_buy
        sell_once = legacy_signals_enabled and sell_raw and not self._prev_sell_signal and not should_filter_sell
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
        intraday_candidate = setup_state.get("selected") if self._is_intraday_sd_v1() else None
        intraday_signal = None
        intraday_preview_signal = None
        intraday_stage = "none"
        if intraday_candidate:
            raw_key = f"{intraday_candidate.get('direction')}:{intraday_candidate.get('setup')}"
            intraday_once = raw_key != self._prev_intraday_raw_key
            self._prev_intraday_raw_key = raw_key
            intraday_preview_signal = self._build_intraday_signal(snapshot, intraday_candidate)
            if intraday_candidate.get("filters_pass"):
                intraday_stage = "confirmed" if intraday_once else "candidate"
                if intraday_once:
                    intraday_signal = intraday_preview_signal
            else:
                intraday_stage = "blocked"
        else:
            self._prev_intraday_raw_key = ""

        # ── 6. 生成信号预览 ──
        signal = None
        stage = "none"
        preview_signal = None
        if buy_once:
            signal = self._build_signal("long", snapshot, sd_upper_valid, sd_lower_valid)
            preview_signal = signal
            stage = "confirmed"
        elif sell_once:
            signal = self._build_signal("short", snapshot, sd_upper_valid, sd_lower_valid)
            preview_signal = signal
            stage = "confirmed"
        elif intraday_signal:
            signal = intraday_signal
            preview_signal = signal
            stage = "confirmed"
            events.append(f"确认 {intraday_candidate.get('setup')} setup")
        elif legacy_signals_enabled and buy_raw:
            preview_signal = self._build_signal("long", snapshot, sd_upper_valid, sd_lower_valid)
            stage = "blocked" if should_filter_buy else "candidate"
        elif legacy_signals_enabled and sell_raw:
            preview_signal = self._build_signal("short", snapshot, sd_upper_valid, sd_lower_valid)
            stage = "blocked" if should_filter_sell else "candidate"
        elif intraday_preview_signal:
            preview_signal = intraday_preview_signal
            stage = intraday_stage

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
        if buy_once:
            if sd_upper_valid and self.sd_upper_bull_touch_seen and self.sd_upper_bull_fractal_seen and bull_div_seen:
                self.sd_upper_mr_used = True
            if sd_lower_valid and self.sd_lower_bull_fractal_seen and bull_div_seen:
                self.sd_lower_mr_used = True
        if sell_once:
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
        if buy_once:
            self.buy_consumed = True
            self._clear_bull_components()
            events.append("多头信号确认并消费窗口")
        if sell_once:
            self.sell_consumed = True
            self._clear_bear_components()
            events.append("空头信号确认并消费窗口")

        trace_filter_reason = ""
        if stage == "blocked" and preview_signal and preview_signal.get("direction") == "long":
            trace_filter_reason = filter_reason_buy
        elif stage == "blocked" and preview_signal and preview_signal.get("direction") == "short":
            trace_filter_reason = filter_reason_sell
        if stage == "blocked" and intraday_candidate and preview_signal is intraday_preview_signal:
            failed = [
                name for name, passed in dict(intraday_candidate.get("filter_checks") or {}).items()
                if not passed
            ]
            trace_filter_reason = ",".join(failed) or trace_filter_reason

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
            "pullback_to_vwap_band": low <= max(vwap, vwap_upper1),
            "close_above_vwap": close >= vwap,
            "trend_walk_regime": (
                not self._intraday_param_bool("intraday_vwap_pullback_long_require_trend_walk", False)
                or str(snapshot.get("sd_regime", "") or "").strip().lower() == "trend_walk_up"
            ),
        }
        trend_short_checks = {
            "sd_trend_walk_down": bool(snapshot.get("sd_trend_walk_down", False)),
            "vwap_bearish": not bool(snapshot.get("vwap_bullish", close > vwap)),
            "pullback_to_vwap_band": high >= min(vwap, vwap_lower1),
            "close_below_vwap": close <= vwap,
        }

        candidates = [
            self._intraday_candidate(
                "sd_squeeze_breakout_long",
                "long",
                "breakout",
                squeeze_long_checks,
                self._intraday_filter_checks(snapshot, "long", base_filter_checks),
                "SD squeeze released upward with price holding above VWAP.",
            ),
            self._intraday_candidate(
                "sd_squeeze_breakout_short",
                "short",
                "breakout",
                squeeze_short_checks,
                self._intraday_filter_checks(snapshot, "short", base_filter_checks),
                "SD squeeze released downward with price holding below VWAP.",
            ),
            self._intraday_candidate(
                "vwap_trend_pullback_long",
                "long",
                "trend_pullback",
                trend_long_checks,
                self._intraday_filter_checks(snapshot, "long", base_filter_checks),
                "SD trend-walk up remains intact after a pullback into the VWAP band.",
            ),
            self._intraday_candidate(
                "vwap_trend_pullback_short",
                "short",
                "trend_pullback",
                trend_short_checks,
                self._intraday_filter_checks(snapshot, "short", base_filter_checks),
                "SD trend-walk down remains intact after a pullback into the VWAP band.",
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
    ) -> dict:
        filters_pass = all(filter_checks.values())
        return {
            "setup": setup,
            "direction": direction,
            "signal_mode": signal_mode,
            "trigger_checks": dict(trigger_checks),
            "filter_checks": dict(filter_checks),
            "triggered": all(trigger_checks.values()),
            "filters_pass": bool(filters_pass),
            "technical_description": technical_description,
        }

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
            "extra": {
                "strategy_profile": self.strategy_profile,
                "setup": setup,
                "sd_regime": snapshot.get("sd_regime", ""),
                "entry_order_type": "marketable_limit",
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
            },
        }

    def _intraday_validity_minutes(self) -> int:
        try:
            return max(1, int(self.params.get("intraday_signal_validity_minutes", 15) or 15))
        except Exception:
            return 15

    def _intraday_param_float(self, key: str, default: float = 0.0) -> float:
        try:
            return float(self.params.get(key, default) or default)
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

    def _intraday_filter_checks(self, snapshot: dict, direction: str, base_filter_checks: dict) -> dict:
        checks = dict(base_filter_checks)
        checks["directional_day_change_max"] = self._intraday_directional_day_change_pass(snapshot, direction)
        checks["trend_mismatch_day_change_guard"] = self._intraday_trend_mismatch_pass(snapshot, direction)
        return checks

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

    # ── 信号构建 ──

    def _build_signal(self, direction: str, snapshot: dict,
                      sd_upper_valid: bool, sd_lower_valid: bool) -> dict:
        close = snapshot.get("close", 0)
        atr = snapshot.get("atr", 0)
        signal_window = "sd_upper"
        signal_mode = "mr"
        ema_touch_line_key = "none"

        if direction == "long":
            pos = calc_long_position(close, atr, self.params)
            if sd_upper_valid and self.sd_upper_bull_touch_seen:
                signal_type = "trend_sdUpper"
                signal_window = "sd_upper"
                signal_mode = "trend"
                ema_touch_line_key = self._resolve_touch_line_key(snapshot, "bull")
                reason = f"SD上轨→顺势做多(fractal↑+EMA-touch↑[{self.sd_upper_bull_touch_line}]+div↑)"
            else:
                signal_type = "mr_sdLower"
                signal_window = "sd_lower"
                signal_mode = "mr"
                reason = "SD下轨→均值回归做多(fractal↑+div↑)"
        else:
            pos = calc_short_position(close, atr, self.params)
            if sd_lower_valid and self.sd_lower_bear_touch_seen:
                signal_type = "trend_sdLower"
                signal_window = "sd_lower"
                signal_mode = "trend"
                ema_touch_line_key = self._resolve_touch_line_key(snapshot, "bear")
                reason = f"SD下轨→顺势做空(fractal↓+EMA-touch↓[{self.sd_lower_bear_touch_line}]+div↓)"
            else:
                signal_type = "mr_sdUpper"
                signal_window = "sd_upper"
                signal_mode = "mr"
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

        extra = {
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
            **exit_meta,
        }
        if self._is_intraday_sd_v1():
            extra.update({
                "strategy_profile": self.strategy_profile,
                "setup": signal_type,
                "sd_regime": snapshot.get("sd_regime", ""),
                "entry_order_type": "pullback_limit",
                "validity_minutes": self._intraday_validity_minutes(),
                "trigger_checks": {
                    "legacy_mr_window_valid": bool(sd_upper_valid or sd_lower_valid),
                    "divergence_confirmed": bool(div_source),
                },
                "filter_checks": {"legacy_filters_pass": True},
                "technical_description": reason,
            })

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
                "entry_order_type": "",
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
            else:
                base = "顺势多" if signal_mode == "trend" and signal_window == "sd_upper" else "回归多"
        elif direction == "short":
            setup = str(extra.get("setup", "") or "")
            if setup == "sd_squeeze_breakout_short":
                base = "SD挤压突破空"
            elif setup == "vwap_trend_pullback_short":
                base = "VWAP趋势回踩空"
            else:
                base = "顺势空" if signal_mode == "trend" and signal_window == "sd_lower" else "回归空"
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
                "entry_order_type": str(signal_extra.get("entry_order_type", "") or ""),
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
        self.last_trace = self._empty_trace()

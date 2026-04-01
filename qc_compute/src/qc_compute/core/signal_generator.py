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

from .position_sizing import calc_long_position, calc_short_position

logger = logging.getLogger(__name__)


class SignalGenerator:
    def __init__(self, symbol: str, interval: str, params: dict = None):
        self.symbol = symbol
        self.interval = interval
        self.params = params or {}

        # MR 窗口状态
        self.sd_lower_mr_active = False
        self.sd_upper_mr_active = False
        self.sd_lower_mr_used = False
        self.sd_upper_mr_used = False
        self.sd_lower_touched = False
        self.sd_upper_touched = False

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

    def update(self, snapshot: dict) -> dict:
        """根据最新指标快照更新 MR 窗口状态, 检测信号。
        返回 None (无信号) 或 signal dict。
        """
        if not snapshot:
            return None

        sd_lower = snapshot.get("sd_lower", False)
        sd_upper = snapshot.get("sd_upper", False)

        # ── 1. SD 窗口激活 (新触及边缘检测) ──
        if sd_lower and not self._prev_sd_lower:
            self._activate_window("lower")
        if sd_upper and not self._prev_sd_upper:
            self._activate_window("upper")
        self._prev_sd_lower = sd_lower
        self._prev_sd_upper = sd_upper

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
        if sd_lower_valid and ema_bear_touch:
            self.sd_lower_bear_touch_seen = True
            self.sd_lower_bear_touch_line = self._resolve_touch_line(snapshot, "bear")

        # Fractal 收集
        if sd_upper_valid and fractal_bull:
            self.sd_upper_bull_fractal_seen = True
        if sd_lower_valid and fractal_bull:
            self.sd_lower_bull_fractal_seen = True
        if sd_upper_valid and fractal_bear:
            self.sd_upper_bear_fractal_seen = True
        if sd_lower_valid and fractal_bear:
            self.sd_lower_bear_fractal_seen = True

        # 背离收集 (任一窗口有效时)
        if sd_upper_valid or sd_lower_valid:
            if crsi_bull_div:
                self.bull_crsi_div_seen = True
            if crsi_bear_div:
                self.bear_crsi_div_seen = True
            if obv_bull_div:
                self.bull_obv_div_seen = True
            if obv_bear_div:
                self.bear_obv_div_seen = True

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
        if bear_components_exist and (fractal_bull or crsi_bull_div or obv_bull_div):
            self._clear_bear_components()

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

        # 去重: buy_once / sell_once
        buy_once = buy_raw and not self._prev_buy_signal and not should_filter_buy
        sell_once = sell_raw and not self._prev_sell_signal and not should_filter_sell
        self._prev_buy_signal = buy_raw
        self._prev_sell_signal = sell_raw

        # ── 6. 窗口消费 ──
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

        # ── 7. 组件清除 ──
        if buy_raw and should_filter_buy:
            self._clear_bull_components()
        if sell_raw and should_filter_sell:
            self._clear_bear_components()
        if buy_once:
            self.buy_consumed = True
            self._clear_bull_components()
        if sell_once:
            self.sell_consumed = True
            self._clear_bear_components()

        # ── 8. 生成信号 ──
        signal = None
        if buy_once:
            signal = self._build_signal("long", snapshot, sd_upper_valid, sd_lower_valid)
        elif sell_once:
            signal = self._build_signal("short", snapshot, sd_upper_valid, sd_lower_valid)

        return signal

    # ── 窗口激活 ──

    def _activate_window(self, side: str):
        """SD 触及 → 开启窗口, 重置所有组件"""
        if side == "lower":
            self.sd_lower_mr_active = True
            self.sd_lower_mr_used = False
            self.sd_lower_touched = True
        else:
            self.sd_upper_mr_active = True
            self.sd_upper_mr_used = False
            self.sd_upper_touched = True

        self._clear_bull_components()
        self._clear_bear_components()
        self.buy_consumed = False
        self.sell_consumed = False

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
        return "fast"

    # ── 信号构建 ──

    def _build_signal(self, direction: str, snapshot: dict,
                      sd_upper_valid: bool, sd_lower_valid: bool) -> dict:
        close = snapshot.get("close", 0)
        atr = snapshot.get("atr", 0)

        if direction == "long":
            pos = calc_long_position(close, atr, self.params)
            if sd_upper_valid and self.sd_upper_bull_touch_seen:
                signal_type = "trend_sdUpper"
                reason = f"SD上轨→顺势做多(fractal↑+EMA-touch↑[{self.sd_upper_bull_touch_line}]+div↑)"
            else:
                signal_type = "mr_sdLower"
                reason = "SD下轨→均值回归做多(fractal↑+div↑)"
        else:
            pos = calc_short_position(close, atr, self.params)
            if sd_lower_valid and self.sd_lower_bear_touch_seen:
                signal_type = "trend_sdLower"
                reason = f"SD下轨→顺势做空(fractal↓+EMA-touch↓[{self.sd_lower_bear_touch_line}]+div↓)"
            else:
                signal_type = "mr_sdUpper"
                reason = "SD上轨→均值回归做空(fractal↓+div↓)"

        div_source = self._get_div_source(direction)
        if div_source:
            reason = reason.replace("div↑", f"div↑[{div_source}]").replace("div↓", f"div↓[{div_source}]")

        return {
            "symbol": self.symbol,
            "direction": direction,
            "signal": signal_type,
            "entry": round(pos["entry"], 2),
            "stop_loss": round(pos["stop_loss"], 2),
            "take_profit": round(pos["take_profit"], 2),
            "shares": pos["shares"],
            "rr": pos["rr"],
            "reason": reason,
            "interval": self.interval,
            "extra": {
                "sd_zone": self._zone_str(snapshot),
                "sd_trend": self._trend_str(snapshot.get("sd_trend", 0)),
                "dtp_dir": self._dtp_str(snapshot.get("dtp_dir", 0)),
                "dtp_phase": snapshot.get("dtp_phase", "neutral"),
                "crsi_state": "overbought" if snapshot.get("crsi_ob") else ("oversold" if snapshot.get("crsi_os") else "normal"),
                "atr": snapshot.get("atr", 0),
                "atr_raw": snapshot.get("atr_raw", 0),
                "atr_pct": snapshot.get("atr_pct", 0),
                "sl_dist_pct": pos.get("sl_dist_pct", 0),
                "sl_atr_ratio": pos.get("sl_atr_ratio", 0),
                "source": "qc",
            },
        }

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
        self._clear_bull_components()
        self._clear_bear_components()
        self.buy_consumed = False
        self.sell_consumed = False
        self._prev_sd_lower = False
        self._prev_sd_upper = False
        self._prev_buy_signal = False
        self._prev_sell_signal = False

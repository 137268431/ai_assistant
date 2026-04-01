"""
指标计算引擎 — 移植 Pine Script Signal_Alert_Core[Glory].pine 全部指标

子指标列表:
  1. EMA Trend Matrix (20/50/100/200, 斜率, 间距, 排列, touch)
  2. Fractal Pivot Scanner (Williams分形 + Donchian)
  3. SD Channel (线性回归 + 多档标准差)
  4. DTP (SMA+ATR通道, 动量状态机, phase)
  5. ATR (多种平滑方式)
  6. VWAP + 偏离度
  7. cRSI (周期RSI + 动态带 + strict/sensitive背离)
  8. OBV RSI (+ strict/sensitive背离)
  9. 背离统一框架
  10. MR窗口状态机 (信号组装)

每个引擎实例对应一个 (symbol, interval) 组合, 维护滑动窗口历史bar
"""

import numpy as np
from collections import deque


class IndicatorEngine:
    MAX_HISTORY = 300

    def __init__(self, symbol: str, interval: str, params: dict = None):
        self.symbol = symbol
        self.interval = interval
        self.params = params or {}
        self.bars = deque(maxlen=self.MAX_HISTORY)
        self.bar_count = 0
        self.last_bar_time_ms = 0
        self._snapshot = {}

    def update(self, bar: dict) -> dict:
        """
        喂入一根OHLCV bar, 更新全部指标, 返回指标快照

        bar: {open, high, low, close, volume, bar_time_ms, us_time, cn_time, session_type}
        """
        bar_time_ms = bar.get("bar_time_ms", 0)
        if bar_time_ms <= self.last_bar_time_ms:
            return self._snapshot

        self.bars.append(bar)
        self.bar_count += 1
        self.last_bar_time_ms = bar_time_ms

        if len(self.bars) < 2:
            return self._snapshot

        # TODO P2: 实现全部指标计算
        closes = [b["close"] for b in self.bars]
        highs = [b["high"] for b in self.bars]
        lows = [b["low"] for b in self.bars]
        volumes = [b["volume"] for b in self.bars]

        self._snapshot = {
            "close": bar["close"],
            "open": bar["open"],
            "high": bar["high"],
            "low": bar["low"],
            "volume": bar["volume"],
            "bar_count": self.bar_count,
            "session_type": bar.get("session_type", "regular"),
            "source": "qc",
        }

        return self._snapshot

    def detect_signal(self) -> dict:
        """
        检测是否触发买卖信号

        返回: None (无信号) 或 dict {direction, signal, entry, stop_loss, ...}
        TODO P3: 实现MR窗口状态机+信号组装逻辑
        """
        return None

    def get_snapshot(self) -> dict:
        return self._snapshot.copy()

    def is_ready(self) -> bool:
        return len(self.bars) >= 50

    def reset(self):
        self.bars.clear()
        self.bar_count = 0
        self.last_bar_time_ms = 0
        self._snapshot = {}

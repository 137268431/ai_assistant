"""子指标基类 — 所有指标继承此类"""

import math
from collections import deque
from typing import Optional

import numpy as np


class BaseIndicator:
    """每个子指标维护内部滑动窗口, 每根 bar 调用 update() 返回指标值字典"""

    MAX_HISTORY = 300

    def __init__(self, params: dict = None, max_history: int = None):
        self.params = params or {}
        self._max = max_history or self.MAX_HISTORY
        self.closes = deque(maxlen=self._max)
        self.highs = deque(maxlen=self._max)
        self.lows = deque(maxlen=self._max)
        self.opens = deque(maxlen=self._max)
        self.volumes = deque(maxlen=self._max)
        self.bar_count = 0
        self._output = {}

    def _push_bar(self, bar: dict):
        self.opens.append(float(bar.get("open", 0)))
        self.highs.append(float(bar.get("high", 0)))
        self.lows.append(float(bar.get("low", 0)))
        self.closes.append(float(bar.get("close", 0)))
        self.volumes.append(float(bar.get("volume", 0)))
        self.bar_count += 1

    def update(self, bar: dict) -> dict:
        self._push_bar(bar)
        self._compute()
        return self._output

    def _compute(self):
        raise NotImplementedError

    def is_ready(self) -> bool:
        return self.bar_count >= 50

    def get_output(self) -> dict:
        return self._output.copy()

    def reset(self):
        self.closes.clear()
        self.highs.clear()
        self.lows.clear()
        self.opens.clear()
        self.volumes.clear()
        self.bar_count = 0
        self._output = {}

    # ── 通用 TA 工具函数 ──

    @staticmethod
    def ema_step(prev: float, value: float, period: int) -> float:
        alpha = 2.0 / (period + 1)
        return alpha * value + (1.0 - alpha) * prev

    @staticmethod
    def rma_step(prev: float, value: float, period: int) -> float:
        alpha = 1.0 / period
        return alpha * value + (1.0 - alpha) * prev

    @staticmethod
    def sma(data, period: int) -> Optional[float]:
        if len(data) < period:
            return None
        return float(np.mean(list(data)[-period:]))

    @staticmethod
    def wma(data, period: int) -> Optional[float]:
        if len(data) < period:
            return None
        window = list(data)[-period:]
        weights = np.arange(1, period + 1, dtype=float)
        return float(np.dot(window, weights) / weights.sum())

    @staticmethod
    def stdev(data, period: int) -> Optional[float]:
        if len(data) < period:
            return None
        return float(np.std(list(data)[-period:], ddof=0))

    @staticmethod
    def linreg(data, period: int) -> Optional[float]:
        if len(data) < period:
            return None
        window = np.array(list(data)[-period:], dtype=float)
        x = np.arange(period, dtype=float)
        coeffs = np.polyfit(x, window, 1)
        return float(coeffs[0] * (period - 1) + coeffs[1])

    @staticmethod
    def linreg_slope(data, period: int) -> Optional[float]:
        """linreg(offset=0) - linreg(offset=1)"""
        if len(data) < period:
            return None
        window = np.array(list(data)[-period:], dtype=float)
        x = np.arange(period, dtype=float)
        coeffs = np.polyfit(x, window, 1)
        return float(coeffs[0])

    @staticmethod
    def highest(data, period: int) -> Optional[float]:
        if len(data) < period:
            return None
        return float(max(list(data)[-period:]))

    @staticmethod
    def lowest(data, period: int) -> Optional[float]:
        if len(data) < period:
            return None
        return float(min(list(data)[-period:]))

    @staticmethod
    def percentile(data, period: int, pct: float) -> Optional[float]:
        if len(data) < period:
            return None
        window = list(data)[-period:]
        return float(np.percentile(window, pct, method="linear"))

    @staticmethod
    def pivothigh(data, left: int, right: int) -> Optional[float]:
        total = left + right + 1
        if len(data) < total:
            return None
        window = list(data)[-total:]
        mid = left
        mid_val = window[mid]
        for i in range(total):
            if i != mid and window[i] >= mid_val:
                return None
        return float(mid_val)

    @staticmethod
    def pivotlow(data, left: int, right: int) -> Optional[float]:
        total = left + right + 1
        if len(data) < total:
            return None
        window = list(data)[-total:]
        mid = left
        mid_val = window[mid]
        for i in range(total):
            if i != mid and window[i] <= mid_val:
                return None
        return float(mid_val)

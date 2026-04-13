"""Cyclic RSI indicator"""

from collections import deque

from .base import BaseIndicator


class CyclicRSI(BaseIndicator):

    def __init__(self, params: dict = None, max_history: int = None):
        super().__init__(params, max_history)
        self._domcycle: int = self.params.get("crsi_domcycle", 20)
        self._vibration: int = self.params.get("crsi_vibration", 6)
        self._leveling: float = self.params.get("crsi_leveling", 10.0)

        self._cycle_len: int = self._domcycle // 2
        self._cyclic_memory: int = self._domcycle * 2
        self._torque: float = 2.0 / (self._vibration + 1)
        self._phasing_lag: int = round((self._vibration - 1) / 2.0)

        # Running RMA state for up/down
        self._rma_up: float = 0.0
        self._rma_down: float = 0.0
        self._rma_seeded: bool = False
        self._up_seed: list = []
        self._down_seed: list = []

        # History buffers for lookback
        self._crsi_raw_history: deque = deque(maxlen=self._phasing_lag + 2)
        self._crsi_history: deque = deque(maxlen=max(self._cyclic_memory, 1))
        self._prev_crsi: float = 0.0

    def is_ready(self) -> bool:
        return self.bar_count >= self._cycle_len + self._phasing_lag + 2

    def _compute(self):
        if self.bar_count < 2:
            self._crsi_raw_history.append(0.0)
            self._crsi_history.append(0.0)
            self._output = {
                "crsi": 0.0,
                "crsi_raw": 0.0,
                "crsi_ub": 0.0,
                "crsi_db": 0.0,
                "crsi_ob": False,
                "crsi_os": False,
            }
            return

        change = self.closes[-1] - self.closes[-2]
        up_val = max(change, 0.0)
        down_val = -min(change, 0.0)

        # Seed RMA with SMA over cycle_len bars
        if not self._rma_seeded:
            self._up_seed.append(up_val)
            self._down_seed.append(down_val)
            if len(self._up_seed) >= self._cycle_len:
                self._rma_up = sum(self._up_seed) / self._cycle_len
                self._rma_down = sum(self._down_seed) / self._cycle_len
                self._rma_seeded = True
            else:
                self._crsi_raw_history.append(0.0)
                self._crsi_history.append(0.0)
                self._output = {
                    "crsi": 0.0,
                    "crsi_raw": 0.0,
                    "crsi_ub": 0.0,
                    "crsi_db": 0.0,
                    "crsi_ob": False,
                    "crsi_os": False,
                }
                return
        else:
            self._rma_up = self.rma_step(self._rma_up, up_val, self._cycle_len)
            self._rma_down = self.rma_step(self._rma_down, down_val, self._cycle_len)

        # Raw RSI
        if self._rma_down == 0.0:
            crsi_raw = 100.0
        elif self._rma_up == 0.0:
            crsi_raw = 0.0
        else:
            crsi_raw = 100.0 - 100.0 / (1.0 + self._rma_up / self._rma_down)

        self._crsi_raw_history.append(crsi_raw)

        # Phasing lag lookback
        if len(self._crsi_raw_history) > self._phasing_lag:
            lagged_raw = self._crsi_raw_history[-(self._phasing_lag + 1)]
        else:
            lagged_raw = crsi_raw

        # Cyclic RSI with torque smoothing
        crsi = self._torque * (2.0 * crsi_raw - lagged_raw) + (1.0 - self._torque) * self._prev_crsi
        self._prev_crsi = crsi
        self._crsi_history.append(crsi)

        # Dynamic overbought/oversold bands via percentile
        crsi_ub = self.percentile(self._crsi_history, self._cyclic_memory, 100.0 - self._leveling)
        crsi_db = self.percentile(self._crsi_history, self._cyclic_memory, self._leveling)
        if crsi_ub is None:
            crsi_ub = 70.0
        if crsi_db is None:
            crsi_db = 30.0

        self._output = {
            "crsi": crsi,
            "crsi_raw": crsi_raw,
            "crsi_ub": crsi_ub,
            "crsi_db": crsi_db,
            "crsi_ob": crsi > crsi_ub,
            "crsi_os": crsi < crsi_db,
        }

    def reset(self):
        super().reset()
        self._rma_up = 0.0
        self._rma_down = 0.0
        self._rma_seeded = False
        self._up_seed = []
        self._down_seed = []
        self._crsi_raw_history.clear()
        self._crsi_history.clear()
        self._prev_crsi = 0.0

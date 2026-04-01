"""OBV RSI indicator"""

from .base import BaseIndicator


class OBVRsi(BaseIndicator):

    def __init__(self, params: dict = None, max_history: int = None):
        super().__init__(params, max_history)
        self._obv_rsi_len: int = self.params.get("obv_rsi_len", 10)

        # Running state
        self._obv: float = 0.0
        self._prev_obv: float = 0.0
        self._rma_up: float = 0.0
        self._rma_down: float = 0.0
        self._rma_seeded: bool = False
        self._up_seed: list = []
        self._down_seed: list = []

    def is_ready(self) -> bool:
        return self.bar_count >= self._obv_rsi_len + 2

    def _compute(self):
        close = self.closes[-1]
        vol = self.volumes[-1]

        # Update OBV
        if self.bar_count >= 2:
            change = close - self.closes[-2]
            if change > 0:
                self._obv += vol
            elif change < 0:
                self._obv -= vol
            # change == 0: OBV unchanged

        # RSI of OBV
        obv_rsi = 50.0  # default
        if self.bar_count >= 2:
            obv_change = self._obv - self._prev_obv
            up_val = max(obv_change, 0.0)
            down_val = -min(obv_change, 0.0)

            if not self._rma_seeded:
                self._up_seed.append(up_val)
                self._down_seed.append(down_val)
                if len(self._up_seed) >= self._obv_rsi_len:
                    self._rma_up = sum(self._up_seed) / self._obv_rsi_len
                    self._rma_down = sum(self._down_seed) / self._obv_rsi_len
                    self._rma_seeded = True
            else:
                self._rma_up = self.rma_step(self._rma_up, up_val, self._obv_rsi_len)
                self._rma_down = self.rma_step(self._rma_down, down_val, self._obv_rsi_len)

            if self._rma_seeded:
                if self._rma_down == 0.0:
                    obv_rsi = 100.0
                elif self._rma_up == 0.0:
                    obv_rsi = 0.0
                else:
                    obv_rsi = 100.0 - 100.0 / (1.0 + self._rma_up / self._rma_down)

        self._prev_obv = self._obv

        self._output = {
            "obv": self._obv,
            "obv_rsi": obv_rsi,
        }

    def reset(self):
        super().reset()
        self._obv = 0.0
        self._prev_obv = 0.0
        self._rma_up = 0.0
        self._rma_down = 0.0
        self._rma_seeded = False
        self._up_seed = []
        self._down_seed = []

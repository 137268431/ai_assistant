#!/usr/bin/env python3
from __future__ import annotations

import runpy
from pathlib import Path

_TARGET = Path(__file__).resolve().parents[1] / "ibkr_console/validate/check_console_static_sync.py"

if __name__ == "__main__":
    runpy.run_path(str(_TARGET), run_name="__main__")

from __future__ import annotations

import runpy
from pathlib import Path

_TARGET = Path(__file__).resolve().parents[3] / "extensions/ibkr_api/tests/test_control_plane_split_stack.py"

if __name__ == "__main__":
    runpy.run_path(str(_TARGET), run_name="__main__")
else:
    _LOADED = runpy.run_path(str(_TARGET))
    for _key, _value in _LOADED.items():
        if _key in {"__name__", "__file__", "__package__", "__cached__", "__spec__"}:
            continue
        globals()[_key] = _value

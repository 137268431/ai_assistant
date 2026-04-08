#!/usr/bin/env python3
from pathlib import Path
import runpy

runpy.run_path(str(Path(__file__).resolve().parent / "migrations" / "migrate_tv_shadow_rows.py"), run_name="__main__")

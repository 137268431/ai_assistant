"""Timezone helpers shared across the IBKR runtime.

Use IANA zones instead of fixed UTC offsets so New York daylight saving
transitions do not shift trading windows by one hour.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
CN = ZoneInfo("Asia/Shanghai")


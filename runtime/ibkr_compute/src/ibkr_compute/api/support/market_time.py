from __future__ import annotations

from datetime import datetime

from ibkr_compute.core.time_utils import ET
from ibkr_compute.market.timeframe_utils import ms_to_et


def _api_app():
    from .. import app as api_app

    return api_app


def current_market_date(now: datetime | None = None) -> str:
    et_now = now.astimezone(ET) if now else datetime.now(ET)
    return et_now.strftime("%Y-%m-%d")


def get_environment_time_window(environment: str, key: str, default: tuple[int, int]) -> tuple[int, int]:
    api_app = _api_app()
    raw_value = str(
        api_app.cfg.get_for_environment(key, environment, f"{default[0]:02d}:{default[1]:02d}") or ""
    ).strip()
    try:
        hour_text, minute_text = raw_value.split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute
    except Exception:
        pass
    return default


def resolve_initial_signal_state(environment: str, bar_ms: int) -> tuple[str, str]:
    api_app = _api_app()
    manual_confirm_enabled = api_app.cfg.get_bool_for_environment(
        "signal_manual_confirm_enabled",
        environment,
        False,
    )

    if bar_ms <= 0:
        return (
            ("awaiting_confirm", "manual_confirmation_required")
            if manual_confirm_enabled
            else ("pending", "")
        )

    signal_time = ms_to_et(bar_ms)
    current = (signal_time.hour, signal_time.minute)
    trade_start = get_environment_time_window(
        environment,
        "trade_window_start_time",
        getattr(api_app, "DEFAULT_TRADE_WINDOW_START", (9, 35)),
    )
    trade_end = get_environment_time_window(
        environment,
        "trade_window_end_time",
        getattr(api_app, "DEFAULT_TRADE_WINDOW_END", (15, 30)),
    )
    order_end = get_environment_time_window(
        environment,
        "order_window_end_time",
        getattr(api_app, "DEFAULT_ORDER_WINDOW_END", (15, 0)),
    )

    if not (trade_start <= current <= trade_end):
        return "rejected", "outside_trade_window"
    if not (trade_start <= current <= order_end):
        return "rejected", "outside_order_window"
    if manual_confirm_enabled:
        return "awaiting_confirm", "manual_confirmation_required"
    return "pending", ""

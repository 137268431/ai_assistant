from __future__ import annotations


WATCHLIST_SYMBOL_ROLE_TRADE = "trade"
WATCHLIST_SYMBOL_ROLE_MARKET_MONITOR = "market_monitor"
VALID_WATCHLIST_SYMBOL_ROLES = {
    WATCHLIST_SYMBOL_ROLE_TRADE,
    WATCHLIST_SYMBOL_ROLE_MARKET_MONITOR,
}


def build_bar_environment_filter(environment: str, include_legacy_empty: bool = False) -> str:
    runtime_environment = str(environment or "").strip().lower() or "live"
    clauses = [f'environment = "{runtime_environment}"']
    if include_legacy_empty and runtime_environment == "live":
        clauses.append('environment = ""')
    return f"({' || '.join(clauses)})" if len(clauses) > 1 else clauses[0]


def normalize_watchlist_symbol_role(value, default: str = WATCHLIST_SYMBOL_ROLE_TRADE) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in VALID_WATCHLIST_SYMBOL_ROLES:
        return normalized
    return default


def normalize_symbols(symbols) -> list[str]:
    normalized = []
    seen = set()
    if isinstance(symbols, str):
        symbols = [symbols]
    for raw_symbol in (symbols or []):
        symbol = str(raw_symbol or "").strip().upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        normalized.append(symbol)
    return normalized


def normalize_symbol_csv(value) -> list[str]:
    if isinstance(value, str):
        items = value.replace("\n", ",").split(",")
    elif isinstance(value, (list, tuple, set)):
        items = []
        for raw in value:
            if isinstance(raw, str):
                items.extend(raw.replace("\n", ",").split(","))
            else:
                items.append(raw)
    else:
        items = []
    return normalize_symbols(items)


def build_symbol_filter(symbols) -> str:
    normalized = normalize_symbols(symbols)
    if not normalized:
        return ""
    return "(" + " || ".join(f'symbol = "{symbol}"' for symbol in normalized) + ")"


def normalize_bar_environment(bar: dict, environment: str) -> dict:
    payload = dict(bar)
    payload["environment"] = str(environment or "live").strip().lower() or "live"
    return payload

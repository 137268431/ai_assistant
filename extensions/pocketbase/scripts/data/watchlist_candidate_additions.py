#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any

import requests


REPO_ROOT = Path(__file__).resolve().parents[4]
API_SRC = REPO_ROOT / "runtime" / "ibkr_api" / "src"
if str(API_SRC) not in sys.path:
    sys.path.insert(0, str(API_SRC))

from ibkr_api.universe.watchlist_eligibility import (  # noqa: E402
    CANONICAL_SYMBOL_BY_GROUP,
    SIMILAR_SYMBOL_GROUPS,
    _normalize_similarity_symbol,
    _similar_group_key,
)

CANDIDATES: list[dict[str, Any]] = [
    {"symbol": "HIMS", "exchange": "NYSE", "industry": "Health Care", "priority": "user_named"},
    {"symbol": "GLW", "exchange": "NYSE", "industry": "Technology Hardware", "priority": "user_named"},
    {"symbol": "ORCL", "exchange": "NYSE", "industry": "Software", "priority": "user_named"},
    {"symbol": "NOW", "exchange": "NYSE", "industry": "Software", "priority": "user_named"},
    {"symbol": "SHOP", "exchange": "NYSE", "industry": "Internet Retail", "priority": "user_named"},
    {"symbol": "LLY", "exchange": "NYSE", "industry": "Pharmaceuticals", "priority": "user_named"},
    {"symbol": "TEM", "exchange": "NASDAQ", "industry": "Health Technology", "priority": "user_named"},
    {"symbol": "DDOG", "exchange": "NASDAQ", "industry": "Software", "priority": "user_named"},
    {"symbol": "UBER", "exchange": "NYSE", "industry": "Mobility", "priority": "active_large_mid"},
    {"symbol": "ABNB", "exchange": "NASDAQ", "industry": "Travel", "priority": "active_large_mid"},
    {"symbol": "DASH", "exchange": "NASDAQ", "industry": "Internet Services", "priority": "active_large_mid"},
    {"symbol": "TTD", "exchange": "NASDAQ", "industry": "Advertising Technology", "priority": "active_large_mid"},
    {"symbol": "CAVA", "exchange": "NYSE", "industry": "Restaurants", "priority": "active_large_mid"},
    {"symbol": "CELH", "exchange": "NASDAQ", "industry": "Beverages", "priority": "active_large_mid"},
    {"symbol": "RDDT", "exchange": "NYSE", "industry": "Internet Content", "priority": "active_large_mid"},
    {"symbol": "TOST", "exchange": "NYSE", "industry": "Payments Software", "priority": "active_large_mid"},
    {"symbol": "MDB", "exchange": "NASDAQ", "industry": "Software", "priority": "active_large_mid"},
    {"symbol": "ZS", "exchange": "NASDAQ", "industry": "Cybersecurity", "priority": "active_large_mid"},
    {"symbol": "OKTA", "exchange": "NASDAQ", "industry": "Cybersecurity", "priority": "active_large_mid"},
    {"symbol": "ONON", "exchange": "NYSE", "industry": "Apparel", "priority": "active_large_mid"},
    {"symbol": "ELF", "exchange": "NYSE", "industry": "Consumer Products", "priority": "active_large_mid"},
    {"symbol": "RIVN", "exchange": "NASDAQ", "industry": "EV", "priority": "active_large_mid"},
    {"symbol": "LCID", "exchange": "NASDAQ", "industry": "EV", "priority": "active_large_mid"},
    {"symbol": "XPEV", "exchange": "NYSE", "industry": "EV ADR", "priority": "active_large_mid"},
    {"symbol": "LI", "exchange": "NASDAQ", "industry": "EV ADR", "priority": "active_large_mid"},
    {"symbol": "NIO", "exchange": "NYSE", "industry": "EV ADR", "priority": "active_large_mid"},
    {"symbol": "DKNG", "exchange": "NASDAQ", "industry": "Gaming", "priority": "active_large_mid"},
    {"symbol": "TKO", "exchange": "NYSE", "industry": "Entertainment", "priority": "active_large_mid"},
    {"symbol": "SPOT", "exchange": "NYSE", "industry": "Streaming", "priority": "active_large_mid"},
    {"symbol": "XYZ", "exchange": "NYSE", "industry": "Payments", "priority": "active_large_mid", "aliases": ["SQ"]},
    {"symbol": "PYPL", "exchange": "NASDAQ", "industry": "Payments", "priority": "active_large_mid"},
    {"symbol": "WDAY", "exchange": "NASDAQ", "industry": "Software", "priority": "active_large_mid"},
    {"symbol": "TEAM", "exchange": "NASDAQ", "industry": "Software", "priority": "active_large_mid"},
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a repeatable watchlist candidate additions list without writing PocketBase."
    )
    parser.add_argument("--base-url", help="PocketBase base URL used only to read existing watchlist records")
    parser.add_argument("--email", help="PocketBase superuser email for read-only query")
    parser.add_argument("--password", help="PocketBase superuser password for read-only query")
    parser.add_argument("--token", help="PocketBase auth token for read-only query")
    parser.add_argument("--environment", default="live", help="Watchlist environment to check, plus global/empty")
    parser.add_argument("--existing-symbols", default="", help="Comma-separated existing symbols when PB is unavailable")
    parser.add_argument("--existing-json", help="JSON file containing watchlist rows or symbols")
    parser.add_argument("--format", choices=("json", "csv", "symbols"), default="json")
    return parser.parse_args()


def escape_filter_value(value: str) -> str:
    return str(value or "").replace("\\", "\\\\").replace('"', '\\"')


def http_json(method: str, url: str, *, headers: dict[str, str] | None = None, payload: dict | None = None) -> dict:
    response = requests.request(method, url, headers=headers, json=payload, timeout=30)
    if response.status_code >= 400:
        raise RuntimeError(f"{method} {url} failed: status={response.status_code} body={response.text[:500]}")
    return response.json() if response.text else {}


def auth_headers(args: argparse.Namespace) -> dict[str, str]:
    if args.token:
        return {"Authorization": args.token}
    if not args.email or not args.password:
        return {}
    payload = http_json(
        "POST",
        f"{args.base_url.rstrip('/')}/api/collections/_superusers/auth-with-password",
        payload={"identity": args.email, "password": args.password},
    )
    token = str(payload.get("token") or "").strip()
    return {"Authorization": token} if token else {}


def load_existing_from_pb(args: argparse.Namespace) -> list[dict[str, Any]]:
    if not args.base_url:
        return []
    env = escape_filter_value(args.environment)
    filter_expr = f'(environment = "{env}" || environment = "global" || environment = "")'
    url = (
        f"{args.base_url.rstrip('/')}/api/collections/watchlist/records"
        f"?filter={requests.utils.quote(filter_expr, safe='')}&perPage=500"
    )
    return list(http_json("GET", url, headers=auth_headers(args)).get("items") or [])


def load_existing(args: argparse.Namespace) -> list[dict[str, Any]]:
    rows = load_existing_from_pb(args)
    if args.existing_json:
        payload = json.loads(Path(args.existing_json).read_text(encoding="utf-8"))
        source = payload.get("items") if isinstance(payload, dict) else payload
        for item in source or []:
            rows.append(item if isinstance(item, dict) else {"symbol": str(item)})
    for symbol in [part.strip() for part in args.existing_symbols.split(",") if part.strip()]:
        rows.append({"symbol": symbol})
    return rows


def canonical_for(symbol: str) -> str:
    normalized = _normalize_similarity_symbol(symbol)
    for group in SIMILAR_SYMBOL_GROUPS:
        if normalized in {_normalize_similarity_symbol(member) for member in group}:
            return CANONICAL_SYMBOL_BY_GROUP.get(_similar_group_key(group), group[0])
    return symbol


def build_plan(existing_rows: list[dict[str, Any]]) -> dict[str, Any]:
    existing_symbols = {
        _normalize_similarity_symbol(row.get("symbol")): str(row.get("symbol") or "").upper()
        for row in existing_rows
        if str(row.get("symbol") or "").strip()
    }
    additions: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    seen_candidates: set[str] = set()

    for raw in CANDIDATES:
        item = dict(raw)
        symbol = canonical_for(str(item["symbol"]).upper())
        item["symbol"] = symbol
        keys = {_normalize_similarity_symbol(symbol)} | {
            _normalize_similarity_symbol(alias) for alias in item.get("aliases", [])
        }
        if keys & seen_candidates:
            continue
        seen_candidates.update(keys)
        matched = sorted(existing_symbols[key] for key in keys if key in existing_symbols)
        if matched:
            skipped.append({"symbol": symbol, "reason": "already_in_watchlist", "matched_symbols": matched})
            continue
        additions.append(
            {
                "symbol": symbol,
                "exchange": item.get("exchange", ""),
                "industry": item.get("industry", ""),
                "symbol_role": "market_monitor",
                "priority": item.get("priority", "active_large_mid"),
                "note": "candidate_only_no_db_write",
            }
        )

    return {
        "ok": True,
        "candidate_count": len(CANDIDATES),
        "additions_count": len(additions),
        "skipped_count": len(skipped),
        "additions": additions,
        "skipped": skipped,
    }


def main() -> int:
    args = parse_args()
    plan = build_plan(load_existing(args))
    if args.format == "symbols":
        print(",".join(item["symbol"] for item in plan["additions"]))
    elif args.format == "csv":
        writer = csv.DictWriter(sys.stdout, fieldnames=["symbol", "exchange", "industry", "symbol_role", "priority", "note"])
        writer.writeheader()
        writer.writerows(plan["additions"])
    else:
        print(json.dumps(plan, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

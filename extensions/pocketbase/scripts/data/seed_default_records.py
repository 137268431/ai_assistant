#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

import requests


SCRIPT_DIR = Path(__file__).resolve().parent
POCKETBASE_ROOT = SCRIPT_DIR.parent.parent
SEED_FILE = POCKETBASE_ROOT / "seeds" / "import.js"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Seed PocketBase config/watchlist defaults from the repo seed file.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8090", help="PocketBase base URL")
    parser.add_argument("--email", required=True, help="PocketBase superuser email")
    parser.add_argument("--password", required=True, help="PocketBase superuser password")
    parser.add_argument("--environment", default="global", help="Environment field for seeded records")
    parser.add_argument("--config-only", action="store_true", help="Seed only config defaults")
    parser.add_argument("--watchlist-only", action="store_true", help="Seed only watchlist defaults")
    return parser.parse_args()


def load_seed_text() -> str:
    return SEED_FILE.read_text(encoding="utf-8")


def normalize_sort_order(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(round(value))
    text = str(value or "").strip()
    if not text:
        return 0
    return int(round(float(text)))


def load_config_defaults(seed_text: str, environment: str) -> list[dict]:
    records: list[dict] = []
    for raw_line in seed_text.splitlines():
        line = raw_line.strip()
        if not line.startswith("cfg("):
            continue
        expr = line[:-1] if line.endswith(",") else line
        args = ast.literal_eval(expr[len("cfg"):])
        if len(args) != 7:
            raise ValueError(f"Unexpected config seed shape: {line}")
        key, value, default_value, display_name, group_name, sort_order, description = args
        records.append(
            {
                "key": str(key),
                "value": str(value),
                "default_value": str(default_value),
                "display_name": str(display_name),
                "group_name": str(group_name),
                "sort_order": normalize_sort_order(sort_order),
                "description": str(description),
                "environment": environment,
            }
        )
    if not records:
        raise ValueError(f"No config defaults found in {SEED_FILE}")
    return records


def load_watchlist_defaults(seed_text: str, environment: str) -> list[dict]:
    marker = "const watchlistData = "
    start = seed_text.find(marker)
    if start < 0:
        raise ValueError(f"watchlistData marker missing in {SEED_FILE}")
    start += len(marker)
    end = seed_text.find("];\n\nfunction escapeFilterValue", start)
    if end < 0:
        raise ValueError(f"watchlistData terminator missing in {SEED_FILE}")
    payload = json.loads(seed_text[start : end + 1])
    records: list[dict] = []
    for item in payload:
        symbol = str(item.get("symbol") or item.get("ticker") or "").strip()
        if not symbol:
            continue
        records.append(
            {
                "symbol": symbol,
                "exchange": str(item.get("exchange") or ""),
                "industry": str(item.get("industry") or ""),
                "created_us": str(item.get("created_us") or ""),
                "created_cn": str(item.get("created_cn") or ""),
                "updated_us": str(item.get("updated_us") or ""),
                "updated_cn": str(item.get("updated_cn") or ""),
                "note": str(item.get("note") or ""),
                "environment": environment,
                "symbol_role": str(item.get("symbol_role") or "trade"),
            }
        )
    if not records:
        raise ValueError(f"No watchlist defaults found in {SEED_FILE}")
    return records


def http_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    payload: dict | None = None,
) -> tuple[int, dict]:
    response = requests.request(
        method,
        url,
        headers=headers,
        json=payload,
        timeout=30,
    )
    if response.status_code >= 400:
        try:
            detail = response.json()
        except Exception:
            detail = response.text
        raise RuntimeError(f"{method} {url} failed: status={response.status_code} body={detail}")
    try:
        data = response.json() if response.text else {}
    except Exception:
        data = {}
    return response.status_code, data


def auth_headers(base_url: str, email: str, password: str) -> dict[str, str]:
    _, payload = http_json(
        "POST",
        f"{base_url.rstrip('/')}/api/collections/_superusers/auth-with-password",
        payload={"identity": email, "password": password},
    )
    token = str(payload.get("token") or "").strip()
    if not token:
        raise RuntimeError("PocketBase superuser auth returned no token")
    return {"Authorization": token}


def escape_filter_value(value: str) -> str:
    return str(value or "").replace("\\", "\\\\").replace('"', '\\"')


def find_existing_record(base_url: str, headers: dict[str, str], collection: str, filter_expr: str) -> dict | None:
    _, payload = http_json(
        "GET",
        f"{base_url.rstrip('/')}/api/collections/{collection}/records?filter={requests.utils.quote(filter_expr, safe='')}&perPage=1",
        headers=headers,
    )
    items = payload.get("items") or []
    return items[0] if items else None


def upsert_records(
    *,
    base_url: str,
    headers: dict[str, str],
    collection: str,
    records: list[dict],
    unique_field: str,
    environment: str,
) -> tuple[int, int]:
    created = 0
    updated = 0
    for record in records:
        unique_value = str(record.get(unique_field) or "").strip()
        if not unique_value:
            continue
        filter_expr = (
            f'{unique_field} = "{escape_filter_value(unique_value)}" && '
            f'environment = "{escape_filter_value(environment)}"'
        )
        existing = find_existing_record(base_url, headers, collection, filter_expr)
        if existing:
            http_json(
                "PATCH",
                f"{base_url.rstrip('/')}/api/collections/{collection}/records/{existing['id']}",
                headers=headers,
                payload=record,
            )
            updated += 1
        else:
            http_json(
                "POST",
                f"{base_url.rstrip('/')}/api/collections/{collection}/records",
                headers=headers,
                payload=record,
            )
            created += 1
    return created, updated


def main() -> int:
    args = parse_args()
    if args.config_only and args.watchlist_only:
        print("--config-only and --watchlist-only are mutually exclusive", file=sys.stderr)
        return 2

    seed_text = load_seed_text()
    headers = auth_headers(args.base_url, args.email, args.password)

    seed_config = not args.watchlist_only
    seed_watchlist = not args.config_only

    if seed_config:
        config_records = load_config_defaults(seed_text, args.environment)
        created, updated = upsert_records(
            base_url=args.base_url,
            headers=headers,
            collection="config",
            records=config_records,
            unique_field="key",
            environment=args.environment,
        )
        print(json.dumps({"collection": "config", "created": created, "updated": updated}, ensure_ascii=False))

    if seed_watchlist:
        watchlist_records = load_watchlist_defaults(seed_text, args.environment)
        created, updated = upsert_records(
            base_url=args.base_url,
            headers=headers,
            collection="watchlist",
            records=watchlist_records,
            unique_field="symbol",
            environment=args.environment,
        )
        print(json.dumps({"collection": "watchlist", "created": created, "updated": updated}, ensure_ascii=False))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

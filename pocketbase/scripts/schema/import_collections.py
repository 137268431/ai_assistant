#!/usr/bin/env python3
"""Import one or more PocketBase collection schemas via the superuser API.

Examples:
  python3 pocketbase/scripts/schema/import_collections.py \
    pocketbase/pb_table/schema_ibkr_backtest_reverse_signals.json \
    --base-url http://127.0.0.1:8090 \
    --email admin@example.com \
    --password secret

  python3 pocketbase/scripts/schema/import_collections.py \
    --bundle ibkr_backtest \
    --base-url http://127.0.0.1:8090 \
    --email admin@example.com \
    --password secret
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from pathlib import Path

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
POCKETBASE_ROOT = SCRIPT_DIR.parent.parent
SCHEMA_ROOT = POCKETBASE_ROOT / "pb_table"
VALID_COLLECTION_ID = re.compile(r"^[A-Za-z0-9]{15}$")
ALLOWED_KEYS = {
    "id",
    "name",
    "type",
    "system",
    "listRule",
    "viewRule",
    "createRule",
    "updateRule",
    "deleteRule",
    "fields",
    "schema",
    "indexes",
    "options",
    "authRule",
    "manageRule",
    "authAlert",
    "oauth2",
    "passwordAuth",
    "mfa",
    "otp",
    "authToken",
    "passwordResetToken",
    "emailChangeToken",
    "verificationToken",
    "fileToken",
    "verificationTemplate",
    "resetPasswordTemplate",
    "confirmEmailChangeTemplate",
}
SCHEMA_BUNDLES = {
    "ibkr_backtest": (
        "schema_ibkr_backtest_batches.json",
        "schema_ibkr_backtest_runs.json",
        "schema_ibkr_backtest_trades.json",
        "schema_ibkr_backtest_indicators.json",
        "schema_ibkr_backtest_signals.json",
        "schema_ibkr_backtest_targets.json",
        "schema_ibkr_backtest_reverse_signals.json",
    ),
}


def load_schema_file(path: Path) -> list[dict]:
    payload = json.loads(path.read_text())
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list):
        raise ValueError(f"schema file must contain an object or list: {path}")
    collections = []
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError(f"schema file contains non-object entry: {path}")
        collections.append(normalize_collection(item))
    return collections


def normalize_collection(item: dict) -> dict:
    collection = {key: value for key, value in item.items() if key in ALLOWED_KEYS}
    if "schema" in collection and "fields" not in collection:
        collection["fields"] = collection.pop("schema")
    if "fields" not in collection:
        raise ValueError(f"collection {item.get('name') or '<unknown>'} missing fields/schema")
    collection.setdefault("type", "base")
    collection.setdefault("system", False)
    collection.setdefault("indexes", [])
    collection.setdefault("options", {})
    for rule_key in ("listRule", "viewRule", "createRule", "updateRule", "deleteRule"):
        collection.setdefault(rule_key, "")
    collection_id = str(collection.get("id") or "").strip()
    if collection_id and not VALID_COLLECTION_ID.fullmatch(collection_id):
        collection.pop("id", None)
    return collection


def auth_superuser(session: requests.Session, base_url: str, email: str, password: str) -> None:
    url = f"{base_url.rstrip('/')}/api/collections/_superusers/auth-with-password"
    resp = session.post(url, json={"identity": email, "password": password}, timeout=20)
    resp.raise_for_status()
    payload = resp.json()
    token = payload.get("token")
    if not token:
        raise RuntimeError("superuser auth succeeded but no token was returned")
    session.headers["Authorization"] = token


def import_collections(session: requests.Session, base_url: str, collections: list[dict], delete_missing: bool) -> None:
    url = f"{base_url.rstrip('/')}/api/collections/import"
    resp = session.put(
        url,
        json={"collections": collections, "deleteMissing": delete_missing},
        timeout=30,
    )
    if resp.status_code not in (200, 204):
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text
        raise RuntimeError(f"import failed: {resp.status_code} {detail}")


def expand_schema_inputs(raw_inputs: list[str]) -> list[Path]:
    resolved: list[Path] = []
    seen: set[Path] = set()
    for raw_value in raw_inputs:
        raw_text = str(raw_value or "").strip()
        if not raw_text:
            continue
        matches: list[Path] = []
        if any(token in raw_text for token in ("*", "?", "[")):
            matches = [Path(item) for item in sorted(glob.glob(raw_text))]
        else:
            candidate = Path(raw_text)
            if candidate.is_dir():
                matches = sorted(path for path in candidate.iterdir() if path.suffix.lower() == ".json")
            else:
                matches = [candidate]
        if not matches:
            raise FileNotFoundError(f"schema input not found: {raw_text}")
        for match in matches:
            path = match.resolve()
            if path in seen:
                continue
            seen.add(path)
            resolved.append(path)
    return resolved


def merge_schema_paths(*groups: list[Path]) -> list[Path]:
    resolved: list[Path] = []
    seen: set[Path] = set()
    for group in groups:
        for path in group:
            normalized = path.resolve()
            if normalized in seen:
                continue
            seen.add(normalized)
            resolved.append(normalized)
    return resolved


def resolve_bundle_paths(bundle_names: list[str]) -> list[Path]:
    resolved: list[Path] = []
    seen: set[Path] = set()
    for bundle_name in bundle_names:
        normalized_name = str(bundle_name or "").strip()
        if normalized_name not in SCHEMA_BUNDLES:
            supported = ", ".join(sorted(SCHEMA_BUNDLES))
            raise ValueError(f"unknown bundle: {normalized_name} (supported: {supported})")
        for filename in SCHEMA_BUNDLES[normalized_name]:
            path = (SCHEMA_ROOT / filename).resolve()
            if path in seen:
                continue
            seen.add(path)
            resolved.append(path)
    return resolved


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "schemas",
        nargs="*",
        help="Schema files, directories, or globs to import",
    )
    parser.add_argument(
        "--bundle",
        action="append",
        default=[],
        help=f"Named schema bundle to import (supported: {', '.join(sorted(SCHEMA_BUNDLES))})",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("PB_BASE_URL", "http://127.0.0.1:8090"),
        help="PocketBase base URL (default: env PB_BASE_URL or http://127.0.0.1:8090)",
    )
    parser.add_argument(
        "--email",
        default=os.environ.get("PB_SUPERUSER_EMAIL", ""),
        help="PocketBase superuser email (default: env PB_SUPERUSER_EMAIL)",
    )
    parser.add_argument(
        "--password",
        default=os.environ.get("PB_SUPERUSER_PASSWORD", ""),
        help="PocketBase superuser password (default: env PB_SUPERUSER_PASSWORD)",
    )
    parser.add_argument(
        "--delete-missing",
        action="store_true",
        help="Delete collections/fields missing from the import payload",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve inputs and print the collection names without importing",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    schema_paths = merge_schema_paths(
        resolve_bundle_paths(args.bundle),
        expand_schema_inputs(args.schemas),
    )
    if not schema_paths:
        print("at least one schema path or --bundle is required", file=sys.stderr)
        return 2

    collections = []
    for schema_path in schema_paths:
        collections.extend(load_schema_file(schema_path))

    if args.dry_run:
        print(f"resolved {len(schema_paths)} schema file(s)")
        for schema_path in schema_paths:
            print(f"- file: {schema_path}")
        print(f"prepared {len(collections)} collection schema(s)")
        for collection in collections:
            print(f"- collection: {collection['name']}")
        return 0

    if not args.email or not args.password:
        print("PB superuser email/password are required", file=sys.stderr)
        return 2

    session = requests.Session()
    auth_superuser(session, args.base_url, args.email, args.password)
    import_collections(session, args.base_url, collections, args.delete_missing)

    print(f"imported {len(collections)} collection schema(s) into {args.base_url}")
    for collection in collections:
        print(f"- {collection['name']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

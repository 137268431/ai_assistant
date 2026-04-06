#!/usr/bin/env python3
"""Import one or more PocketBase collection schemas via the superuser API."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

import requests

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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "schemas",
        nargs="+",
        help="One or more schema json files to import",
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
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.email or not args.password:
        print("PB superuser email/password are required", file=sys.stderr)
        return 2

    collections = []
    for schema_path in args.schemas:
        collections.extend(load_schema_file(Path(schema_path)))

    session = requests.Session()
    auth_superuser(session, args.base_url, args.email, args.password)
    import_collections(session, args.base_url, collections, args.delete_missing)

    print(f"imported {len(collections)} collection schema(s) into {args.base_url}")
    for collection in collections:
        print(f"- {collection['name']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""
PocketBase REST API 客户端 (用于 qc_compute 服务)
"""

import json
import requests
from typing import Optional, Dict, Any, List


class PBClient:
    def __init__(self, base_url: str = "http://localhost:8090", token: str = ""):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.session = requests.Session()
        if token:
            self.session.headers["Authorization"] = token

    def get_records(self, collection: str, filter: str = None, sort: str = None,
                    per_page: int = 200, page: int = 1) -> List[Dict[str, Any]]:
        params = {"perPage": per_page, "page": page}
        if filter:
            params["filter"] = filter
        if sort:
            params["sort"] = sort
        url = f"{self.base_url}/api/collections/{collection}/records"
        resp = self.session.get(url, params=params, timeout=15)
        resp.raise_for_status()
        return resp.json().get("items", [])

    def get_all_records(self, collection: str, filter: str = None, sort: str = None,
                        max_pages: int = 10) -> List[Dict[str, Any]]:
        all_items = []
        for page in range(1, max_pages + 1):
            items = self.get_records(collection, filter=filter, sort=sort,
                                     per_page=200, page=page)
            all_items.extend(items)
            if len(items) < 200:
                break
        return all_items

    def create_record(self, collection: str, data: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self.base_url}/api/collections/{collection}/records"
        resp = self.session.post(url, json=data, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def update_record(self, collection: str, record_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self.base_url}/api/collections/{collection}/records/{record_id}"
        resp = self.session.patch(url, json=data, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def call_custom_api(self, endpoint: str, method: str = "POST",
                        data: Dict[str, Any] = None) -> Dict[str, Any]:
        url = f"{self.base_url}/api/custom/{endpoint}"
        if method.upper() == "GET":
            resp = self.session.get(url, timeout=15)
        else:
            resp = self.session.post(url, json=data or {}, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def upsert_indicator(self, data: Dict[str, Any]) -> Dict[str, Any]:
        return self.call_custom_api("qc/indicator", method="POST", data=data)

    def upsert_signal(self, data: Dict[str, Any]) -> Dict[str, Any]:
        return self.call_custom_api("qc/signal", method="POST", data=data)

    def upsert_scan(self, data: Dict[str, Any]) -> Dict[str, Any]:
        return self.call_custom_api("qc/scan", method="POST", data=data)

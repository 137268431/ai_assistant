"""
PocketBase REST API 客户端 (用于 ibkr_compute 服务)
"""

import os
import requests
from typing import Dict, Any, List, Optional


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

    def call_custom_api(
        self,
        endpoint: str,
        method: str = "POST",
        data: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        timeout: int = 15,
    ) -> Dict[str, Any]:
        url = f"{self.base_url}/api/custom/{endpoint}"
        if method.upper() == "GET":
            resp = self.session.get(url, params=params or {}, timeout=timeout)
        else:
            resp = self.session.post(url, json=data or {}, timeout=timeout)
        resp.raise_for_status()
        return resp.json()

    def upsert_indicator(self, data: Dict[str, Any]) -> Dict[str, Any]:
        return self.call_custom_api("ibkr/indicator", method="POST", data=data)

    def upsert_signal(self, data: Dict[str, Any]) -> Dict[str, Any]:
        return self.call_custom_api("ibkr/signal", method="POST", data=data)

    def upsert_bars(self, bars: List[Dict[str, Any]]) -> Dict[str, Any]:
        return self.call_custom_api("ibkr/bars", method="POST", data={"bars": bars})

    def upsert_scan(self, data: Dict[str, Any]) -> Dict[str, Any]:
        return self.call_custom_api("ibkr/scan", method="POST", data=data)

    def upsert_order(self, data: Dict[str, Any]) -> Dict[str, Any]:
        return self.call_custom_api("ibkr/orders/upsert", method="POST", data=data)

    def get_runtime_config(self) -> List[Dict[str, Any]]:
        payload = self.call_custom_api("ibkr/runtime/config", method="GET")
        items = payload.get("items", [])
        return items if isinstance(items, list) else []

    def notify_ibkr_event(
        self,
        title: str,
        detail: Optional[Dict[str, Any]] = None,
        notify_type: str = "status",
        environment: Optional[str] = None,
    ) -> Dict[str, Any]:
        payload = {
            "type": notify_type,
            "title": title,
            "detail": detail or {},
            "environment": environment or os.environ.get("IBKR_ENVIRONMENT", "live"),
        }
        return self.call_custom_api("ibkr/notify", method="POST", data=payload, timeout=5)

    def request_ibkr_2fa(
        self,
        reason: str = "manual_reauth",
        detail: Optional[Dict[str, Any]] = None,
        source: str = "ibkr_compute",
        environment: Optional[str] = None,
        message: str = "",
        force_reset: bool = False,
        force_new: bool = False,
    ) -> Dict[str, Any]:
        payload = {
            "reason": reason,
            "detail": detail or {},
            "source": source,
            "environment": environment or os.environ.get("IBKR_ENVIRONMENT", "live"),
            "message": message,
            "force_reset": force_reset,
            "force_new": force_new,
        }
        return self.call_custom_api("ibkr/2fa/request", method="POST", data=payload, timeout=8)

    def get_ibkr_2fa_status(
        self,
        environment: Optional[str] = None,
    ) -> Dict[str, Any]:
        params = {
            "environment": environment or os.environ.get("IBKR_ENVIRONMENT", "live"),
        }
        return self.call_custom_api("ibkr/2fa/status", method="GET", params=params, timeout=8)

    def submit_ibkr_2fa_response(
        self,
        response_code: str,
        challenge_code: str = "",
        source: str = "runtime_page",
        environment: Optional[str] = None,
    ) -> Dict[str, Any]:
        payload = {
            "response_code": response_code,
            "challenge_code": challenge_code,
            "source": source,
            "environment": environment or os.environ.get("IBKR_ENVIRONMENT", "live"),
        }
        return self.call_custom_api("ibkr/2fa/respond", method="POST", data=payload, timeout=8)

    def report_ibkr_2fa_result(
        self,
        status: str,
        detail: Optional[Dict[str, Any]] = None,
        source: str = "ibkr_compute",
        environment: Optional[str] = None,
        message: str = "",
        last_result: str = "",
        error: str = "",
        state_patch: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        payload = {
            "status": status,
            "detail": detail or {},
            "source": source,
            "environment": environment or os.environ.get("IBKR_ENVIRONMENT", "live"),
            "message": message,
            "last_result": last_result,
            "error": error,
            "state_patch": state_patch or {},
        }
        return self.call_custom_api("ibkr/2fa/result", method="POST", data=payload, timeout=8)

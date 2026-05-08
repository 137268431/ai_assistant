"""
PocketBase REST API 客户端 (用于 ibkr_compute 服务)
"""

import os
import json
import time
import requests
from typing import Dict, Any, List, Optional

PB_RETRY_ATTEMPTS = max(1, int(os.environ.get("PB_RETRY_ATTEMPTS", "7")))
PB_RETRY_BACKOFF_SECONDS = max(0.2, float(os.environ.get("PB_RETRY_BACKOFF_SECONDS", "0.5")))
PB_RETRY_STATUS_CODES = {502, 503, 504}


class PBClient:
    def __init__(
        self,
        base_url: str = "http://localhost:8090",
        token: str = "",
        *,
        prefer_runtime_config_api: bool = True,
    ):
        self.base_url = base_url.rstrip("/")
        self.custom_api_base_url = str(
            os.environ.get("IBKR_API_INTERNAL_URL")
            or os.environ.get("IBKR_API_BASE_URL")
            or "http://127.0.0.1:5102"
        ).rstrip("/")
        self.token = token
        self.prefer_runtime_config_api = bool(prefer_runtime_config_api)
        self.session = requests.Session()
        self._batch_requests_supported: Optional[bool] = None
        if token:
            self.session.headers["Authorization"] = token

    @staticmethod
    def _is_batch_requests_disabled(exc: Exception) -> bool:
        text = str(exc or "").lower()
        if "batch requests are not allowed" in text:
            return True
        if "/api/batch" not in text:
            return False
        return any(token in text for token in ("status=400", "status=403", "status=404", "status=405"))

    def _execute_single_batch_request(
        self,
        request_payload: Dict[str, Any],
        *,
        timeout: int,
    ) -> None:
        method = str(request_payload.get("method") or "POST").strip().upper() or "POST"
        path = str(request_payload.get("url") or "").strip()
        if not path:
            raise RuntimeError("pb_batch_fallback_missing_url")
        url = f"{self.base_url}{path}" if path.startswith("/") else f"{self.base_url}/{path}"
        body = request_payload.get("body")
        kwargs: Dict[str, Any] = {}
        if body is not None and method not in {"GET", "DELETE"}:
            kwargs["json"] = body
        self._request(method, url, timeout=timeout, **kwargs)

    def _execute_batch_requests_sequentially(
        self,
        requests_payload: List[Dict[str, Any]],
        *,
        timeout: int,
    ) -> None:
        for request_payload in requests_payload:
            self._execute_single_batch_request(request_payload, timeout=timeout)

    def _request(
        self,
        method: str,
        url: str,
        *,
        timeout: int = 15,
        **kwargs,
    ) -> requests.Response:
        backoff_seconds = PB_RETRY_BACKOFF_SECONDS
        last_error = None
        for attempt in range(1, PB_RETRY_ATTEMPTS + 1):
            try:
                resp = self.session.request(method.upper(), url, timeout=timeout, **kwargs)
                if resp.status_code in PB_RETRY_STATUS_CODES and attempt < PB_RETRY_ATTEMPTS:
                    time.sleep(backoff_seconds)
                    backoff_seconds = min(backoff_seconds * 2, 5.0)
                    continue
                resp.raise_for_status()
                return resp
            except requests.exceptions.RequestException as exc:
                status_code = getattr(getattr(exc, "response", None), "status_code", None)
                retryable = status_code is None or status_code in PB_RETRY_STATUS_CODES
                last_error = exc
                if not retryable or attempt >= PB_RETRY_ATTEMPTS:
                    response = getattr(exc, "response", None)
                    if response is not None:
                        try:
                            body = (response.text or "").strip()
                        except Exception:
                            body = ""
                        if body:
                            body = body.replace("\n", " ")
                            if len(body) > 500:
                                body = body[:500] + "..."
                            raise RuntimeError(
                                f"pb_request_failed:{method.upper()}:{url}:status={status_code}:body={body}"
                            ) from exc
                    raise
                time.sleep(backoff_seconds)
                backoff_seconds = min(backoff_seconds * 2, 5.0)
        if last_error is not None:
            raise last_error
        raise RuntimeError(f"pb_request_failed:{method.upper()}:{url}")

    def get_records(self, collection: str, filter: str = None, sort: str = None,
                    per_page: int = 200, page: int = 1) -> List[Dict[str, Any]]:
        params = {"perPage": per_page, "page": page}
        if filter:
            params["filter"] = filter
        if sort:
            params["sort"] = sort
        url = f"{self.base_url}/api/collections/{collection}/records"
        resp = self._request("GET", url, params=params, timeout=15)
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
        resp = self._request("POST", url, json=data, timeout=15)
        return resp.json()

    def create_records(
        self,
        collection: str,
        items: List[Dict[str, Any]],
        *,
        timeout: int = 30,
        batch_size: int = 50,
    ) -> Dict[str, Any]:
        prepared = [dict(item or {}) for item in (items or []) if isinstance(item, dict)]
        if not prepared:
            return {"ok": True, "created": 0, "total": 0}

        requests_payload = [
            {
                "method": "POST",
                "url": f"/api/collections/{collection}/records",
                "body": item,
            }
            for item in prepared
        ]
        self._execute_batch_requests(requests_payload, timeout=timeout, batch_size=batch_size)
        return {"ok": True, "created": len(prepared), "total": len(prepared)}

    def update_record(self, collection: str, record_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self.base_url}/api/collections/{collection}/records/{record_id}"
        resp = self._request("PATCH", url, json=data, timeout=15)
        return resp.json()

    def delete_record(self, collection: str, record_id: str) -> bool:
        url = f"{self.base_url}/api/collections/{collection}/records/{record_id}"
        resp = self._request("DELETE", url, timeout=15)
        if resp.status_code in (200, 204):
            return True
        return True

    def get_first_record(
        self,
        collection: str,
        filter: str = None,
        sort: str = None,
    ) -> Optional[Dict[str, Any]]:
        rows = self.get_records(collection, filter=filter, sort=sort, per_page=1, page=1)
        return rows[0] if rows else None

    def call_custom_api(
        self,
        endpoint: str,
        method: str = "POST",
        data: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        timeout: int = 15,
    ) -> Dict[str, Any]:
        url = f"{self.custom_api_base_url}/api/custom/{endpoint}"
        if method.upper() == "GET":
            resp = self._request("GET", url, params=params or {}, timeout=timeout)
        else:
            resp = self._request("POST", url, json=data or {}, timeout=timeout)
        return resp.json()

    @staticmethod
    def _escape_filter_string(value: Any) -> str:
        return str(value or "").replace("\\", "\\\\").replace('"', '\\"')

    @staticmethod
    def _build_unique_key(data: Dict[str, Any], unique_fields: List[str]) -> tuple:
        return tuple(data.get(field) for field in unique_fields)

    def _render_filter_condition(self, field: str, value: Any) -> str:
        if value is None:
            return f"{field} = null"
        if isinstance(value, bool):
            return f"{field} = {'true' if value else 'false'}"
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return f"{field} = {value}"
        return f'{field} = "{self._escape_filter_string(value)}"'

    def _find_existing_records(
        self,
        collection: str,
        items: List[Dict[str, Any]],
        unique_fields: List[str],
        *,
        filter_chunk_size: int = 12,
    ) -> Dict[tuple, Dict[str, Any]]:
        existing: Dict[tuple, Dict[str, Any]] = {}
        if not items:
            return existing

        for start in range(0, len(items), max(1, int(filter_chunk_size or 1))):
            chunk = items[start:start + max(1, int(filter_chunk_size or 1))]
            filter_parts = []
            for item in chunk:
                predicates = [
                    self._render_filter_condition(field, item.get(field))
                    for field in unique_fields
                ]
                filter_parts.append(f"({' && '.join(predicates)})")
            rows = self.get_all_records(
                collection,
                filter=" || ".join(filter_parts),
                max_pages=max(1, len(chunk)),
            )
            for row in rows:
                existing[self._build_unique_key(row, unique_fields)] = row
        return existing

    def _execute_batch_requests(
        self,
        requests_payload: List[Dict[str, Any]],
        *,
        timeout: int = 30,
        batch_size: int = 50,
    ) -> None:
        if not requests_payload:
            return

        if self._batch_requests_supported is False:
            self._execute_batch_requests_sequentially(requests_payload, timeout=timeout)
            return

        url = f"{self.base_url}/api/batch"
        for start in range(0, len(requests_payload), max(1, int(batch_size or 1))):
            chunk = requests_payload[start:start + max(1, int(batch_size or 1))]
            try:
                self._request(
                    "POST",
                    url,
                    json={"requests": chunk},
                    timeout=timeout,
                )
                self._batch_requests_supported = True
            except Exception as exc:
                if not self._is_batch_requests_disabled(exc):
                    raise
                self._batch_requests_supported = False
                self._execute_batch_requests_sequentially(chunk, timeout=timeout)

    def _batch_upsert_records(
        self,
        collection: str,
        items: List[Dict[str, Any]],
        unique_fields: List[str],
        *,
        timeout: int = 30,
    ) -> Dict[str, Any]:
        prepared = [dict(item or {}) for item in (items or []) if isinstance(item, dict)]
        if not prepared:
            return {"ok": True, "created": 0, "updated": 0, "skipped": 0}

        existing = self._find_existing_records(collection, prepared, unique_fields)
        requests_payload = []
        created = 0
        updated = 0

        for item in prepared:
            unique_key = self._build_unique_key(item, unique_fields)
            existing_row = existing.get(unique_key)
            if existing_row and existing_row.get("id"):
                updated += 1
                requests_payload.append(
                    {
                        "method": "PATCH",
                        "url": f"/api/collections/{collection}/records/{existing_row['id']}",
                        "body": item,
                    }
                )
                continue

            created += 1
            requests_payload.append(
                {
                    "method": "POST",
                    "url": f"/api/collections/{collection}/records",
                    "body": item,
                }
            )

        self._execute_batch_requests(requests_payload, timeout=timeout)
        return {
            "ok": True,
            "created": created,
            "updated": updated,
            "skipped": 0,
            "total": len(prepared),
        }

    def upsert_indicator(self, data: Dict[str, Any]) -> Dict[str, Any]:
        return self.call_custom_api("ibkr/indicator", method="POST", data=data)

    def upsert_indicators(self, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        return self.call_custom_api("ibkr/indicators", method="POST", data={"items": items}, timeout=30)

    def upsert_signal(self, data: Dict[str, Any]) -> Dict[str, Any]:
        return self.call_custom_api("ibkr/signal", method="POST", data=data)

    def upsert_signals(self, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        return self.call_custom_api("ibkr/signals", method="POST", data={"items": items}, timeout=30)

    def upsert_bars(self, bars: List[Dict[str, Any]]) -> Dict[str, Any]:
        normalized = []
        for bar in bars or []:
            if not isinstance(bar, dict):
                continue
            item = dict(bar)
            item["symbol"] = str(item.get("symbol") or "").strip().upper()
            item["interval"] = str(item.get("interval") or "").strip().lower()
            item["environment"] = str(item.get("environment") or os.environ.get("IBKR_ENVIRONMENT", "live")).strip().lower() or "live"
            normalized.append(item)
        return self._batch_upsert_records(
            "ibkr_bars",
            normalized,
            ["symbol", "interval", "bar_time_ms", "environment"],
            timeout=30,
        )

    def upsert_scan(self, data: Dict[str, Any]) -> Dict[str, Any]:
        return self.call_custom_api("ibkr/scan", method="POST", data=data)

    def upsert_bar_integrity_items(self, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        return self.call_custom_api(
            "ibkr/data_quality/upsert",
            method="POST",
            data={"items": items},
            timeout=30,
        )

    def upsert_bar_coverage_daily_items(self, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        return self.call_custom_api(
            "ibkr/data_quality/daily_upsert",
            method="POST",
            data={"items": items},
            timeout=30,
        )

    def upsert_bar_truth_audit_items(self, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        return self.call_custom_api(
            "ibkr/data_quality/truth_upsert",
            method="POST",
            data={"items": items},
            timeout=30,
        )

    def upsert_backtest_daily_selection_cache_items(self, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        return self._batch_upsert_records(
            "ibkr_backtest_daily_selection_cache",
            items,
            ["cache_key", "market_date"],
            timeout=30,
        )

    def rescan_bar_integrity(self, data: Dict[str, Any]) -> Dict[str, Any]:
        return self.call_custom_api("ibkr/data_quality/rescan", method="POST", data=data, timeout=30)

    def repair_bar_integrity(self, data: Dict[str, Any]) -> Dict[str, Any]:
        return self.call_custom_api("ibkr/data_quality/repair", method="POST", data=data, timeout=30)

    def truth_audit_bar_data(self, data: Dict[str, Any]) -> Dict[str, Any]:
        return self.call_custom_api("ibkr/data_quality/truth_audit", method="POST", data=data, timeout=60)

    def upsert_order(self, data: Dict[str, Any]) -> Dict[str, Any]:
        return self.call_custom_api("ibkr/orders/upsert", method="POST", data=data)

    def ack_ibkr_signal(
        self,
        signal_id: str,
        status: str = "submitted",
        note: str = "",
        order: Optional[Dict[str, Any]] = None,
        child_orders: Optional[List[Dict[str, Any]]] = None,
        environment: Optional[str] = None,
    ) -> Dict[str, Any]:
        runtime_environment = environment or os.environ.get("IBKR_ENVIRONMENT", "live")
        payload: Dict[str, Any] = {
            "signal_id": signal_id,
            "status": status,
            "note": note,
            "environment": runtime_environment,
        }
        if order:
            payload["order"] = order
        if child_orders:
            payload["child_orders"] = child_orders

        try:
            return self.call_custom_api("ibkr/signals/ack", method="POST", data=payload, timeout=15)
        except Exception:
            # Fallback: ensure the signal is not left pending if the custom hook is temporarily unavailable.
            safe_signal_id = str(signal_id or "").replace('"', '\\"')
            safe_environment = str(runtime_environment or "live").replace('"', '\\"')
            record = self.get_first_record(
                "ibkr_signals",
                filter=(
                    f'signal_id = "{safe_signal_id}" && '
                    f'environment = "{safe_environment}"'
                ),
            )
            if not record or not record.get("id"):
                raise

            patch: Dict[str, Any] = {
                "status": status,
                "note": note,
            }
            existing_extra = record.get("extra") or {}
            if isinstance(existing_extra, str):
                try:
                    existing_extra = json.loads(existing_extra)
                except Exception:
                    existing_extra = {}
            if not isinstance(existing_extra, dict):
                existing_extra = {}
            patch["extra"] = {
                **existing_extra,
                "signal_ack_fallback": True,
                "signal_ack_fallback_at": int(time.time() * 1000),
            }
            updated = self.update_record("ibkr_signals", record["id"], patch)
            return {
                "success": True,
                "signal_id": signal_id,
                "status": status,
                "fallback": True,
                "record": updated,
            }

    def get_state(
        self,
        state_key: str,
        environment: str,
        date: str = "global",
    ) -> Optional[Dict[str, Any]]:
        runtime_environment = str(environment or "live").strip().lower() or "live"
        safe_state_key = str(state_key or "").replace('"', '\\"')
        safe_date = str(date or "global").replace('"', '\\"')
        safe_environment = runtime_environment.replace('"', '\\"')
        return self.get_first_record(
            "ibkr_state",
            filter=(
                f'state_key = "{safe_state_key}" && '
                f'date = "{safe_date}" && '
                f'environment = "{safe_environment}"'
            ),
        )

    def upsert_state(
        self,
        state_key: str,
        environment: str,
        data: Dict[str, Any],
        date: str = "global",
    ) -> Dict[str, Any]:
        runtime_environment = str(environment or "live").strip().lower() or "live"
        payload = {
            "state_key": str(state_key or ""),
            "date": str(date or "global"),
            "environment": runtime_environment,
            "data": data or {},
        }
        existing = self.get_state(payload["state_key"], runtime_environment, payload["date"])
        if existing and existing.get("id"):
            return self.update_record("ibkr_state", existing["id"], payload)
        return self.create_record("ibkr_state", payload)

    def get_runtime_config(
        self,
        *,
        scope: str = "all",
        environment: str = "",
    ) -> List[Dict[str, Any]]:
        params = {"scope": str(scope or "all").strip().lower() or "all"}
        runtime_environment = str(environment or "").strip().lower()
        if runtime_environment:
            params["environment"] = runtime_environment

        if self.prefer_runtime_config_api:
            try:
                payload = self.call_custom_api(
                    "ibkr/runtime/config",
                    method="GET",
                    params=params,
                    timeout=15,
                )
                items = payload.get("items") if isinstance(payload, dict) else None
                if isinstance(items, list):
                    return [item for item in items if isinstance(item, dict)]
            except Exception:
                pass

        rows = self.get_all_records("config", sort="sort_order,key", max_pages=20)
        return rows if isinstance(rows, list) else []

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

    def notify_system_event(
        self,
        title: str,
        detail: Optional[Dict[str, Any]] = None,
        *,
        event_type: str = "status_change",
        level: str = "info",
        source: str = "ibkr_compute",
        environment: Optional[str] = None,
        message_id: str = "",
    ) -> Dict[str, Any]:
        payload = {
            "title": title,
            "detail": detail or {},
            "event_type": event_type,
            "level": level,
            "source": source,
            "environment": environment or os.environ.get("IBKR_ENVIRONMENT", "live"),
        }
        if message_id:
            payload["message_id"] = message_id
        return self.call_custom_api("system/event", method="POST", data=payload, timeout=5)

    def sync_startup_progress(
        self,
        *,
        action: str = "update",
        environment: Optional[str] = None,
        status: str = "",
        title: str = "",
        summary: str = "",
        current_step: str = "",
        current_blocker: str = "",
        operator_action: str = "",
        reason: str = "",
        source: str = "",
        runtime_phase: str = "",
        runtime_url: str = "",
        trigger_login: Optional[bool] = None,
        steps: Optional[Dict[str, Any]] = None,
        fields: Optional[Dict[str, Any]] = None,
        create_if_missing: bool = False,
        record_event: bool = False,
        event_type: str = "",
        event_title: str = "",
        event_detail: Optional[Dict[str, Any]] = None,
        level: str = "",
        event_source: str = "",
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "action": action,
            "environment": environment or os.environ.get("IBKR_ENVIRONMENT", "live"),
            "status": status,
            "title": title,
            "summary": summary,
            "current_step": current_step,
            "current_blocker": current_blocker,
            "operator_action": operator_action,
            "reason": reason,
            "source": source,
            "runtime_phase": runtime_phase,
            "runtime_url": runtime_url,
            "steps": steps or {},
            "fields": fields or {},
            "create_if_missing": bool(create_if_missing),
            "record_event": bool(record_event),
            "event_type": event_type,
            "event_title": event_title,
            "event_detail": event_detail or {},
            "level": level,
            "event_source": event_source,
        }
        if trigger_login is not None:
            payload["trigger_login"] = bool(trigger_login)
        return self.call_custom_api("ibkr/startup/progress", method="POST", data=payload, timeout=8)

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

    def ack_ibkr_reverse_signal(
        self,
        reverse_id: str,
        status: str = "confirmed",
        reason: str = "",
        detail: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        payload = {
            "reverse_id": reverse_id,
            "status": status,
            "reason": reason,
        }
        if detail:
            payload.update(detail)
        return self.call_custom_api("ibkr/reverse/ack", method="POST", data=payload, timeout=8)

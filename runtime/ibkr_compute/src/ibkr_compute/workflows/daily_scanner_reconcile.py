"""Target reconciliation helpers for the daily IBKR scanner."""

from __future__ import annotations

import time

from .daily_scanner_constants import DAILY_SCAN_SOURCE, DAILY_SCAN_STAGE
from .daily_scanner_support import _safe_extra, _target_row_is_manual


class DailyScannerReconcileMixin:
    def _load_today_target_rows(self, date: str, environment: str) -> list[dict]:
        try:
            return self.pb_client.get_all_records(
                "ibkr_targets",
                filter=(
                    f'date = "{date}" && '
                    f'environment = "{environment}" && '
                    '(status = "candidate" || status = "active")'
                ),
                max_pages=20,
            )
        except Exception as exc:
            print(f"[Scanner] load targets error: {exc}")
            return []

    def _reconcile_removed_targets(self, *, date: str, environment: str, retained_symbols: set[str]) -> int:
        rows = self._load_today_target_rows(date, environment)
        removed = 0
        for row in rows:
            symbol = str(row.get("symbol", "")).strip().upper()
            if not symbol or symbol in retained_symbols or _target_row_is_manual(row):
                continue
            record_id = str(row.get("id") or "").strip()
            if not record_id:
                continue
            extra = _safe_extra(row)
            next_extra = {
                **extra,
                "source": extra.get("source") or DAILY_SCAN_SOURCE,
                "scan_stage": DAILY_SCAN_STAGE,
                "removed_reason": "daily_scan_trim",
                "removed_at": int(time.time() * 1000),
            }
            try:
                self.pb_client.update_record(
                    "ibkr_targets",
                    record_id,
                    {
                        "status": "removed",
                        "extra": next_extra,
                    },
                )
                removed += 1
            except Exception as exc:
                print(f"[Scanner] mark removed error: {environment}/{symbol}: {exc}")
        return removed

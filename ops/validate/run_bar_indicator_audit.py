#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


ET = ZoneInfo("America/New_York")
DEFAULT_BASE_URL = (
    os.environ.get("IBKR_BAR_AUDIT_BASE_URL")
    or os.environ.get("QUANT_BASE_URL")
    or os.environ.get("CONSOLE_BASE_URL")
    or "https://quant.lzw-glory.top"
)
DEFAULT_INTERVAL = "5m"
DEFAULT_ENVIRONMENT = "live"
DEFAULT_CHUNK_SIZE = 5
DEFAULT_TIMEOUT_SECONDS = 120.0
BAR_ERROR_FIELDS = (
    "missing_stored_bar_count",
    "missing_ibkr_bar_count",
    "bar_mismatch_count",
)
MISSING_COMPARE_STATUSES = {"missing_stored", "missing_ibkr"}

SYMBOL_PRESETS: dict[str, tuple[str, ...]] = {
    "core": ("AAPL", "MSFT", "NVDA", "TSM", "AVGO", "MU", "PLTR", "INTC"),
    "expanded": (
        "SPY",
        "QQQ",
        "IWM",
        "AAPL",
        "MSFT",
        "NVDA",
        "AMZN",
        "META",
        "GOOGL",
        "TSLA",
        "AVGO",
        "AMD",
        "MU",
        "TSM",
        "PLTR",
        "INTC",
    ),
    "semis": ("NVDA", "AVGO", "AMD", "TSM", "MU", "INTC", "ARM", "ASML", "AMAT", "LRCX"),
    "etf": ("SPY", "QQQ", "IWM", "DIA", "XLK", "SMH", "SOXX"),
}


@dataclass(frozen=True)
class AuditConfig:
    base_url: str
    environment: str
    interval: str
    market_date: str
    symbols: tuple[str, ...]
    persist_truth: bool
    chunk_size: int
    scan_scope: str
    skip_indicator_compare: bool
    compare_indicators_on_bar_fail: bool
    include_signals: bool
    timeout_seconds: float


class JsonHttpClient:
    def __init__(self, base_url: str):
        self.base_url = normalize_base_url(base_url)

    def post_json(self, path: str, payload: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        url = build_url(self.base_url, path)
        body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "ibkr-bar-indicator-audit/1.0",
            },
        )
        status_code = 0
        raw = b""
        try:
            with urllib.request.urlopen(request, timeout=float(timeout or DEFAULT_TIMEOUT_SECONDS)) as response:
                status_code = int(getattr(response, "status", 200) or 200)
                raw = response.read()
        except urllib.error.HTTPError as exc:
            status_code = int(exc.code or 0)
            raw = exc.read()
        except urllib.error.URLError as exc:
            raise RuntimeError(f"request_failed:{url}:{exc}") from exc

        text = raw.decode("utf-8", errors="replace")
        try:
            data = json.loads(text) if text else {}
        except json.JSONDecodeError:
            data = {
                "ok": False,
                "error": "non_json_response",
                "raw": text[:1000],
            }
        if not isinstance(data, dict):
            data = {"ok": False, "error": "non_object_json_response", "raw": data}
        data["_http_status"] = status_code
        data["_request_url"] = url
        return data


def normalize_base_url(base_url: str) -> str:
    clean = str(base_url or "").strip().rstrip("/")
    return clean or DEFAULT_BASE_URL.rstrip("/")


def build_url(base_url: str, path: str) -> str:
    clean_path = str(path or "").strip()
    if not clean_path.startswith("/"):
        clean_path = f"/{clean_path}"
    return f"{normalize_base_url(base_url)}{clean_path}"


def split_symbols(raw: str | list[str] | tuple[str, ...] | None) -> list[str]:
    if raw is None:
        return []
    parts: list[str] = []
    if isinstance(raw, (list, tuple)):
        for item in raw:
            parts.extend(split_symbols(str(item or "")))
    else:
        for piece in str(raw or "").replace("\n", ",").replace(";", ",").split(","):
            symbol = piece.strip().upper()
            if symbol:
                parts.append(symbol)

    output: list[str] = []
    seen: set[str] = set()
    for symbol in parts:
        if symbol in seen:
            continue
        seen.add(symbol)
        output.append(symbol)
    return output


def load_symbols_from_file(path: str) -> list[str]:
    clean = str(path or "").strip()
    if not clean:
        return []
    return split_symbols(Path(clean).expanduser().read_text(encoding="utf-8"))


def previous_weekday(value: date) -> date:
    candidate = value - timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


def latest_complete_market_date(now: datetime | None = None) -> str:
    current = now.astimezone(ET) if now else datetime.now(ET)
    candidate = current.date()
    if candidate.weekday() >= 5:
        while candidate.weekday() >= 5:
            candidate -= timedelta(days=1)
        return candidate.isoformat()

    if current.time() < dt_time(17, 0):
        candidate = previous_weekday(candidate)
    return candidate.isoformat()


def int_value(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def bar_error_total(summary: dict[str, Any] | None) -> int:
    data = summary if isinstance(summary, dict) else {}
    return sum(int_value(data.get(field), 0) for field in BAR_ERROR_FIELDS)


def row_has_empty_sample(row: dict[str, Any] | None) -> bool:
    data = row if isinstance(row, dict) else {}
    sampled = int_value(data.get("sampled_bar_count"), 0)
    matched = int_value(data.get("matched_bar_count"), 0)
    stored = int_value(data.get("stored_visible_bars"), 0)
    ibkr = int_value(data.get("ibkr_visible_bars"), 0)
    return max(sampled, matched, stored, ibkr) <= 0


def row_bar_ok(row: dict[str, Any] | None) -> bool:
    data = row if isinstance(row, dict) else {}
    status = str(data.get("status") or "").strip().lower()
    return status == "ok" and not row_has_empty_sample(data)


def truth_bar_ok(payload: dict[str, Any], expected_symbols: tuple[str, ...]) -> bool:
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        return False
    rows = [row for row in payload.get("rows") or [] if isinstance(row, dict)]
    rows_by_symbol = {str(row.get("symbol") or "").upper(): row for row in rows}
    if any(symbol not in rows_by_symbol for symbol in expected_symbols):
        return False
    if any(not row_bar_ok(rows_by_symbol.get(symbol)) for symbol in expected_symbols):
        return False

    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    status_counts = summary.get("status_counts") if isinstance(summary.get("status_counts"), dict) else {}
    if int_value(summary.get("error_count"), 0) > 0:
        return False
    if int_value(status_counts.get("error"), 0) > 0 or int_value(status_counts.get("unavailable"), 0) > 0:
        return False
    if summary.get("coverage_complete") is False:
        return False
    return True


def truth_failure_symbols(payload: dict[str, Any], expected_symbols: tuple[str, ...]) -> list[str]:
    rows = [row for row in (payload or {}).get("rows") or [] if isinstance(row, dict)]
    rows_by_symbol = {str(row.get("symbol") or "").upper(): row for row in rows}
    failures = []
    for symbol in expected_symbols:
        row = rows_by_symbol.get(symbol)
        if not row_bar_ok(row):
            failures.append(symbol)
    return failures


def compare_bar_ok(summary: dict[str, Any] | None) -> bool:
    data = summary if isinstance(summary, dict) else {}
    return bar_error_total(data) == 0 and int_value(data.get("matched_bar_count"), 0) > 0


def compare_indicator_ok(summary: dict[str, Any] | None, *, include_signals: bool = True) -> bool:
    data = summary if isinstance(summary, dict) else {}
    if not compare_bar_ok(data):
        return False
    if int_value(data.get("indicator_mismatch_count"), 0) > 0:
        return False
    if include_signals and int_value(data.get("signal_mismatch_count"), 0) > 0:
        return False
    return True


def classify_compare_summary(summary: dict[str, Any] | None, *, include_signals: bool = True) -> str:
    data = summary if isinstance(summary, dict) else {}
    if not compare_bar_ok(data):
        return "bar_failed"
    if int_value(data.get("indicator_mismatch_count"), 0) > 0:
        return "indicator_mismatch"
    if include_signals and int_value(data.get("signal_mismatch_count"), 0) > 0:
        return "signal_mismatch"
    return "ok"


def build_compare_status_counts(timeline: list[Any] | None) -> dict[str, dict[str, int]]:
    counts = {
        "bar": {},
        "indicator": {},
        "signal": {},
    }
    for item in timeline or []:
        if not isinstance(item, dict):
            continue
        statuses = item.get("status") if isinstance(item.get("status"), dict) else {}
        for layer in counts:
            status = str(statuses.get(layer) or "").strip().lower()
            if not status:
                continue
            layer_counts = counts[layer]
            layer_counts[status] = layer_counts.get(status, 0) + 1
    return counts


def has_missing_compare_status(status_counts: dict[str, dict[str, int]], layer: str) -> bool:
    layer_counts = status_counts.get(layer) if isinstance(status_counts, dict) else {}
    if not isinstance(layer_counts, dict):
        return False
    return any(int_value(layer_counts.get(status), 0) > 0 for status in MISSING_COMPARE_STATUSES)


def classify_compare_payload(
    summary: dict[str, Any] | None,
    status_counts: dict[str, dict[str, int]] | None = None,
    *,
    include_signals: bool = True,
) -> str:
    data = summary if isinstance(summary, dict) else {}
    counts = status_counts if isinstance(status_counts, dict) else {}
    if not compare_bar_ok(data) or has_missing_compare_status(counts, "bar"):
        return "bar_failed"
    if int_value(data.get("indicator_mismatch_count"), 0) > 0 or has_missing_compare_status(counts, "indicator"):
        return "indicator_mismatch"
    if include_signals and (
        int_value(data.get("signal_mismatch_count"), 0) > 0
        or has_missing_compare_status(counts, "signal")
    ):
        return "signal_mismatch"
    return "ok"


def build_truth_audit_payload(config: AuditConfig) -> dict[str, Any]:
    return {
        "environment": config.environment,
        "data_environment": config.environment,
        "market_data_mode": config.environment,
        "symbols": list(config.symbols),
        "market_date": config.market_date,
        "persist": bool(config.persist_truth),
        "chunk_size": int(config.chunk_size or DEFAULT_CHUNK_SIZE),
        "scan_scope": config.scan_scope,
    }


def build_chart_compare_proxy_payload(
    config: AuditConfig,
    *,
    symbol: str,
    start_ms: int,
    end_ms: int,
) -> dict[str, Any]:
    return {
        "action": "chart/compare",
        "environment": config.environment,
        "data_environment": config.environment,
        "market_data_mode": config.environment,
        "symbol": str(symbol or "").strip().upper(),
        "interval": config.interval,
        "start_ms": int(start_ms or 0),
        "end_ms": int(end_ms or 0),
        "include_signals": bool(config.include_signals and config.interval == DEFAULT_INTERVAL),
    }


def slim_truth_row(row: dict[str, Any]) -> dict[str, Any]:
    examples = row.get("mismatch_examples") if isinstance(row.get("mismatch_examples"), list) else []
    return {
        "symbol": str(row.get("symbol") or "").upper(),
        "status": str(row.get("status") or ""),
        "sampled_bar_count": int_value(row.get("sampled_bar_count"), 0),
        "matched_bar_count": int_value(row.get("matched_bar_count"), 0),
        "missing_stored_bar_count": int_value(row.get("missing_stored_bar_count"), 0),
        "missing_ibkr_bar_count": int_value(row.get("missing_ibkr_bar_count"), 0),
        "bar_mismatch_count": int_value(row.get("bar_mismatch_count"), 0),
        "indicator_mismatch_count": int_value(row.get("indicator_mismatch_count"), 0),
        "signal_mismatch_count": int_value(row.get("signal_mismatch_count"), 0),
        "window_start_ms": int_value(row.get("window_start_ms"), 0),
        "window_end_ms": int_value(row.get("window_end_ms"), 0),
        "last_checked_at": str(row.get("last_checked_at") or ""),
        "mismatch_examples": examples[:5],
        "source_meta": row.get("source_meta") if isinstance(row.get("source_meta"), dict) else {},
    }


def slim_compare_response(symbol: str, response: dict[str, Any], *, include_signals: bool) -> dict[str, Any]:
    comparison = response.get("comparison") if isinstance(response.get("comparison"), dict) else {}
    summary = comparison.get("summary") if isinstance(comparison.get("summary"), dict) else {}
    examples = comparison.get("mismatch_examples") if isinstance(comparison.get("mismatch_examples"), list) else []
    status_counts = build_compare_status_counts(
        comparison.get("timeline") if isinstance(comparison.get("timeline"), list) else []
    )
    status = (
        classify_compare_payload(summary, status_counts, include_signals=include_signals)
        if response.get("ok") is True
        else "unavailable"
    )
    return {
        "symbol": str(symbol or "").strip().upper(),
        "ok": bool(response.get("ok") is True and status == "ok"),
        "status": status,
        "http_status": int_value(response.get("_http_status"), 0),
        "error": str(response.get("error") or ""),
        "summary": summary,
        "status_counts": status_counts,
        "mismatch_examples": examples[:5],
        "meta": response.get("meta") if isinstance(response.get("meta"), dict) else {},
    }


def build_indicator_summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    failed_symbols = [item["symbol"] for item in items if not item.get("ok")]
    return {
        "total": len(items),
        "ok": not failed_symbols and bool(items),
        "ok_symbols": [item["symbol"] for item in items if item.get("ok")],
        "failed_symbols": failed_symbols,
        "bar_failed_symbols": [item["symbol"] for item in items if item.get("status") == "bar_failed"],
        "indicator_mismatch_symbols": [item["symbol"] for item in items if item.get("status") == "indicator_mismatch"],
        "signal_mismatch_symbols": [item["symbol"] for item in items if item.get("status") == "signal_mismatch"],
        "unavailable_symbols": [item["symbol"] for item in items if item.get("status") == "unavailable"],
    }


def run_audit(config: AuditConfig, client: JsonHttpClient | None = None) -> dict[str, Any]:
    http = client or JsonHttpClient(config.base_url)
    truth_payload = http.post_json(
        "/api/custom/ibkr/data_quality/truth_audit",
        build_truth_audit_payload(config),
        timeout=config.timeout_seconds,
    )
    bar_ok = truth_bar_ok(truth_payload, config.symbols)
    truth_rows = [slim_truth_row(row) for row in truth_payload.get("rows") or [] if isinstance(row, dict)]
    truth_summary = truth_payload.get("summary") if isinstance(truth_payload.get("summary"), dict) else {}
    window_start_ms = int_value(truth_summary.get("window_start_ms"), 0)
    window_end_ms = int_value(truth_summary.get("window_end_ms"), 0)

    indicator_section: dict[str, Any] = {
        "ran": False,
        "skipped_reason": "",
        "items": [],
        "summary": {
            "total": 0,
            "ok": False,
            "ok_symbols": [],
            "failed_symbols": [],
            "bar_failed_symbols": [],
            "indicator_mismatch_symbols": [],
            "signal_mismatch_symbols": [],
            "unavailable_symbols": [],
        },
    }

    if config.skip_indicator_compare:
        indicator_section["skipped_reason"] = "disabled_by_flag"
    elif not bar_ok and not config.compare_indicators_on_bar_fail:
        indicator_section["skipped_reason"] = "bar_truth_failed"
    elif window_start_ms <= 0 or window_end_ms <= 0:
        indicator_section["skipped_reason"] = "missing_truth_window"
    else:
        items: list[dict[str, Any]] = []
        for symbol in config.symbols:
            response = http.post_json(
                "/api/custom/ibkr/proxy",
                build_chart_compare_proxy_payload(
                    config,
                    symbol=symbol,
                    start_ms=window_start_ms,
                    end_ms=window_end_ms,
                ),
                timeout=config.timeout_seconds,
            )
            items.append(slim_compare_response(symbol, response, include_signals=config.include_signals))
        indicator_section = {
            "ran": True,
            "skipped_reason": "",
            "items": items,
            "summary": build_indicator_summary(items),
        }

    indicator_ok = True
    if indicator_section["ran"]:
        indicator_ok = bool((indicator_section.get("summary") or {}).get("ok"))
    elif indicator_section.get("skipped_reason") == "bar_truth_failed":
        indicator_ok = False

    if not bar_ok:
        verdict = "bar_truth_failed"
    elif indicator_section["ran"] and not indicator_ok:
        verdict = "indicator_compare_failed"
    elif indicator_section["ran"]:
        verdict = "bar_truth_ok_indicator_ok"
    else:
        verdict = "bar_truth_ok_indicator_skipped"

    return {
        "ok": bool(bar_ok and indicator_ok),
        "verdict": verdict,
        "config": {
            "base_url": normalize_base_url(config.base_url),
            "environment": config.environment,
            "interval": config.interval,
            "market_date": config.market_date,
            "symbols": list(config.symbols),
            "persist_truth": config.persist_truth,
            "chunk_size": config.chunk_size,
            "scan_scope": config.scan_scope,
            "include_signals": config.include_signals,
        },
        "bar_truth": {
            "ok": bar_ok,
            "failure_symbols": truth_failure_symbols(truth_payload, config.symbols),
            "http_status": int_value(truth_payload.get("_http_status"), 0),
            "error": str(truth_payload.get("error") or ""),
            "summary": truth_summary,
            "rows": truth_rows,
            "errors": truth_payload.get("errors") if isinstance(truth_payload.get("errors"), list) else [],
        },
        "indicator_compare": indicator_section,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def print_human_summary(result: dict[str, Any]) -> None:
    config = result.get("config") if isinstance(result.get("config"), dict) else {}
    bar = result.get("bar_truth") if isinstance(result.get("bar_truth"), dict) else {}
    indicator = result.get("indicator_compare") if isinstance(result.get("indicator_compare"), dict) else {}
    symbols = config.get("symbols") if isinstance(config.get("symbols"), list) else []
    truth_summary = bar.get("summary") if isinstance(bar.get("summary"), dict) else {}
    status_counts = truth_summary.get("status_counts") if isinstance(truth_summary.get("status_counts"), dict) else {}

    print("Bar -> Indicator Audit")
    print(f"- Verdict: {result.get('verdict')} (ok={bool(result.get('ok'))})")
    print(
        f"- Scope: {config.get('environment')} {config.get('interval')} "
        f"{config.get('market_date')} symbols={len(symbols)}"
    )
    print(
        "- Bar truth: "
        f"ok={bool(bar.get('ok'))} audited={truth_summary.get('audited_symbols_total', truth_summary.get('total', 0))} "
        f"status={status_counts} failures={bar.get('failure_symbols') or []}"
    )

    rows = bar.get("rows") if isinstance(bar.get("rows"), list) else []
    bad_rows = [
        row for row in rows
        if str(row.get("status") or "").lower() != "ok"
        or row_has_empty_sample(row)
        or bar_error_total(row) > 0
    ]
    if bad_rows:
        print("- Bar failures:")
        for row in bad_rows[:12]:
            print(
                f"  {row.get('symbol')}: status={row.get('status')} matched={row.get('matched_bar_count')} "
                f"missing_stored={row.get('missing_stored_bar_count')} "
                f"missing_ibkr={row.get('missing_ibkr_bar_count')} diff={row.get('bar_mismatch_count')}"
            )
    else:
        print("- Bar failures: none")

    if indicator.get("ran"):
        summary = indicator.get("summary") if isinstance(indicator.get("summary"), dict) else {}
        print(
            "- Indicator compare: "
            f"ok={bool(summary.get('ok'))} total={summary.get('total', 0)} "
            f"indicator_diff={summary.get('indicator_mismatch_symbols') or []} "
            f"signal_diff={summary.get('signal_mismatch_symbols') or []} "
            f"bar_failed={summary.get('bar_failed_symbols') or []}"
        )
    else:
        print(f"- Indicator compare: skipped ({indicator.get('skipped_reason') or 'not_run'})")

    print("- Rule: if bar truth fails, do not trust indicator or strategy-alpha conclusions.")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate stored IBKR bars against fresh IBKR bars before checking indicator parity."
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--environment", default=DEFAULT_ENVIRONMENT)
    parser.add_argument("--interval", default=DEFAULT_INTERVAL)
    parser.add_argument("--market-date", default="")
    parser.add_argument("--preset", choices=sorted(SYMBOL_PRESETS), default="expanded")
    parser.add_argument("--symbols", default="")
    parser.add_argument("--symbols-file", default="")
    parser.add_argument("--persist-truth", dest="persist_truth", action="store_true", default=True)
    parser.add_argument("--no-persist-truth", dest="persist_truth", action="store_false")
    parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE)
    parser.add_argument("--scan-scope", default="manual_bar_indicator_audit")
    parser.add_argument("--skip-indicator-compare", action="store_true")
    parser.add_argument("--compare-indicators-on-bar-fail", action="store_true")
    parser.add_argument("--include-signals", dest="include_signals", action="store_true", default=True)
    parser.add_argument("--no-include-signals", dest="include_signals", action="store_false")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--output", default="")
    parser.add_argument("--json-only", action="store_true")
    parser.add_argument("--soft-exit", action="store_true")
    return parser.parse_args(argv)


def build_config(args: argparse.Namespace) -> AuditConfig:
    file_symbols = load_symbols_from_file(args.symbols_file)
    symbols = split_symbols(args.symbols) or file_symbols or list(SYMBOL_PRESETS[args.preset])
    if not symbols:
        raise ValueError("symbols_required")
    market_date = str(args.market_date or "").strip() or latest_complete_market_date()
    return AuditConfig(
        base_url=normalize_base_url(args.base_url),
        environment=str(args.environment or DEFAULT_ENVIRONMENT).strip().lower() or DEFAULT_ENVIRONMENT,
        interval=str(args.interval or DEFAULT_INTERVAL).strip().lower() or DEFAULT_INTERVAL,
        market_date=market_date,
        symbols=tuple(symbols),
        persist_truth=bool(args.persist_truth),
        chunk_size=max(1, int(args.chunk_size or DEFAULT_CHUNK_SIZE)),
        scan_scope=str(args.scan_scope or "manual_bar_indicator_audit").strip() or "manual_bar_indicator_audit",
        skip_indicator_compare=bool(args.skip_indicator_compare),
        compare_indicators_on_bar_fail=bool(args.compare_indicators_on_bar_fail),
        include_signals=bool(args.include_signals),
        timeout_seconds=max(1.0, float(args.timeout or DEFAULT_TIMEOUT_SECONDS)),
    )


def write_result(path: str, result: dict[str, Any]) -> str:
    clean = str(path or "").strip()
    if not clean:
        return ""
    target = Path(clean).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(target)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        config = build_config(args)
        started = time.time()
        result = run_audit(config)
        result["duration_s"] = round(time.time() - started, 3)
        output_path = write_result(args.output, result)
        if args.json_only:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print_human_summary(result)
            if output_path:
                print(f"- Output: {output_path}")
        if result.get("ok") or args.soft_exit:
            return 0
        return 2
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"bar_indicator_audit_failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

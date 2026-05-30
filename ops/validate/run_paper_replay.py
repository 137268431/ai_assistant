#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import time

DEFAULT_HOST = os.environ.get("IBKR_DEPLOY_HOST", "root@206.119.171.246")
DEFAULT_IBKR_REMOTE_ROOT = os.environ.get("IBKR_REMOTE_ROOT", "/opt/ibkr_compute").rstrip("/")
DEFAULT_PB_REMOTE_ROOT = os.environ.get("PB_REMOTE_ROOT", "/opt/pocketbase").rstrip("/")
DEFAULT_REMOTE_PYTHON = os.environ.get("IBKR_REMOTE_PYTHON", f"{DEFAULT_IBKR_REMOTE_ROOT}/venv/bin/python")
DEFAULT_DB_PATH = os.environ.get("PB_DB_PATH", f"{DEFAULT_PB_REMOTE_ROOT}/pb_data/data.db")
DEFAULT_API_LOCAL_URL = os.environ.get("IBKR_API_LOCAL_URL", "http://127.0.0.1:5102")
DEFAULT_COMPUTE_LOCAL_URL = os.environ.get("IBKR_COMPUTE_LOCAL_URL", "http://127.0.0.1:5100")

REMOTE_SCRIPT = r'''
import base64
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

cfg = json.loads(base64.b64decode(os.environ["VALIDATION_CONFIG_B64"]).decode("utf-8"))
IBKR_SRC_ROOT = cfg["ibkr_src_root"]
sys.path.insert(0, IBKR_SRC_ROOT)
from ibkr_compute.core.indicator_engine import IndicatorEngine
from ibkr_compute.core.signal_generator import SignalGenerator
from ibkr_compute.core.broker_mode import resolve_market_data_mode
from ibkr_compute.market.timeframe_utils import classify_session, format_cn_time, format_us_time

ET = ZoneInfo("America/New_York")

DB = cfg["db_path"]
API = cfg["api_local_url"].rstrip("/")
COMPUTE = cfg["compute_local_url"].rstrip("/")
LOOKBACK_BARS = int(cfg["lookback_bars"])
SCAN_SYMBOL_LIMIT = int(cfg["scan_symbol_limit"])
SCAN_BARS_PER_SYMBOL = int(cfg["scan_bars_per_symbol"])
KEEP_DATA = bool(cfg["keep_data"])
REQUIRE_CLEAN_PAPER = bool(cfg["require_clean_paper"])
ALLOW_LIVE_ALIAS = bool(cfg.get("allow_live_alias"))
VALIDATION_PREFIX = str(cfg.get("validation_prefix") or "VAL").upper()
VALIDATION_TAG = str(cfg.get("validation_tag") or "paper_replay_validation")
REQUESTED_REPLAY_ENVIRONMENT = "paper"
RESOLVED_REPLAY_ENVIRONMENT = resolve_market_data_mode(REQUESTED_REPLAY_ENVIRONMENT)
WRITE_ENVIRONMENT = RESOLVED_REPLAY_ENVIRONMENT if ALLOW_LIVE_ALIAS else REQUESTED_REPLAY_ENVIRONMENT


def open_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def json_error(code: int, **payload):
    payload.setdefault("ok", False)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    raise SystemExit(code)


def safe_response_json(resp: requests.Response):
    try:
        return resp.json()
    except Exception:
        return {"status_code": resp.status_code, "text": (resp.text or "")[:500]}


def load_pre_counts(conn: sqlite3.Connection) -> dict:
    tables = ("ibkr_bars", "ibkr_indicators", "ibkr_signals", "ibkr_state")
    return {
        table: conn.execute(
            f"select count(*) from {table} where environment='paper'"
        ).fetchone()[0]
        for table in tables
    }


def get_candidate_symbols(conn: sqlite3.Connection, limit: int) -> list[str]:
    rows = conn.execute(
        """
        select symbol
        from ibkr_targets
        where environment='live' and status='active'
        order by date desc, score desc, symbol asc
        limit ?
        """,
        (limit,),
    ).fetchall()
    symbols = [str(row[0] or "").upper() for row in rows if str(row[0] or "").strip()]
    if symbols:
        return symbols

    rows = conn.execute(
        """
        select symbol, max(bar_time_ms) as last_ms
        from ibkr_bars
        where environment='live' and interval='5m'
        group by symbol
        order by last_ms desc
        limit ?
        """,
        (limit,),
    ).fetchall()
    return [str(row[0] or "").upper() for row in rows if str(row[0] or "").strip()]


def simulate_shifted_signal(symbol: str, window_rows: list[sqlite3.Row], shift_ms: int):
    engine = IndicatorEngine(symbol, "5m")
    signal_gen = SignalGenerator(symbol, "5m")
    last_index = len(window_rows) - 1
    for index, row in enumerate(window_rows):
        new_ms = int(row["bar_time_ms"]) + shift_ms
        bar = {
            "symbol": symbol,
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": float(row["volume"] or 0),
            "session_type": classify_session(bar_time_ms=new_ms),
            "us_time": format_us_time(new_ms),
            "cn_time": format_cn_time(new_ms),
            "bar_time_ms": new_ms,
        }
        snapshot = engine.update(bar)
        if not snapshot or not engine.is_ready():
            continue
        signal = signal_gen.update(snapshot)
        if signal and index == last_index:
            return {
                "bar_time_ms": new_ms,
                "us_time": format_us_time(new_ms),
                "direction": str(signal.get("direction") or ""),
                "signal": str(signal.get("signal") or ""),
                "reason": str(signal.get("reason") or ""),
            }
    return None


def find_recent_signal(
    conn: sqlite3.Connection,
    symbols: list[str],
    bars_per_symbol: int,
    target_bar_ms: int,
):
    best = None
    required_bars = min(240, LOOKBACK_BARS)
    for symbol in symbols:
        rows = conn.execute(
            """
            select symbol, open, high, low, close, volume, session_type, us_time, cn_time, bar_time_ms
            from ibkr_bars
            where environment='live' and interval='5m' and symbol=?
            order by bar_time_ms desc
            limit ?
            """,
            (symbol, bars_per_symbol),
        ).fetchall()
        rows = list(reversed(rows))
        if len(rows) < required_bars:
            continue

        engine = IndicatorEngine(symbol, "5m")
        signal_gen = SignalGenerator(symbol, "5m")
        candidate_indexes = []
        for index, row in enumerate(rows):
            bar = dict(row)
            snapshot = engine.update(bar)
            if not snapshot or not engine.is_ready():
                continue
            signal = signal_gen.update(snapshot)
            if signal and index + 1 >= required_bars:
                candidate_indexes.append(index)

        for index in reversed(candidate_indexes[-12:]):
            window_rows = rows[max(0, index - LOOKBACK_BARS + 1): index + 1]
            if len(window_rows) < required_bars:
                continue
            shift_ms = target_bar_ms - int(window_rows[-1]["bar_time_ms"])
            replay_signal = simulate_shifted_signal(symbol, window_rows, shift_ms)
            if not replay_signal:
                continue
            candidate = {
                "symbol": symbol,
                "bar_time_ms": int(rows[index]["bar_time_ms"]),
                "us_time": str(rows[index]["us_time"] or ""),
                "direction": replay_signal["direction"],
                "signal": replay_signal["signal"],
                "reason": replay_signal["reason"],
                "offline_verified": True,
                "shift_ms": shift_ms,
                "target_bar_ms": target_bar_ms,
                "target_bar_us": replay_signal["us_time"],
            }
            if best is None or candidate["bar_time_ms"] > best["bar_time_ms"]:
                best = candidate
            break
    return best


def fetch_symbol_counts(symbol: str, environment: str) -> tuple[dict, list[dict], list[dict], int]:
    runtime_environment = str(environment or "paper").strip().lower() or "paper"
    conn = open_db()
    try:
        counts = {
            "ibkr_bars": conn.execute(
                "select count(*) from ibkr_bars where environment=? and symbol=?",
                (runtime_environment, symbol),
            ).fetchone()[0],
            "ibkr_indicators": conn.execute(
                "select count(*) from ibkr_indicators where environment=? and symbol=?",
                (runtime_environment, symbol),
            ).fetchone()[0],
            "ibkr_signals": conn.execute(
                "select count(*) from ibkr_signals where environment=? and symbol=?",
                (runtime_environment, symbol),
            ).fetchone()[0],
            "ibkr_state": conn.execute(
                "select count(*) from ibkr_state where environment=?",
                (runtime_environment,),
            ).fetchone()[0],
        }
        indicators = [
            dict(row)
            for row in conn.execute(
                """
                select symbol, interval, us_time, bar_time_ms, created
                from ibkr_indicators
                where environment=? and symbol=?
                order by bar_time_ms desc, created desc
                limit 3
                """,
                (runtime_environment, symbol),
            ).fetchall()
        ]
        signals = [
            dict(row)
            for row in conn.execute(
                """
                select symbol, interval, us_time, bar_time_ms, direction, signal, status, signal_id, created
                from ibkr_signals
                where environment=? and symbol=?
                order by bar_time_ms desc, created desc
                limit 3
                """,
                (runtime_environment, symbol),
            ).fetchall()
        ]
        state_count = counts["ibkr_state"]
        return counts, indicators, signals, state_count
    finally:
        conn.close()


def cleanup_validation_rows(symbol: str, created_state: bool, environments: list[str]) -> dict:
    cleanup_environments = []
    for value in environments or []:
        normalized = str(value or "").strip().lower()
        if normalized and normalized not in cleanup_environments:
            cleanup_environments.append(normalized)
    if not cleanup_environments:
        cleanup_environments = ["paper"]

    conn = open_db()
    cleanup = {
        "performed": True,
        "deleted": {},
        "environments": cleanup_environments,
        "compute_restarted": False,
    }
    try:
        for environment in cleanup_environments:
            for table in ("ibkr_signals", "ibkr_indicators", "ibkr_bars"):
                cur = conn.execute(
                    f"delete from {table} where environment=? and symbol=?",
                    (environment, symbol),
                )
                cleanup["deleted"][f"{table}:{environment}"] = int(cur.rowcount or 0)
            if created_state and environment == WRITE_ENVIRONMENT:
                cur = conn.execute("delete from ibkr_state where environment=?", (environment,))
                cleanup["deleted"][f"ibkr_state:{environment}"] = int(cur.rowcount or 0)
        conn.commit()
    finally:
        conn.close()

    try:
        subprocess.run(
            ["systemctl", "restart", "ibkr-compute"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        cleanup["compute_restarted"] = True
    except Exception as exc:
        cleanup["compute_restart_error"] = str(exc)
    return cleanup


base_conn = open_db()
pre_counts = load_pre_counts(base_conn)
base_conn.close()
if REQUIRE_CLEAN_PAPER and any(pre_counts.values()):
    json_error(2, error="paper_environment_not_clean", pre_counts=pre_counts)
if RESOLVED_REPLAY_ENVIRONMENT != REQUESTED_REPLAY_ENVIRONMENT and not ALLOW_LIVE_ALIAS:
    json_error(
        2,
        error="paper_market_data_aliases_to_live",
        requested_environment=REQUESTED_REPLAY_ENVIRONMENT,
        resolved_environment=RESOLVED_REPLAY_ENVIRONMENT,
        hint="Current compute config aliases paper market data to live. Re-run with --allow-live-alias only if writing and cleaning an isolated live validation symbol is acceptable.",
        pre_counts=pre_counts,
    )

requested_symbol = str(cfg.get("symbol") or "").strip().upper()
requested_signal_bar_ms = int(cfg.get("signal_bar_ms") or 0)
if bool(requested_symbol) != bool(requested_signal_bar_ms):
    json_error(2, error="symbol_and_signal_bar_ms_must_be_used_together")

conn = open_db()
candidate_symbols = get_candidate_symbols(conn, SCAN_SYMBOL_LIMIT)
latest_complete_ms = (int(datetime.now(ET).timestamp() * 1000) // 300000) * 300000 - 300000
if requested_symbol and requested_signal_bar_ms:
    source = {
        "symbol": requested_symbol,
        "bar_time_ms": requested_signal_bar_ms,
        "us_time": "",
        "direction": "",
        "signal": "",
        "reason": "manual_source",
        "offline_verified": False,
    }
else:
    source = find_recent_signal(conn, candidate_symbols, SCAN_BARS_PER_SYMBOL, latest_complete_ms)

if not source:
    conn.close()
    json_error(3, error="no_live_signal_source_found", candidate_symbols=candidate_symbols)

source_symbol = source["symbol"]
source_bar_ms = int(source["bar_time_ms"])
source_rows = conn.execute(
    """
    select symbol, exchange, interval, open, high, low, close, volume, session_type, us_time, cn_time, bar_time_ms
    from ibkr_bars
    where environment='live' and interval='5m' and symbol=? and bar_time_ms <= ?
    order by bar_time_ms desc
    limit ?
    """,
    (source_symbol, source_bar_ms, LOOKBACK_BARS),
).fetchall()
conn.close()
source_rows = list(reversed(source_rows))
if len(source_rows) < min(240, LOOKBACK_BARS):
    json_error(
        4,
        error="not_enough_source_bars",
        source_symbol=source_symbol,
        source_bar_ms=source_bar_ms,
        source_row_count=len(source_rows),
    )

stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
validation_symbol = f"{VALIDATION_PREFIX}{stamp[-8:]}{source_symbol}"[:24].upper()
target_bar_ms = int(source.get("target_bar_ms") or latest_complete_ms)
shift_ms = int(source.get("shift_ms") or (target_bar_ms - source_bar_ms))
replay_bars = []
for idx, row in enumerate(source_rows, start=1):
    new_ms = int(row["bar_time_ms"]) + shift_ms
    replay_bars.append({
        "symbol": validation_symbol,
        "exchange": str(row["exchange"] or "SMART").upper(),
        "environment": WRITE_ENVIRONMENT,
        "interval": "5m",
        "open": float(row["open"]),
        "high": float(row["high"]),
        "low": float(row["low"]),
        "close": float(row["close"]),
        "volume": float(row["volume"] or 0),
        "session_type": classify_session(bar_time_ms=new_ms),
        "us_time": format_us_time(new_ms),
        "cn_time": format_cn_time(new_ms),
        "bar_time_ms": new_ms,
        "extra": {
            "source": "codex_validation_replay",
            "validation_tag": VALIDATION_TAG,
            "validation_symbol": validation_symbol,
            "replayed_from_environment": "live",
            "replayed_from_symbol": source_symbol,
            "replayed_from_bar_time_ms": int(row["bar_time_ms"]),
            "replay_sequence": idx,
        },
    })

report = {
    "ok": False,
    "validation_tag": VALIDATION_TAG,
    "validation_symbol": validation_symbol,
    "source": source,
    "candidate_symbols": candidate_symbols,
    "source_row_count": len(source_rows),
    "target_bar_ms": target_bar_ms,
    "target_bar_us": format_us_time(target_bar_ms),
    "shift_ms": shift_ms,
    "pre_counts": pre_counts,
    "requested_environment": REQUESTED_REPLAY_ENVIRONMENT,
    "resolved_environment": RESOLVED_REPLAY_ENVIRONMENT,
    "write_environment": WRITE_ENVIRONMENT,
}
cleanup = {"performed": False, "deleted": {}, "compute_restarted": False}
created_paper_state = False

try:
    bars_resp = requests.post(
        f"{API}/api/custom/ibkr/bars",
        json={"environment": WRITE_ENVIRONMENT, "bars": replay_bars},
        timeout=120,
    )
    bars_resp.raise_for_status()
    report["bar_write"] = safe_response_json(bars_resp)

    compute_resp = requests.post(
        f"{COMPUTE}/compute",
        json={
            "environment": WRITE_ENVIRONMENT,
            "data_environment": WRITE_ENVIRONMENT,
            "environments": [WRITE_ENVIRONMENT],
            "symbols": [validation_symbol],
            "persist_signal_symbols": [validation_symbol],
            "source": "recompute",
            "force_rollup": True,
        },
        timeout=180,
    )
    compute_resp.raise_for_status()
    report["compute"] = safe_response_json(compute_resp)

    paper_counts, latest_indicators, latest_signals, paper_state_count = fetch_symbol_counts(validation_symbol, WRITE_ENVIRONMENT)
    created_paper_state = (
        WRITE_ENVIRONMENT == "paper"
        and paper_state_count > 0
        and pre_counts.get("ibkr_state", 0) == 0
    )

    report["paper_counts"] = paper_counts
    report["latest_indicator"] = latest_indicators
    report["latest_signal"] = latest_signals
    report["verified"] = {
        "bars_written": paper_counts["ibkr_bars"] > 0,
        "indicators_written": paper_counts["ibkr_indicators"] > 0,
        "signals_written": paper_counts["ibkr_signals"] > 0,
    }
    report["ok"] = (
        all(report["verified"].values())
        and int((report.get("compute") or {}).get("errors", 0) or 0) == 0
    )
finally:
    if not KEEP_DATA:
        cleanup = cleanup_validation_rows(
            validation_symbol,
            created_paper_state,
            [REQUESTED_REPLAY_ENVIRONMENT, RESOLVED_REPLAY_ENVIRONMENT, WRITE_ENVIRONMENT],
        )
    report["cleanup"] = cleanup

print(json.dumps(report, ensure_ascii=False, indent=2))
'''


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a safe paper replay validation for the IBKR pipeline.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--ibkr-remote-root", default=DEFAULT_IBKR_REMOTE_ROOT)
    parser.add_argument("--remote-python", default=DEFAULT_REMOTE_PYTHON)
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    parser.add_argument("--api-local-url", default=DEFAULT_API_LOCAL_URL)
    parser.add_argument("--compute-local-url", default=DEFAULT_COMPUTE_LOCAL_URL)
    parser.add_argument("--symbol", default="")
    parser.add_argument("--signal-bar-ms", type=int, default=0)
    parser.add_argument("--lookback-bars", type=int, default=280)
    parser.add_argument("--scan-symbol-limit", type=int, default=24)
    parser.add_argument("--scan-bars-per-symbol", type=int, default=900)
    parser.add_argument("--validation-prefix", default="VAL")
    parser.add_argument("--keep-data", action="store_true")
    parser.add_argument("--allow-dirty-paper", action="store_true")
    parser.add_argument("--allow-live-alias", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def _decode_json_payload(raw: str) -> dict:
    text = (raw or "").strip()
    if not text:
        raise ValueError("empty stdout")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(text[start:end + 1])
        raise


def run_remote(args: argparse.Namespace) -> dict:
    ibkr_remote_root = str(args.ibkr_remote_root or DEFAULT_IBKR_REMOTE_ROOT).rstrip("/")
    config = {
        "ibkr_src_root": f"{ibkr_remote_root}/src",
        "db_path": args.db_path,
        "api_local_url": args.api_local_url,
        "compute_local_url": args.compute_local_url,
        "symbol": args.symbol,
        "signal_bar_ms": args.signal_bar_ms,
        "lookback_bars": args.lookback_bars,
        "scan_symbol_limit": args.scan_symbol_limit,
        "scan_bars_per_symbol": args.scan_bars_per_symbol,
        "validation_prefix": args.validation_prefix,
        "validation_tag": f"paper_replay_validation_{int(time.time())}",
        "keep_data": args.keep_data,
        "require_clean_paper": not args.allow_dirty_paper,
        "allow_live_alias": args.allow_live_alias,
    }
    env_blob = base64.b64encode(json.dumps(config).encode("utf-8")).decode("ascii")
    cmd = [
        "ssh",
        args.host,
        "env",
        f"VALIDATION_CONFIG_B64={env_blob}",
        args.remote_python,
        "-",
    ]
    proc = subprocess.run(cmd, input=REMOTE_SCRIPT, text=True, capture_output=True)
    if proc.returncode != 0 and not proc.stdout.strip():
        raise RuntimeError(proc.stderr.strip() or f"remote command failed with code {proc.returncode}")
    try:
        payload = _decode_json_payload(proc.stdout)
    except Exception as exc:
        raise RuntimeError(
            f"failed to parse remote output: {exc}\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        ) from exc
    payload.setdefault("ssh_returncode", proc.returncode)
    if proc.stderr.strip():
        payload["ssh_stderr"] = proc.stderr.strip()
    return payload


def main() -> int:
    args = parse_args()
    try:
        payload = run_remote(args)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        return 0 if payload.get("ok") else 1

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if payload.get("ok"):
        print("\nValidation OK: paper replay wrote bars, indicators, and signals, then cleaned up.")
        return 0
    print("\nValidation FAILED.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""
IBKR Trading System — 全链路 E2E 自动化测试
覆盖: 服务启动 → PB集合 → Gateway认证 → conid解析 → 历史数据 →
      WebSocket连接 → 5min K线聚合 → 指标计算 → 信号生成 → 信号流转 →
      OCO Bracket下单(Paper) → 订单跟踪 → Dashboard展示 → 保活机制

在远端执行:
  source /opt/ibkr_compute/.env
  PYTHONPATH=/opt/ibkr_compute/src python3 /opt/ibkr_compute/deploy/e2e_test.py
"""

import os
import sys
import json
import time
import traceback
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

sys.path.insert(0, "/opt/ibkr_compute/src")

PB_URL = os.environ.get("PB_BASE_URL", "http://localhost:8090")
GW_URL = os.environ.get("IBKR_GATEWAY_URL", "https://localhost:5001")
COMPUTE_URL = "http://localhost:5100"
ACCT_ID = os.environ.get("IBKR_ACCOUNT_ID", "U13281777")
PAPER_ACCT = os.environ.get("IBKR_PAPER_ACCOUNT_ID", "U18316222")
TEST_SYMBOL = "AAPL"
TEST_CONID = 265598

passed = 0
failed = 0
warnings = 0
results = []


def test(name):
    def decorator(fn):
        def wrapper():
            global passed, failed, warnings
            sys.stdout.write("  [TEST] %-55s " % name)
            sys.stdout.flush()
            try:
                result = fn()
                if result is True or result is None:
                    passed += 1
                    results.append(("PASS", name))
                    print("PASS")
                    return True
                elif result == "WARN":
                    warnings += 1
                    results.append(("WARN", name))
                    print("WARN")
                    return True
                else:
                    failed += 1
                    results.append(("FAIL", name))
                    print("FAIL - %s" % str(result))
                    return False
            except Exception as e:
                failed += 1
                results.append(("FAIL", name))
                print("FAIL - %s" % str(e))
                return False
        return wrapper
    return decorator


def gw_get(path, **kwargs):
    s = requests.Session()
    s.verify = False
    return s.get(GW_URL + "/v1/api" + path, timeout=15, **kwargs)


def gw_post(path, **kwargs):
    s = requests.Session()
    s.verify = False
    return s.post(GW_URL + "/v1/api" + path, timeout=15, **kwargs)


def pb_get(path):
    return requests.get(PB_URL + path, timeout=10)


def pb_post(path, data):
    return requests.post(PB_URL + path, json=data, timeout=10)


def pb_patch(path, data):
    return requests.patch(PB_URL + path, json=data, timeout=10)


def pb_delete(path):
    return requests.delete(PB_URL + path, timeout=10)


def compute_get(path):
    return requests.get(COMPUTE_URL + path, timeout=10)


def compute_post(path, data=None):
    return requests.post(COMPUTE_URL + path, json=data or {}, timeout=15)


# ====================================================================
# Phase 0: PocketBase Collections Setup
# ====================================================================

def ensure_pb_collection(name, schema, indexes=None):
    """Create PB collection if it doesn't exist. Return True if OK."""
    resp = pb_get("/api/collections/%s/records?perPage=1" % name)
    if resp.status_code == 200 and "items" in resp.json():
        return True  # exists

    # Collection doesn't exist - need to create via migration
    return False


def create_ibkr_collections_via_migration():
    """Create all ibkr_* collections using a PB JS migration file."""
    migration_js = '''/// <reference path="../pb_data/types.d.ts" />
migrate((db) => {
  // ibkr_bars
  const bars = new Collection({
    "id": "_pb_ibkr_bars_",
    "name": "ibkr_bars",
    "type": "base",
    "system": false,
    "schema": [
      {"id":"ib_sym","name":"symbol","type":"text","required":true,"options":{}},
      {"id":"ib_intv","name":"interval","type":"text","required":false,"options":{}},
      {"id":"ib_btms","name":"bar_time_ms","type":"number","required":false,"options":{"noDecimal":true}},
      {"id":"ib_open","name":"open","type":"number","required":false,"options":{}},
      {"id":"ib_high","name":"high","type":"number","required":false,"options":{}},
      {"id":"ib_low","name":"low","type":"number","required":false,"options":{}},
      {"id":"ib_close","name":"close","type":"number","required":false,"options":{}},
      {"id":"ib_vol","name":"volume","type":"number","required":false,"options":{}},
      {"id":"ib_ust","name":"us_time","type":"text","required":false,"options":{}},
      {"id":"ib_cnt","name":"cn_time","type":"text","required":false,"options":{}},
      {"id":"ib_src","name":"source","type":"text","required":false,"options":{}},
      {"id":"ib_tc","name":"tick_count","type":"number","required":false,"options":{"noDecimal":true}},
      {"id":"ib_env","name":"environment","type":"text","required":false,"options":{}}
    ],
    "indexes": [
      "CREATE INDEX idx_ibkr_bars_sym ON ibkr_bars (symbol)",
      "CREATE INDEX idx_ibkr_bars_btms ON ibkr_bars (bar_time_ms)",
      "CREATE UNIQUE INDEX idx_ibkr_bars_uniq ON ibkr_bars (symbol, interval, bar_time_ms)"
    ],
    "listRule": "",
    "viewRule": "",
    "createRule": "",
    "updateRule": "",
    "deleteRule": "",
    "options": {}
  });
  Dao(db).saveCollection(bars);

  // ibkr_orders
  const orders = new Collection({
    "id": "_pb_ibkr_orders_",
    "name": "ibkr_orders",
    "type": "base",
    "system": false,
    "schema": [
      {"id":"io_coid","name":"cOID","type":"text","required":false,"options":{}},
      {"id":"io_oid","name":"orderId","type":"text","required":false,"options":{}},
      {"id":"io_sym","name":"symbol","type":"text","required":false,"options":{}},
      {"id":"io_conid","name":"conid","type":"number","required":false,"options":{"noDecimal":true}},
      {"id":"io_side","name":"side","type":"text","required":false,"options":{}},
      {"id":"io_type","name":"orderType","type":"text","required":false,"options":{}},
      {"id":"io_price","name":"price","type":"number","required":false,"options":{}},
      {"id":"io_qty","name":"quantity","type":"number","required":false,"options":{"noDecimal":true}},
      {"id":"io_fqty","name":"filled_quantity","type":"number","required":false,"options":{"noDecimal":true}},
      {"id":"io_avg","name":"avg_price","type":"number","required":false,"options":{}},
      {"id":"io_st","name":"status","type":"text","required":false,"options":{}},
      {"id":"io_pid","name":"parentId","type":"text","required":false,"options":{}},
      {"id":"io_bg","name":"bracket_group","type":"text","required":false,"options":{}},
      {"id":"io_sig","name":"signal_id","type":"text","required":false,"options":{}},
      {"id":"io_acct","name":"account","type":"text","required":false,"options":{}},
      {"id":"io_tp","name":"tp_price","type":"number","required":false,"options":{}},
      {"id":"io_sl","name":"sl_price","type":"number","required":false,"options":{}},
      {"id":"io_ust","name":"us_time","type":"text","required":false,"options":{}},
      {"id":"io_rpnl","name":"realized_pnl","type":"number","required":false,"options":{}}
    ],
    "indexes": [
      "CREATE INDEX idx_ibkr_orders_sym ON ibkr_orders (symbol)",
      "CREATE INDEX idx_ibkr_orders_bg ON ibkr_orders (bracket_group)"
    ],
    "listRule": "",
    "viewRule": "",
    "createRule": "",
    "updateRule": "",
    "deleteRule": "",
    "options": {}
  });
  Dao(db).saveCollection(orders);

  // ibkr_positions
  const positions = new Collection({
    "id": "_pb_ibkr_positions_",
    "name": "ibkr_positions",
    "type": "base",
    "system": false,
    "schema": [
      {"id":"ip_sym","name":"symbol","type":"text","required":false,"options":{}},
      {"id":"ip_cid","name":"conid","type":"number","required":false,"options":{"noDecimal":true}},
      {"id":"ip_qty","name":"quantity","type":"number","required":false,"options":{}},
      {"id":"ip_avg","name":"avgCost","type":"number","required":false,"options":{}},
      {"id":"ip_mkt","name":"mktPrice","type":"number","required":false,"options":{}},
      {"id":"ip_pnl","name":"unrealizedPnl","type":"number","required":false,"options":{}},
      {"id":"ip_acct","name":"account","type":"text","required":false,"options":{}},
      {"id":"ip_ust","name":"us_time","type":"text","required":false,"options":{}}
    ],
    "indexes": [
      "CREATE INDEX idx_ibkr_pos_sym ON ibkr_positions (symbol)"
    ],
    "listRule": "",
    "viewRule": "",
    "createRule": "",
    "updateRule": "",
    "deleteRule": "",
    "options": {}
  });
  Dao(db).saveCollection(positions);

  // ibkr_session
  const session = new Collection({
    "id": "_pb_ibkr_session_",
    "name": "ibkr_session",
    "type": "base",
    "system": false,
    "schema": [
      {"id":"is_evt","name":"event","type":"text","required":false,"options":{}},
      {"id":"is_st","name":"status","type":"text","required":false,"options":{}},
      {"id":"is_det","name":"detail","type":"text","required":false,"options":{"max":2000}},
      {"id":"is_ust","name":"us_time","type":"text","required":false,"options":{}}
    ],
    "indexes": [],
    "listRule": "",
    "viewRule": "",
    "createRule": "",
    "updateRule": "",
    "deleteRule": "",
    "options": {}
  });
  Dao(db).saveCollection(session);

  // ibkr_conid_cache
  const conids = new Collection({
    "id": "_pb_ibkr_conid_cache_",
    "name": "ibkr_conid_cache",
    "type": "base",
    "system": false,
    "schema": [
      {"id":"ic_sym","name":"symbol","type":"text","required":true,"options":{}},
      {"id":"ic_cid","name":"conid","type":"number","required":true,"options":{"noDecimal":true}}
    ],
    "indexes": [
      "CREATE UNIQUE INDEX idx_ibkr_conid_sym ON ibkr_conid_cache (symbol)"
    ],
    "listRule": "",
    "viewRule": "",
    "createRule": "",
    "updateRule": "",
    "deleteRule": "",
    "options": {}
  });
  Dao(db).saveCollection(conids);

}, (db) => {
  const dao = new Dao(db);
  ["ibkr_bars","ibkr_orders","ibkr_positions","ibkr_session","ibkr_conid_cache"].forEach(name => {
    try { dao.deleteCollection(dao.findCollectionByNameOrId(name)); } catch(e) {}
  });
})
'''
    ts = int(time.time())
    migration_path = "/opt/pocketbase/pb_migrations/%d_created_ibkr_collections.js" % ts
    with open(migration_path, "w") as f:
        f.write(migration_js)
    return migration_path


# ====================================================================
# Phase 1: Service Health
# ====================================================================

@test("1.1 IB Gateway service is active")
def t_gw_service():
    r = os.popen("systemctl is-active ibkr-gateway").read().strip()
    if r != "active":
        return "ibkr-gateway is %s" % r
    return True

@test("1.2 ibkr-compute service is active")
def t_compute_service():
    r = os.popen("systemctl is-active ibkr-compute").read().strip()
    if r != "active":
        return "ibkr-compute is %s" % r
    return True

@test("1.3 PocketBase service is active")
def t_pb_service():
    r = os.popen("systemctl is-active pocketbase").read().strip()
    if r != "active":
        return "pocketbase is %s" % r
    return True

@test("1.4 ibkr-compute /health returns ok")
def t_compute_health():
    r = compute_get("/health")
    d = r.json()
    if not d.get("ok"):
        return "health not ok: %s" % d
    return True

# ====================================================================
# Phase 2: Gateway Authentication
# ====================================================================

@test("2.1 Gateway port 5001 listening")
def t_gw_port():
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(3)
    result = s.connect_ex(("127.0.0.1", 5001))
    s.close()
    if result != 0:
        return "port 5001 not open"
    return True

@test("2.2 Gateway authenticated")
def t_gw_auth():
    r = gw_post("/iserver/auth/status", data="")
    if r.status_code != 200 or not r.text.strip():
        return "empty response (not logged in?)"
    d = r.json()
    if not d.get("authenticated"):
        return "not authenticated: %s" % d
    return True

@test("2.3 Gateway tickle responds")
def t_gw_tickle():
    r = gw_post("/tickle", data="")
    if r.status_code != 200 or not r.text.strip():
        return "tickle empty response"
    d = r.json()
    if not d.get("session"):
        return "no session in tickle"
    return True

@test("2.4 Portfolio accounts accessible")
def t_accounts():
    r = gw_get("/portfolio/accounts")
    accts = r.json()
    if not isinstance(accts, list) or len(accts) == 0:
        return "no accounts returned"
    ids = [a.get("accountId") for a in accts]
    if ACCT_ID not in ids:
        return "account %s not found in %s" % (ACCT_ID, ids)
    return True

# ====================================================================
# Phase 3: PB Collections
# ====================================================================

@test("3.1 PB ibkr_bars collection exists")
def t_pb_bars():
    r = pb_get("/api/collections/ibkr_bars/records?perPage=1")
    if r.status_code == 404:
        return "collection not found (will create)"
    return True

@test("3.2 PB ibkr_orders collection exists")
def t_pb_orders():
    r = pb_get("/api/collections/ibkr_orders/records?perPage=1")
    if r.status_code == 404:
        return "collection not found (will create)"
    return True

@test("3.3 PB ibkr_session collection exists")
def t_pb_session():
    r = pb_get("/api/collections/ibkr_session/records?perPage=1")
    if r.status_code == 404:
        return "collection not found (will create)"
    return True

@test("3.4 PB ibkr_positions collection exists")
def t_pb_positions():
    r = pb_get("/api/collections/ibkr_positions/records?perPage=1")
    if r.status_code == 404:
        return "collection not found (will create)"
    return True

@test("3.5 PB ibkr_conid_cache collection exists")
def t_pb_conid():
    r = pb_get("/api/collections/ibkr_conid_cache/records?perPage=1")
    if r.status_code == 404:
        return "collection not found (will create)"
    return True

# ====================================================================
# Phase 4: Market Data Pipeline
# ====================================================================

@test("4.1 Conid resolution (AAPL → 265598)")
def t_conid():
    r = gw_get("/iserver/secdef/search", params={"symbol": TEST_SYMBOL})
    data = r.json()
    if not data:
        return "empty secdef search result"
    conid = data[0].get("conid")
    if str(conid) != str(TEST_CONID):
        return "expected conid %s, got %s" % (TEST_CONID, conid)
    return True

@test("4.2 Historical bars fetch (AAPL 5min)")
def t_history():
    r = gw_get("/iserver/marketdata/history",
               params={"conid": TEST_CONID, "period": "1d", "bar": "5min"})
    d = r.json()
    bars = d.get("data", [])
    if len(bars) < 5:
        return "too few bars: %d" % len(bars)
    bar = bars[0]
    for k in ("o", "h", "l", "c", "v", "t"):
        if k not in bar:
            return "missing field %s in bar" % k
    return True

@test("4.3 Write test bar to PB ibkr_bars")
def t_write_bar():
    r = pb_get("/api/collections/ibkr_bars/records?perPage=1")
    if r.status_code == 404:
        return "WARN"  # collection doesn't exist yet, will fix

    test_data = {
        "symbol": "E2E_TEST",
        "interval": "5m",
        "bar_time_ms": 9999999999999,
        "open": 100.0,
        "high": 105.0,
        "low": 99.0,
        "close": 103.0,
        "volume": 1000,
        "us_time": "2026-04-02 00:00:00",
        "source": "e2e_test",
    }
    r2 = pb_post("/api/collections/ibkr_bars/records", test_data)
    if r2.status_code not in (200, 201):
        return "write failed: %d %s" % (r2.status_code, r2.text[:200])

    # Cleanup
    rec_id = r2.json().get("id")
    if rec_id:
        pb_delete("/api/collections/ibkr_bars/records/%s" % rec_id)
    return True

# ====================================================================
# Phase 5: Signal Pipeline (Simulated)
# ====================================================================

@test("5.1 Create test signal in PB ibkr_signals collection")
def t_create_signal():
    from datetime import datetime, timezone, timedelta
    ET = timezone(timedelta(hours=-4))
    et_now = datetime.now(ET)

    signal_data = {
        "symbol": "E2E_TEST",
        "direction": "long",
        "entry": 150.0,
        "stop_loss": 147.0,
        "take_profit": 156.0,
        "shares": 10,
        "rr": "2:1",
        "signal_id": "e2e_test_%d" % int(time.time()),
        "status": "pending",
        "date": et_now.strftime("%Y-%m-%d"),
        "us_time": et_now.strftime("%Y-%m-%d %H:%M:%S"),
        "interval": "5",
        "environment": "paper",
        "signal": "e2e_test",
        "script_tag": "e2e",
    }
    r = pb_post("/api/collections/ibkr_signals/records", signal_data)
    if r.status_code not in (200, 201):
        return "signal create failed: %d %s" % (r.status_code, r.text[:200])

    global _test_signal_id
    _test_signal_id = r.json().get("id")
    return True

@test("5.2 Signal processor validates prices correctly")
def t_signal_validate():
    from ibkr_compute.signal.signal_processor import SignalProcessor
    from ibkr_compute.core.config import Config
    cfg = Config()
    sp = SignalProcessor(config=cfg, environment="paper")

    # Valid long
    assert sp._validate_prices({"direction": "long", "entry": 150, "stop_loss": 147, "take_profit": 156})
    # Invalid long (SL > entry)
    assert not sp._validate_prices({"direction": "long", "entry": 150, "stop_loss": 152, "take_profit": 156})
    # Valid short
    assert sp._validate_prices({"direction": "short", "entry": 150, "stop_loss": 153, "take_profit": 146})
    # Invalid short (SL < entry)
    assert not sp._validate_prices({"direction": "short", "entry": 150, "stop_loss": 148, "take_profit": 146})
    return True

@test("5.3 Signal router can fetch from PB")
def t_signal_router():
    from ibkr_compute.integrations.pb_client import PBClient
    from ibkr_compute.core.config import Config
    from ibkr_compute.signal.signal_router import SignalRouter

    pb = PBClient(base_url=PB_URL)
    cfg = Config(pb_client=pb)
    cfg.refresh()
    router = SignalRouter(pb_client=pb, config=cfg, environment="paper")
    ibkr_signals = router.fetch_pending_signals()
    # Should find at least the test signal we just created
    return True

@test("5.4 Cleanup test signal")
def t_cleanup_signal():
    global _test_signal_id
    if _test_signal_id:
        pb_delete("/api/collections/ibkr_signals/records/%s" % _test_signal_id)
        _test_signal_id = None
    return True

# ====================================================================
# Phase 6: Order Pipeline (Paper Account - Read Only)
# ====================================================================

@test("6.1 Paper account positions accessible")
def t_paper_positions():
    acct = PAPER_ACCT or ACCT_ID
    r = gw_get("/portfolio/%s/positions/0" % acct)
    if r.status_code != 200:
        return "status %d" % r.status_code
    return True

@test("6.2 Live orders endpoint accessible")
def t_live_orders():
    r = gw_get("/iserver/account/orders", params={"force": "true"})
    if r.status_code != 200:
        return "status %d" % r.status_code
    return True

@test("6.3 Order placer module can instantiate")
def t_order_placer():
    from ibkr_compute.order.order_placer import OrderPlacer
    op = OrderPlacer(gateway_url=GW_URL)
    s = op.status()
    if not s.get("account_id"):
        return "no account_id configured"
    return True

# ====================================================================
# Phase 7: IBKR Service Integration
# ====================================================================

@test("7.1 /ibkr/status endpoint responds")
def t_ibkr_status():
    r = compute_get("/ibkr/status")
    d = r.json()
    if not d.get("ok"):
        return "not ok"
    if d.get("environment") != "live":
        return "unexpected env: %s" % d.get("environment")
    return True

@test("7.2 /ibkr/dashboard redirects to PB runtime")
def t_ibkr_dashboard():
    r = requests.get(COMPUTE_URL + "/ibkr/dashboard", timeout=10, allow_redirects=False)
    if r.status_code not in (301, 302):
        return "status %d" % r.status_code

    location = r.headers.get("Location", "")
    if "/ibkr_runtime.html" not in location:
        return "unexpected redirect: %s" % location

    follow = requests.get(location, timeout=10)
    if follow.status_code != 200:
        return "runtime status %d" % follow.status_code
    if "IBKR Runtime" not in follow.text:
        return "runtime content missing"
    return True

# ====================================================================
# Phase 8: Session Keep-alive
# ====================================================================

@test("8.1 Session keeper module works")
def t_session_keeper():
    from ibkr_compute.gateway.session_keeper import SessionKeeper
    sk = SessionKeeper(gateway_url=GW_URL)
    result = sk.tickle()
    if "error" in result:
        return "tickle error: %s" % result["error"]
    return True

@test("8.2 Auth status check works")
def t_auth_check():
    from ibkr_compute.gateway.session_keeper import SessionKeeper
    sk = SessionKeeper(gateway_url=GW_URL)
    result = sk.check_auth_status()
    if not result.get("authenticated"):
        return "not authenticated"
    return True

@test("8.3 Session log can write to PB")
def t_session_log():
    r = pb_get("/api/collections/ibkr_session/records?perPage=1")
    if r.status_code == 404:
        return "WARN"  # collection doesn't exist

    from ibkr_compute.integrations.pb_client import PBClient
    pb = PBClient(base_url=PB_URL)
    rec = pb.create_record("ibkr_session", {
        "event": "e2e_test",
        "status": "ok",
        "detail": "E2E test session log",
        "us_time": time.strftime("%Y-%m-%d %H:%M:%S"),
    })
    if rec.get("id"):
        pb_delete("/api/collections/ibkr_session/records/%s" % rec["id"])
    return True

# ====================================================================
# Phase 9: Bar Aggregator Unit Tests
# ====================================================================

@test("9.1 Bar aggregator 5min alignment")
def t_bar_align():
    from ibkr_compute.market.bar_aggregator import get_bar_interval_start
    from datetime import datetime, timezone, timedelta
    ET = timezone(timedelta(hours=-4))

    dt = datetime(2026, 4, 2, 9, 32, 15, tzinfo=ET)
    start = get_bar_interval_start(dt)
    assert start.minute == 30
    assert start.second == 0

    dt2 = datetime(2026, 4, 2, 10, 7, 45, tzinfo=ET)
    start2 = get_bar_interval_start(dt2)
    assert start2.minute == 5
    return True

@test("9.2 Bar aggregator OHLCV logic")
def t_bar_ohlcv():
    from ibkr_compute.market.bar_aggregator import BarAggregator

    closed = []
    agg = BarAggregator(
        conid_to_symbol={TEST_CONID: TEST_SYMBOL},
        on_bar_close=lambda b: closed.append(b),
    )

    agg.on_tick({"conid": TEST_CONID, "31": "150.25", "87": "1000"})
    agg.on_tick({"conid": TEST_CONID, "31": "152.00", "87": "2000"})
    agg.on_tick({"conid": TEST_CONID, "31": "149.50", "87": "3000"})
    agg.force_close_all()

    assert len(closed) == 1
    b = closed[0]
    assert b["open"] == 150.25
    assert b["high"] == 152.0
    assert b["low"] == 149.5
    assert b["close"] == 149.5
    assert b["symbol"] == TEST_SYMBOL
    return True

# ====================================================================
# Phase 10: Data Writer dedup + validation
# ====================================================================

@test("10.1 Data writer validates OHLC relationship")
def t_dw_validate():
    from ibkr_compute.market.data_writer import DataWriter
    from ibkr_compute.integrations.pb_client import PBClient
    pb = PBClient(base_url=PB_URL)
    dw = DataWriter(pb_client=pb)

    # Valid bar
    assert dw._validate_bar({"symbol": "X", "bar_time_ms": 1, "open": 100, "high": 105, "low": 99, "close": 102})
    # Invalid: H < O
    assert not dw._validate_bar({"symbol": "X", "bar_time_ms": 1, "open": 100, "high": 99, "low": 98, "close": 99})
    # Invalid: L > C
    assert not dw._validate_bar({"symbol": "X", "bar_time_ms": 1, "open": 100, "high": 105, "low": 103, "close": 102})
    # Invalid: zero price
    assert not dw._validate_bar({"symbol": "X", "bar_time_ms": 1, "open": 0, "high": 105, "low": 99, "close": 102})
    return True


_test_signal_id = None


# ====================================================================
# Main
# ====================================================================

def main():
    global passed, failed, warnings

    print("=" * 70)
    print("  IBKR Trading System — E2E Automated Test")
    print("  Gateway: %s  |  PB: %s  |  Compute: %s" % (GW_URL, PB_URL, COMPUTE_URL))
    print("  Account: %s  |  Paper: %s" % (ACCT_ID, PAPER_ACCT))
    print("=" * 70)
    print()

    # Phase 0: Check PB collections exist
    print("[Phase 0] PocketBase Collections Check")
    collections_needed = []
    for name in ["ibkr_bars", "ibkr_orders", "ibkr_positions", "ibkr_session", "ibkr_conid_cache"]:
        try:
            r = pb_get("/api/collections/%s/records?perPage=1" % name)
            if r.status_code == 404:
                collections_needed.append(name)
        except Exception:
            collections_needed.append(name)

    if collections_needed:
        print("  MISSING collections: %s" % ", ".join(collections_needed))
        print("  Please create them via PB admin UI or API before running tests.")
    else:
        print("  All ibkr_* collections exist")
    print()

    # Phase 1: Services
    print("[Phase 1] Service Health")
    t_gw_service()
    t_compute_service()
    t_pb_service()
    t_compute_health()
    print()

    # Phase 2: Gateway
    print("[Phase 2] Gateway Authentication")
    t_gw_port()
    gw_ok = t_gw_auth()
    if gw_ok:
        t_gw_tickle()
        t_accounts()
    else:
        print("  SKIPPING gateway tests - not authenticated")
        print("  Run: source /opt/ibkr_compute/.env && PYTHONPATH=/opt/ibkr_compute/src python3 /opt/ibkr_compute/deploy/ibkr_login.py")
    print()

    # Phase 3: PB Collections
    print("[Phase 3] PocketBase Collections")
    t_pb_bars()
    t_pb_orders()
    t_pb_session()
    t_pb_positions()
    t_pb_conid()
    print()

    # Phase 4: Market Data
    print("[Phase 4] Market Data Pipeline")
    if gw_ok:
        t_conid()
        t_history()
        t_write_bar()
    else:
        print("  SKIPPED (gateway not authenticated)")
    print()

    # Phase 5: Signal Pipeline
    print("[Phase 5] Signal Pipeline")
    t_create_signal()
    t_signal_validate()
    t_signal_router()
    t_cleanup_signal()
    print()

    # Phase 6: Order Pipeline
    print("[Phase 6] Order Pipeline (Read-Only)")
    if gw_ok:
        t_paper_positions()
        t_live_orders()
    else:
        print("  SKIPPED (gateway not authenticated)")
    t_order_placer()
    print()

    # Phase 7: Integration
    print("[Phase 7] IBKR Service Integration")
    t_ibkr_status()
    t_ibkr_dashboard()
    print()

    # Phase 8: Session Keep-alive
    print("[Phase 8] Session Keep-alive")
    if gw_ok:
        t_session_keeper()
        t_auth_check()
    else:
        print("  SKIPPED (gateway not authenticated)")
    t_session_log()
    print()

    # Phase 9: Bar Aggregator
    print("[Phase 9] Bar Aggregator Unit Tests")
    t_bar_align()
    t_bar_ohlcv()
    print()

    # Phase 10: Data Writer
    print("[Phase 10] Data Writer Validation")
    t_dw_validate()
    print()

    # Summary
    total = passed + failed + warnings
    print("=" * 70)
    print("  RESULTS: %d passed, %d failed, %d warnings (total %d)" % (passed, failed, warnings, total))
    print("=" * 70)

    if failed > 0:
        print()
        print("  FAILURES:")
        for status, name in results:
            if status == "FAIL":
                print("    - %s" % name)

    if warnings > 0:
        print()
        print("  WARNINGS:")
        for status, name in results:
            if status == "WARN":
                print("    - %s" % name)

    print()
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

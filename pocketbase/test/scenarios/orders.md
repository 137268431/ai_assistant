# 订单管理 API

## 端点

| 方法 | 端点 | 说明 |
|------|------|------|
| `POST` | `/api/custom/ibkr/orders/upsert` | 创建或更新单条订单，并自动追加 `order_details` |
| `GET` | `/webhook/order/cancel?id={entry_unique_id}` | 取消主入场单 |
| `GET` | `/webhook/order/close?id={entry_unique_id}` | 关闭整个交易组 |

---

## 正式字段模型

订单展示和关系判断都基于以下字段：

| 字段 | 说明 |
|------|------|
| `unique_id` | IBKR 侧唯一订单 ID，`orders` 的逻辑主键 |
| `order_id` | 兼容字段，通常与 broker id 同步 |
| `broker_order_id` | 原始 broker 订单 ID |
| `trade_group_id` | 交易组 ID |
| `entry_order_unique_id` | 主入场单 `unique_id` |
| `parent_order_unique_id` | 子单的父单 |
| `sibling_order_unique_id` | 对手子单 |
| `role` | `entry` / `take_profit` / `stop_loss` / `repair_tp` / `repair_sl` |
| `relation_status` | `planned` / `active` / `closed` / `orphaned` |
| `position_side` | `long` / `short` |

---

## `orders/upsert` 典型请求

说明：

- `signals/ack` 负责预创建 `Entry / TP / SL`
- `orders/upsert` 负责把已有记录从 `Init` 推进到 `Submitted / Filled / Canceled / Closed`

### 1. Entry Submitted

```bash
curl -X POST "https://pb.lzw-glory.top/api/custom/ibkr/orders/upsert" \
  -H "Content-Type: application/json" \
  -d '{
    "unique_id": "test_AAPL_20260331_093000_sig_entry",
    "order_id": "IB_test_AAPL_20260331_093000_sig_entry",
    "broker_order_id": "IB_test_AAPL_20260331_093000_sig_entry",
    "order_type": "Entry",
    "symbol": "AAPL",
    "direction": "long",
    "position_side": "long",
    "trade_group_id": "test_AAPL_20260331_093000_sig_entry",
    "entry_order_unique_id": "test_AAPL_20260331_093000_sig_entry",
    "role": "entry",
    "relation_status": "active",
    "quantity": 100,
    "limit_price": 185.00,
    "status": "Submitted",
    "filled_qty": 0,
    "fill_price": 0,
    "tp_price": 192.50,
    "sl_price": 181.00,
    "signal_id": "AAPL_20260331_093000_sig",
    "us_time": "2026-03-31 09:30:00",
    "cn_time": "2026-03-31 21:30:00",
    "bar_time_ms": 1774949400000,
    "extra": {
      "reason": "entry submitted"
    }
  }'
```

### 2. TP Submitted（由 Entry Filled 激活）

```bash
curl -X POST "https://pb.lzw-glory.top/api/custom/ibkr/orders/upsert" \
  -H "Content-Type: application/json" \
  -d '{
    "unique_id": "test_AAPL_20260331_093000_sig_take_profit",
    "order_id": "IB_test_AAPL_20260331_093000_sig_take_profit",
    "broker_order_id": "IB_test_AAPL_20260331_093000_sig_take_profit",
    "order_type": "TakeProfit",
    "symbol": "AAPL",
    "direction": "long",
    "position_side": "long",
    "trade_group_id": "test_AAPL_20260331_093000_sig_entry",
    "entry_order_unique_id": "test_AAPL_20260331_093000_sig_entry",
    "parent_order_unique_id": "test_AAPL_20260331_093000_sig_entry",
    "sibling_order_unique_id": "test_AAPL_20260331_093000_sig_stop_loss",
    "role": "take_profit",
    "relation_status": "active",
    "quantity": 100,
    "limit_price": 192.50,
    "status": "Submitted",
    "filled_qty": 0,
    "fill_price": 0,
    "signal_id": "AAPL_20260331_093000_sig",
    "us_time": "2026-03-31 09:32:00",
    "cn_time": "2026-03-31 21:32:00",
    "bar_time_ms": 1774949520000,
    "extra": {
      "reason": "tp activated after entry filled"
    }
  }'
```

### 3. TP Filled

```bash
curl -X POST "https://pb.lzw-glory.top/api/custom/ibkr/orders/upsert" \
  -H "Content-Type: application/json" \
  -d '{
    "unique_id": "test_AAPL_20260331_093000_sig_take_profit",
    "order_id": "IB_test_AAPL_20260331_093000_sig_take_profit",
    "broker_order_id": "IB_test_AAPL_20260331_093000_sig_take_profit",
    "order_type": "TakeProfit",
    "symbol": "AAPL",
    "direction": "long",
    "position_side": "long",
    "trade_group_id": "test_AAPL_20260331_093000_sig_entry",
    "entry_order_unique_id": "test_AAPL_20260331_093000_sig_entry",
    "parent_order_unique_id": "test_AAPL_20260331_093000_sig_entry",
    "sibling_order_unique_id": "test_AAPL_20260331_093000_sig_stop_loss",
    "role": "take_profit",
    "relation_status": "closed",
    "quantity": 100,
    "limit_price": 192.50,
    "status": "Filled",
    "filled_qty": 100,
    "fill_price": 192.50,
    "pnl": 750,
    "signal_id": "AAPL_20260331_093000_sig",
    "fill_time": "2026-03-31 10:05:00",
    "us_time": "2026-03-31 10:05:00",
    "cn_time": "2026-03-31 22:05:00",
    "bar_time_ms": 1774951500000,
    "extra": {
      "reason": "take profit filled"
    }
  }'
```

### 4. SL Counterpart Canceled

```bash
curl -X POST "https://pb.lzw-glory.top/api/custom/ibkr/orders/upsert" \
  -H "Content-Type: application/json" \
  -d '{
    "unique_id": "test_AAPL_20260331_093000_sig_stop_loss",
    "order_id": "IB_test_AAPL_20260331_093000_sig_stop_loss",
    "broker_order_id": "IB_test_AAPL_20260331_093000_sig_stop_loss",
    "order_type": "StopLoss",
    "symbol": "AAPL",
    "direction": "long",
    "position_side": "long",
    "trade_group_id": "test_AAPL_20260331_093000_sig_entry",
    "entry_order_unique_id": "test_AAPL_20260331_093000_sig_entry",
    "parent_order_unique_id": "test_AAPL_20260331_093000_sig_entry",
    "sibling_order_unique_id": "test_AAPL_20260331_093000_sig_take_profit",
    "role": "stop_loss",
    "relation_status": "closed",
    "quantity": 100,
    "limit_price": 181.00,
    "status": "Canceled",
    "filled_qty": 0,
    "fill_price": 0,
    "signal_id": "AAPL_20260331_093000_sig",
    "us_time": "2026-03-31 10:05:00",
    "cn_time": "2026-03-31 22:05:00",
    "bar_time_ms": 1774951500000,
    "extra": {
      "reason": "canceled because TP filled"
    }
  }'
```

---

## `orders/upsert` 行为

每次调用都会：

1. 以 `unique_id` 查找现有订单，不存在则创建。
2. 同步正式关系字段到 `orders` 顶层字段和 `extra`。
3. 根据状态写入状态元信息。
4. 追加一条 `order_details` 历史事件。
5. 更新飞书订单卡片。

---

## 页面动作语义

### 取消主单

`GET /webhook/order/cancel?id={entry_unique_id}`

- 只允许 `role=entry`
- 状态改为 `Canceled`
- `relation_status` 改为 `closed`
- 追加一条 `order_details`

### 平仓整组

`GET /webhook/order/close?id={entry_unique_id}`

- 按 `trade_group_id` 找到整组订单
- 主单改为 `Closed`
- 所有仍活跃的子单改为 `Canceled`
- 每条记录都追加自己的 `order_details`

---

## `order_details` 记录规则

`order_details` 是事件表，不是订单表。每个状态变化都会新增一条记录。

建议把它理解为：

- `orders` = 当前快照
- `order_details` = 全量时间线

页面中的：

- `orders.html` 读取 `orders`，按 `trade_group_id` 聚合
- `order_details.html` 读取 `order_details`，支持 trade group 关系树和事件时间线

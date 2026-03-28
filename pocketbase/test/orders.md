# 订单管理 API

## 目录
- [端点总览](#端点总览)
- [Upsert 订单](#upsert-订单)
- [订单详情记录](#订单详情记录)
- [飞书通知机制](#飞书通知机制)

---

## 端点总览

| 方法 | 端点 | 认证 | 说明 |
|------|------|------|------|------|
| POST | `/api/custom/orders/upsert` | - | 创建/更新订单 |

---

## Upsert 订单

创建新订单或更新已有订单状态，同时自动记录 `order_details` 历史。

### 请求

```
POST /api/custom/orders/upsert
```

### 请求示例

**提交订单：**

```bash
curl -X POST "https://pb.lzw-glory.top/api/custom/orders/upsert" \
    -H "Content-Type: application/json" \
    -d '{
      "unique_id": "order_aapl_entry_20260202_1",
      "order_type": "Entry",
      "order_id": "IB_12345",
      "symbol": "AAPL",
      "direction": "long",
      "quantity": 100,
      "limit_price": 185.00,
      "status": "Submitted",
      "filled_qty": 0,
      "fill_price": 0,
      "tp_price": 192.50,
      "sl_price": 181.00,
      "signal_id": "signal_aapl_long_20260202",
      "us_time": "2026-02-02 10:30:00",
      "cn_time": "2026-02-02 18:30:00",
      "bar_time_ms": 1738411800000,
      "extra": {
        "reason": "Test order"
      }
    }'
```

**订单成交：**

```bash
curl -X POST "https://pb.lzw-glory.top/api/custom/orders/upsert" \
    -H "Content-Type: application/json" \
    -d '{
      "unique_id": "order_aapl_entry_20260202_1",
      "order_type": "Entry",
      "order_id": "IB_12345",
      "symbol": "AAPL",
      "direction": "long",
      "quantity": 100,
      "limit_price": 185.00,
      "status": "Filled",
      "filled_qty": 100,
      "fill_price": 185.20,
      "tp_price": 192.50,
      "sl_price": 181.00,
      "signal_id": "signal_aapl_long_20260202",
      "pnl": 0,
      "commission": 1.5,
      "rr_ratio": 2.5,
      "us_time": "2026-02-02 10:35:00",
      "cn_time": "2026-02-02 18:35:00",
      "bar_time_ms": 1738412100000,
      "extra": {
        "reason": "Order filled"
      }
    }'
```

**止盈成交：**

```bash
curl -X POST "https://pb.lzw-glory.top/api/custom/orders/upsert" \
    -H "Content-Type: application/json" \
    -d '{
      "unique_id": "order_aapl_exit_tp_20260202_1",
      "order_type": "TakeProfit",
      "order_id": "IB_67890",
      "symbol": "AAPL",
      "direction": "long",
      "quantity": 100,
      "limit_price": 192.50,
      "status": "Filled",
      "filled_qty": 100,
      "fill_price": 192.50,
      "signal_id": "signal_aapl_long_20260202",
      "pnl": 480,
      "commission": 1.5,
      "rr_ratio": 2.5,
      "us_time": "2026-02-02 14:00:00",
      "cn_time": "2026-02-02 22:00:00",
      "bar_time_ms": 1738428000000,
      "extra": {
        "reason": "Take profit reached"
      }
    }'
```

### 请求字段

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `unique_id` | string | 是 | 订单唯一ID（去重key） |
| `order_type` | string | 是 | QC 订单类型：`"Entry"` / `"TakeProfit"` / `"StopLoss"` |
| `symbol` | string | 是 | 股票代码 |
| `order_id` | string | 否 | 券商订单ID |
| `direction` | string | 否 | `"long"` 或 `"short"` |
| `quantity` | number | 否 | 数量 |
| `limit_price` | number | 否 | 限价 |
| `status` | string | 是 | 事件状态：`"Submitted"` / `"Filled"` / `"Canceled"` |
| `filled_qty` | number | 否 | 成交数量 |
| `fill_price` | number | 否 | 成交价格 |
| `signal_id` | string | 否 | 关联信号ID |
| `tp_price` | number | 否 | 止盈价格（由 QC 算法计算） |
| `sl_price` | number | 否 | 止损价格（由 QC 算法计算） |
| `us_time` | string | 否 | 美东时间，格式 `YYYY-MM-DD HH:MM:SS`（回测时使用 QC 算法时间） |
| `cn_time` | string | 否 | 北京时间，格式 `YYYY-MM-DD HH:MM:SS`（回测时使用 QC 算法时间） |
| `bar_time_ms` | number | 否 | Bar 时间戳（毫秒） |
| `pnl` | number | 否 | 盈亏金额 |
| `commission` | number | 否 | 手续费 |
| `rr_ratio` | number | 否 | 风报比 |
| `extra` | object | 否 | 附加数据 |

### 事件状态流转（status）

| status | 说明 |
|--------|------|
| `Init` | 初始化（信号确认时由 PB 创建） |
| `Submitted` | 已提交（挂单中） |
| `Filled` | 已成交 |
| `Canceled` | 已取消 |

### 订单类型（order_type）

| order_type | 说明 |
|------------|------|
| `Entry` | 入场订单 |
| `TakeProfit` | 止盈订单 |
| `StopLoss` | 止损订单 |

### 自动处理

1. **去重**：以 `unique_id` 为 key，存在则更新
2. **写入 order_details**：每次状态变化记录一条历史，包含 `sequence` 序号
3. **飞书通知**：
   - 首次 `Entry + Submitted` → 发送交互卡片（带取消/平仓按钮）
   - `Filled` → 发送成交通知
   - `Canceled` → 发送取消通知
   - `Closed` → 发送平仓通知

---

## 订单详情记录

每次 `orders` 表状态变化时，自动在 `order_details` 表记录一条历史。

### order_details 字段

| 字段 | 说明 |
|------|------|
| `order_id` | 订单 unique_id |
| `symbol` | 股票代码 |
| `direction` | 方向 |
| `order_type` | QC 订单类型（Entry / TakeProfit / StopLoss） |
| `status` | 事件状态（Init / Submitted / Filled / Canceled） |
| `reason` | 原因 |
| `signal_id` | 关联信号ID |
| `us_time` | 美东时间（回测时使用 QC 算法时间） |
| `cn_time` | 北京时间（回测时使用 QC 算法时间） |
| `bar_time_ms` | K线时间戳 |
| `extra` | 完整订单信息快照（含 order_type / limit_price / fill_price / tp_price / sl_price / quantity 等） |

---

## 飞书通知机制

### 通知类型

| 触发条件 | 通知类型 | 卡片样式 |
|----------|----------|----------|
| Entry + Submitted + 首次 | `notifyNewOrder` | 交互卡片（带按钮） |
| Filled | `notifyOrder` | 普通通知 |
| Canceled | `notifyOrder` | 普通通知 |

### 交互卡片按钮

- **取消挂单** → `GET /webhook/order/cancel?id={unique_id}`
- **平仓** → `GET /webhook/order/close?id={unique_id}`

### Webhook 分组

| 类型 | Webhook URL |
|------|-------------|
| `signal` | `https://open.feishu.cn/open-apis/bot/v2/hook/298ce054-...` |
| `order` | `https://open.feishu.cn/open-apis/bot/v2/hook/eee38484-...` |
| `error` | `https://open.feishu.cn/open-apis/bot/v2/hook/98f6f9a5-...` |

---

## 完整流程图

```
信号确认 → orders/upsert → order_details
                              │
                              │ QC 订单状态变化
                              ▼
                         orders/upsert → order_details

┌─────────────┐
│  TradingView │
└──────┬──────┘
       │ webhook/tv
       ▼
┌─────────────┐     ┌─────────────┐
│   signals   │     │ indicators  │
└──────┬──────┘     └─────────────┘
       │
       │ QC 拉取
       ▼
┌─────────────────┐
│ signals/pending │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  QC 执行下单    │
└────────┬────────┘
         │
         │ signals/ack
         ▼
┌─────────────────┐     ┌────────────────┐
│     orders      │────▶│ order_details   │
│ order_type=Entry│     │ seq=1, status=Init│
│ status=Init     │     └────────────────┘
└────────┬────────┘
         │
         │ orders/upsert (Submitted)
         ▼
┌─────────────────┐     ┌────────────────┐
│     orders      │────▶│ order_details   │
│ status=Submitted│     │ seq=2, status=Submitted│
└────────┬────────┘     └────────────────┘
         │
         │ orders/upsert (Filled/Canceled)
         ▼
┌─────────────────┐     ┌────────────────┐
│     orders      │────▶│ order_details   │
│ status=Filled   │     │ seq=3, status=Filled │
└─────────────────┘     └────────────────┘
```

---

## 相关文档

- [Webhook API](./webhook.md) - 了解信号如何产生
- [信号管理 API](./signals.md) - 了解信号状态流转
- [逆向信号 API](./reverse_signals.md) - 了解逆向信号如何触发订单操作
- [配置参考](./config.md) - 了解订单相关配置

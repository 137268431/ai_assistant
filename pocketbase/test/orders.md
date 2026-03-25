# 订单管理 API

## 目录
- [端点总览](#端点总览)
- [Upsert 订单](#upsert-订单)
- [获取待处理订单](#获取待处理订单)
- [确认订单操作完成](#确认订单操作完成)
- [订单详情记录](#订单详情记录)
- [飞书通知机制](#飞书通知机制)

---

## 端点总览

| 方法 | 端点 | 认证 | 说明 |
|------|------|------|------|
| POST | `/api/custom/orders/upsert` | 超级用户 | 创建/更新订单 |
| GET | `/api/custom/orders/pending` | 超级用户 | 获取待执行操作 |
| POST | `/api/custom/orders/ack` | 超级用户 | 确认操作完成 |

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
    -H "Authorization: Bearer YOUR_TOKEN" \
    -d '{
      "unique_id": "order_aapl_entry_20260201_1",
      "order_type": "Entry",
      "order_id": "IB_12345",
      "symbol": "AAPL",
      "direction": "long",
      "quantity": 100,
      "limit_price": 185.00,
      "status": "Submitted",
      "filled_qty": 0,
      "fill_price": 0,
      "signal_id": "signal_aapl_long_20260201",
      "extra": {
        "reason": "Test order",
        "bar_time_ms": 1738411200000
      }
    }'
```

**订单成交：**

```bash
curl -X POST "https://pb.lzw-glory.top/api/custom/orders/upsert" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer YOUR_TOKEN" \
    -d '{
      "unique_id": "order_aapl_entry_20260201_1",
      "order_type": "Entry",
      "order_id": "IB_12345",
      "symbol": "AAPL",
      "direction": "long",
      "quantity": 100,
      "limit_price": 185.00,
      "status": "Filled",
      "filled_qty": 100,
      "fill_price": 185.20,
      "signal_id": "signal_aapl_long_20260201",
      "pnl": 0,
      "commission": 1.5,
      "rr_ratio": 2.5,
      "extra": {
        "reason": "Order filled",
        "bar_time_ms": 1738411500000
      }
    }'
```

**止盈成交：**

```bash
curl -X POST "https://pb.lzw-glory.top/api/custom/orders/upsert" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer YOUR_TOKEN" \
    -d '{
      "unique_id": "order_aapl_exit_tp_20260201_1",
      "order_type": "Exit",
      "order_id": "IB_67890",
      "symbol": "AAPL",
      "direction": "long",
      "quantity": 100,
      "limit_price": 190.00,
      "status": "Filled",
      "filled_qty": 100,
      "fill_price": 190.00,
      "signal_id": "signal_aapl_long_20260201",
      "pnl": 480,
      "commission": 1.5,
      "rr_ratio": 2.5,
      "extra": {
        "reason": "Take profit reached",
        "exit_type": "take_profit",
        "bar_time_ms": 1738412400000
      }
    }'
```

### 请求字段

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `unique_id` | string | 是 | 订单唯一ID（去重key） |
| `order_type` | string | 是 | `"Entry"` 或 `"Exit"` |
| `symbol` | string | 是 | 股票代码 |
| `order_id` | string | 否 | 券商订单ID |
| `direction` | string | 否 | `"long"` 或 `"short"` |
| `quantity` | number | 否 | 数量 |
| `limit_price` | number | 否 | 限价 |
| `status` | string | 否 | 订单状态 |
| `filled_qty` | number | 否 | 成交数量 |
| `fill_price` | number | 否 | 成交价格 |
| `signal_id` | string | 否 | 关联信号ID |
| `pnl` | number | 否 | 盈亏金额 |
| `commission` | number | 否 | 手续费 |
| `rr_ratio` | number | 否 | 风报比 |
| `extra` | object | 否 | 附加数据 |

### 订单状态流转

| status | 说明 |
|--------|------|
| `Submitted` | 已提交（挂单中） |
| `Filled` | 已成交 |
| `Canceled` | 已取消 |
| `Closed` | 已平仓 |
| `Pending` | 待处理 |
| `New` | 新订单 |
| `PartiallyFilled` | 部分成交 |

### 自动处理

1. **去重**：以 `unique_id` 为 key，存在则更新
2. **写入 order_details**：每次状态变化记录一条历史
3. **飞书通知**：
   - 首次 `Entry + Submitted` → 发送交互卡片（带取消/平仓按钮）
   - `Filled` → 发送成交通知
   - `Canceled` → 发送取消通知
   - `Closed` → 发送平仓通知

---

## 获取待处理订单

查询所有 `action` 字段非空的订单（待 QC 执行操作）。

### 请求

```
GET /api/custom/orders/pending
```

### 请求示例

```bash
curl -X GET "https://pb.lzw-glory.top/api/custom/orders/pending" \
    -H "Authorization: Bearer YOUR_TOKEN"
```

### 响应

```json
{
  "status": "success",
  "actions": [
    {
      "id": "record_id",
      "unique_id": "order_aapl_entry_20260201_1",
      "order_id": "IB_12345",
      "symbol": "AAPL",
      "direction": "long",
      "order_type": "Entry",
      "action": "cancel",
      "action_params": {},
      "status": "Submitted"
    }
  ]
}
```

### action 字段说明

| action | 说明 | QC 应执行的操作 |
|--------|------|----------------|
| `cancel` | 取消订单 | 调用券商API取消 |
| `close` | 平仓订单 | 调用券商API市价平仓 |

---

## 确认订单操作完成

QC 执行完操作后回调，清除 `action` 字段。

### 请求

```
POST /api/custom/orders/ack
```

### 请求示例

```bash
curl -X POST "https://pb.lzw-glory.top/api/custom/orders/ack" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer YOUR_TOKEN" \
    -d '{
      "unique_id": "order_aapl_entry_20260201_1",
      "result": "completed"
    }'
```

### 请求字段

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `unique_id` | string | 是 | 订单唯一ID |
| `result` | string | 否 | 执行结果，默认 `"completed"` |

### 响应

```json
{
  "success": true
}
```

---

## 订单详情记录

每次 `orders` 表状态变化时，自动在 `order_details` 表记录一条历史。

### order_details 字段

| 字段 | 说明 |
|------|------|
| `order_id` | 订单 unique_id |
| `symbol` | 股票代码 |
| `direction` | 方向 |
| `event_type` | 事件类型（submitted/filled/canceled/closed） |
| `old_value` | 旧值（预留） |
| `new_value` | 新值（预留） |
| `reason` | 原因 |
| `signal_id` | 关联信号ID |
| `event_time` | 事件时间 |
| `us_time` | 美国时间 |
| `cn_time` | 中国时间 |
| `bar_time_ms` | K线时间戳 |
| `sequence` | 序列号（同一订单的累计事件序号） |
| `extra` | 完整订单信息快照 |

---

## 飞书通知机制

### 通知类型

| 触发条件 | 通知类型 | 卡片样式 |
|----------|----------|----------|
| Entry + Submitted + 首次 | `notifyNewOrder` | 交互卡片（带按钮） |
| Filled | `notifyOrder` | 普通通知 |
| Canceled | `notifyOrder` | 普通通知 |
| Closed | `notifyOrder` | 普通通知 |

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
         │ orders/upsert
         ▼
┌─────────────────┐     ┌────────────────┐
│     orders      │────▶│ order_details   │
└────────┬────────┘     └────────────────┘
         │
         │ action 非空
         ▼
┌─────────────────┐
│ orders/pending  │ ◀── QC 轮询
└────────┬────────┘
         │
         │ QC 执行操作
         ▼
┌─────────────────┐
│  orders/ack     │ ──▶ 清除 action
└─────────────────┘
```

---

## 相关文档

- [信号管理 API](./signals.md)
- [逆向信号 API](./reverse_signals.md)
- [配置参考](./config.md)

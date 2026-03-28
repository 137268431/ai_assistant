# 信号管理 API

## 目录
- [端点总览](#端点总览)
- [QC 拉取待确认信号](#qc-拉取待确认信号)
- [QC 确认信号已处理](#qc-确认信号已处理)
- [飞书按钮回调](#飞书按钮回调)
- [状态流转图](#状态流转图)

---

## 端点总览

| 方法 | 端点 | 认证 | 说明 |
|------|------|------|------|
| POST | `/webhook/tv` | - | TradingView Webhook 信号接收 |
| GET | `/api/custom/signals/pending` | - | QC 拉取待执行信号 |
| POST | `/api/custom/signals/ack` | - | QC 确认信号已处理 |
| POST | `/webhook/feishu/callback` | - | 飞书按钮回调（确认/拒绝） |

> **订单操作**（取消/平仓）已移至 [订单管理 API](./orders.md)。
> **Webhook 详细说明**请参考 [Webhook API](./webhook.md)。

---

## QC 拉取待确认信号

拉取指定日期内所有状态为 `pending` 的信号。

### 请求

```
GET /api/custom/signals/pending?date=2026-02-02
```

| 参数 | 类型 | 说明 |
|------|------|------|
| `date` | string | 可选，格式 `YYYY-MM-DD`，默认当天美东时间 |

### 请求示例

```bash
curl -X GET "https://pb.lzw-glory.top/api/custom/signals/pending?date=2026-02-02"
```

### 响应

```json
{
  "signals": [
    {
      "id": "record_id",
      "signal_id": "AAPL_20260202_1000_trend_U",
      "symbol": "AAPL",
      "direction": "long",
      "signal": "trend_sdUpper",
      "entry": 242.79,
      "stop_loss": 241.14,
      "take_profit": 245.26,
      "limit_price": 244.00,
      "shares": 42,
      "rr": "1.5:1",
      "reason": "SD上轨→顺势做多(fractal↑+EMA-touch↑[ema慢线]+div↑[cRSI+OBV])",
      "date": "2026-02-02",
      "us_time": "2026-02-02 10:00:00",
      "bar_time_ms": 1770015600000,
      "extra": {
        "day_change_pct": 1.27,
        "atr_pct": 0.23,
        "sl_atr_ratio": 3.0
      },
      "created": "2026-02-02 10:00:00"
    }
  ]
}
```

---

## QC 确认信号已处理

QC 处理完信号后回调，更新信号状态。

### 请求

```
POST /api/custom/signals/ack
```

### 请求示例

```bash
curl -X POST "https://pb.lzw-glory.top/api/custom/signals/ack" \
    -H "Content-Type: application/json" \
    -d '{
      "signal_id": "AAPL_20260202_1000_trend_U",
      "status": "executed",
      "note": "Order placed successfully",
      "order": {
        "unique_id": "12345",
        "order_type": "Entry",
        "direction": "long",
        "quantity": 42,
        "limit_price": 244.00,
        "filled_qty": 0,
        "fill_price": 0,
        "stop_loss": 241.14,
        "take_profit": 245.26,
        "order_time": "2026-02-02 10:30:00",
        "us_time": "2026-02-02 10:30:00",
        "cn_time": "2026-02-02 18:30:00",
        "bar_time_ms": 1770017400000,
        "extra": {}
      }
    }'
```

### 请求参数

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `signal_id` | string | 是 | 信号唯一ID |
| `status` | string | 否 | 新状态，默认 `"executed"` |
| `note` | string | 否 | 备注信息 |
| `order` | object | 否 | 订单信息，QC 下单成功后传入，用于在 PB 中创建 `orders` 记录 |

### order 对象字段

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `unique_id` | string | 是 | 券商订单ID，作为订单唯一标识 |
| `order_type` | string | 是 | 订单类型，如 `"Entry"` |
| `direction` | string | 是 | 方向，`"long"` 或 `"short"` |
| `quantity` | number | 是 | 数量 |
| `limit_price` | number | 是 | 限价 |
| `filled_qty` | number | 否 | 已成交数量，默认 `0` |
| `fill_price` | number | 否 | 成交价格，默认 `0` |
| `stop_loss` | number | 否 | 止损价格 |
| `take_profit` | number | 否 | 止盈价格 |
| `order_time` | string | 否 | 下单时间，格式 `YYYY-MM-DD HH:MM:SS`（美东时间） |
| `us_time` | string | 否 | 美东时间，格式 `YYYY-MM-DD HH:MM:SS`（回测时使用 QC 算法时间） |
| `cn_time` | string | 否 | 北京时间，格式 `YYYY-MM-DD HH:MM:SS`（回测时自动计算） |
| `bar_time_ms` | number | 否 | Bar 时间戳（毫秒），对应 PB orders 表的 `bar_time_ms` 字段 |
| `extra` | object | 否 | 扩展信息（QC 可传入 signal.extra 等补充数据），会写入 `order_details.extra` |

> **说明**：调用成功后，PB 会自动在 `orders` 表创建一条记录（`order_type=Entry`, `status=Init`），并在 `order_details` 表创建一条初始事件记录（`order_type=Entry`, `sequence=1`）。后续 QC 订单状态变化通过 `orders/upsert` 更新。

### 响应

```json
{
  "success": true,
  "signal_id": "AAPL_20260202_1000_trend_U",
  "status": "executed"
}
```

---

## 飞书按钮回调

用户在飞书消息中点击"确认"或"拒绝"按钮，调用此接口处理回调。

### 请求

```
POST /webhook/feishu/callback
```

### 确认操作

**状态检查逻辑：**

| 当前状态 | 行为 |
|----------|------|
| `awaiting_confirm` | 更新为 `pending`，返回成功 |
| `pending` | 返回"信号已确认，请勿重复操作" |
| `expired` | 返回"该信号已过期，无法确认" |
| `rejected` | 返回"该信号已拒绝，无法确认" |
| `executed` | 返回"该信号已执行，无法确认" |
| `closed` | 返回"该信号已平仓，无法确认" |

### 拒绝操作

**状态检查逻辑：**

| 当前状态 | 行为 |
|----------|------|
| `awaiting_confirm` | 更新为 `rejected`，返回成功 |
| `pending` | 返回"信号正在等待执行，无法拒绝" |
| `rejected` | 返回"信号已拒绝，请勿重复操作" |
| `expired` | 返回"该信号已过期，无法拒绝" |
| `executed` | 返回"该信号已执行，无法拒绝" |
| `closed` | 返回"该信号已平仓，无法拒绝" |

### 卡片更新

回调成功后，返回更新后的交互卡片：
- **确认成功**：`✅ 待执行` 状态卡片，消息："确认成功，正在等待执行..."
- **拒绝成功**：`❌ 已拒绝` 状态卡片，消息："信号已拒绝，暂不执行"
- **重复操作**：显示当前状态，提示勿重复操作
- **不可操作**：显示当前状态，提示无法操作原因

---

## 状态流转图

```
                    ┌─────────────────────┐
                    │ TradingView Webhook │
                    └──────────┬──────────┘
                               │
                               ▼
              ┌────────────────────────────────┐
              │  初始状态取决于 signal_auto_confirm  │
              └───────────────┬────────────────┘
                              │
        ┌─────────────────────┼─────────────────────┐
        │                     │                     │
        ▼                     ▼                     ▼
┌───────────────┐    ┌───────────────┐    ┌───────────────┐
│    pending    │    │ awaiting_     │    │    pending    │
│  (自动确认)    │    │ confirm        │    │  (手动确认)    │
│               │    │  (需手动确认)   │    └───────┬───────┘
└───────┬───────┘    └───────┬───────┘            │
        │                   │                    │
        │          ┌────────┴────────┐          │
        │          │                 │          │
        │          ▼                 ▼          │
        │    ┌──────────┐      ┌──────────┐      │
        │    │ pending  │      │ rejected │      │
        │    │ (已确认) │      │ (已拒绝) │      │
        │    └────┬─────┘      └──────────┘      │
        │         │                               │
        │         │ QC 执行/订单成交               │
        │         ▼                               │
        │    ┌──────────┐                        │
        │    │executed  │                        │
        │    │ (已执行) │                        │
        │    └────┬─────┘                        │
        │         │                              │
        │         │ 订单成交/超时/手动平仓        │
        │         ▼                              │
        │    ┌──────────┐                        │
        │    │  closed  │                        │
        │    │ (已平仓) │                        │
        │    └──────────┘                        │
        │                                           │
        │ 信号过期（超有效期）                       │
        ▼                                           │
   ┌──────────┐                                    │
   │ expired  │                                    │
   │ (已过期) │                                    │
   └──────────┘                                    │
```

### 状态说明

| 状态 | 说明 |
|------|------|
| `awaiting_confirm` | 待确认（需手动点击飞书确认按钮） |
| `pending` | 等待执行（已确认，等 QC 执行） |
| `executed` | 已执行（QC 已下订单） |
| `expired` | 已过期（超时自动失效） |
| `rejected` | 已拒绝（手动拒绝） |
| `closed` | 已平仓（订单已平仓） |

---

## 相关文档

- [Webhook API](./webhook.md) - 了解信号如何产生、飞书卡片逻辑
- [订单管理 API](./orders.md) - 了解信号触发后的下单流程
- [逆向信号 API](./reverse_signals.md) - 了解信号冲突检测
- [配置参考](./config.md) - 了解 `signal_auto_confirm` 等配置
# 信号管理 API

## 目录
- [端点总览](#端点总览)
- [QC 拉取待确认信号](#qc-拉取待确认信号)
- [QC 确认信号已处理](#qc-确认信号已处理)
- [飞书按钮 - 确认信号](#飞书按钮---确认信号)
- [飞书按钮 - 取消信号](#飞书按钮---取消信号)

---

## 端点总览

| 方法 | 端点 | 认证 | 说明 |
|------|------|------|------|
| POST | `/webhook/tv` | 无 | TradingView Webhook 信号接收 |
| GET | `/api/custom/signals/pending` | 无 | QC 拉取待执行信号 |
| POST | `/api/custom/signals/ack` | 无 | QC 确认信号已处理 |
| GET | `/webhook/signal/confirm` | 无 | 飞书确认按钮 |
| GET | `/webhook/signal/cancel` | 无 | 飞书拒绝按钮 |

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

curl -X GET "https://pb.lzw-glory.top/api/custom/signals/pending"
```

### 响应

```json
{
  "signals": [
    {
      "id": "record_id",
      "signal_id": "signal_aapl_long_20260201_trend_U",
      "symbol": "AAPL",
      "direction": "long",
      "signal": "trend_sdUpper",
      "entry": 185.00,
      "stop_loss": 183.00,
      "take_profit": 190.00,
      "limit_price": 185.00,
      "shares": 100,
      "rr": "2.5:1",
      "reason": "SD上轨→顺势做多",
      "date": "2026-02-02",
      "us_time": "2026-02-02 10:00:00",
      "bar_time_ms": 1738411200000,
      "extra": {},
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
      "signal_id": "signal_aapl_long_20260201_trend_U",
      "status": "executed",
      "note": "Order placed successfully"
    }'
```

### 请求参数

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `signal_id` | string | 是 | 信号唯一ID |
| `status` | string | 否 | 新状态，默认 `"executed"` |
| `note` | string | 否 | 备注信息 |

### 响应

```json
{
  "success": true,
  "signal_id": "signal_aapl_long_20260201_trend_U",
  "status": "executed"
}
```

---

## 飞书按钮 - 确认信号

用户在飞书消息中点击"确认"按钮调用此接口。

### 请求

```
GET /webhook/signal/confirm?id={signal_id}&token={token}
```

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `id` | string | 是 | 信号ID（支持 `id` 或 `signal_id`） |
| `token` | string | 否 | 权限验证 Token（当前未实现验证） |

> **注意**：`token` 参数虽然出现在 URL 中，但后端目前**未实现验证**逻辑。实际权限控制依赖飞书机器人的安全性。

### 请求示例

```bash
curl -X GET "https://pb.lzw-glory.top/webhook/signal/confirm?id=signal_aapl_long_20260201_trend_U&token=YOUR_TOKEN"
```

### 状态检查逻辑

| 当前状态 | 行为 |
|----------|------|
| `pending` / `awaiting_confirm` | 更新为 `pending`，返回成功 |
| `expired` | 返回"信号已过期" |
| `rejected` | 返回"信号已拒绝" |
| `executed` | 返回"信号已执行" |

### 响应（HTML）

成功：
```html
<div class="card">
  <div class="emoji">✅</div>
  <div class="title">信号已确认</div>
  <div class="detail">AAPL</div>
</div>
```

失败：
```html
<div class="card">
  <div class="emoji">🔒</div>
  <div class="title">无权限</div>
  <div class="detail">链接无效或已过期</div>
</div>
```

---

## 飞书按钮 - 取消信号

用户在飞书消息中点击"取消"按钮调用此接口。

### 请求

```
GET /webhook/signal/cancel?id={signal_id}&token={token}
```

### 请求示例

```bash
curl -X GET "https://pb.lzw-glory.top/webhook/signal/cancel?id=signal_aapl_long_20260201_trend_U&token=YOUR_TOKEN"
```

### 状态检查逻辑

| 当前状态 | 行为 |
|----------|------|
| `pending` / `awaiting_confirm` | 更新为 `rejected`，返回成功 |
| `expired` | 返回"信号已过期" |
| `rejected` | 返回"信号已拒绝" |
| `executed` | 返回"信号已执行，无法拒绝" |

---

## 状态流转图

```
                    ┌─────────────┐
                    │   源头      │
                    └──────┬──────┘
                           │ TradingView Webhook
                           ▼
                    ┌─────────────┐
                    │   pending   │ ←── 初始状态（auto_confirm=true）
                    └──────┬──────┘
                           │ 配置 auto_confirm=false
                           ▼
                    ┌──────────────────┐
                    │ awaiting_confirm │ ←── 需手动确认
                    └────────┬─────────┘
                             │
              ┌──────────────┼──────────────┐
              │              │              │
              ▼              ▼              ▼
        ┌─────────┐    ┌───────────┐   ┌──────────┐
        │pending  │    │ executed  │   │ rejected │
        └─────────┘    └───────────┘   └──────────┘
              │              │
              │              │ 订单成交/超时
              ▼              ▼
        ┌─────────┐    ┌──────────┐
        │ expired │    │ (结束)   │
        └─────────┘    └──────────┘
```

### 状态说明

| 状态 | 说明 |
|------|------|
| `awaiting_confirm` | 待确认（需手动点击确认） |
| `pending` | 等待执行（已确认，等 QC 执行） |
| `executed` | 已执行（QC 已下订单） |
| `expired` | 已过期（超时自动失效） |
| `rejected` | 已拒绝（手动拒绝） |

---

## 相关文档

- [Webhook API](./webhook.md)
- [订单管理 API](./orders.md)
- [逆向信号 API](./reverse_signals.md)
- [配置参考](./config.md)

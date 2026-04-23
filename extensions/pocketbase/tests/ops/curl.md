# PocketBase API 测试用例

> **注意**: 本文档已拆分，详细用例请查看各独立文档。
> - [总览与架构图](./README.md)
> - [Webhook 接收](./webhook.md)
> - [信号管理 API](./ibkr_signals.md)
> - [订单管理 API](./orders.md)
> - [逆向信号 API](./reverse_signals.md)
> - [配置参考](./config.md)

---

## 快速索引

| 功能 | 端点 | 文档章节 |
|------|------|----------|
| TradingView 信号 | `POST /webhook/tv` | [webhook.md](./webhook.md#signal-信号) |
| TradingView 指标 | `POST /webhook/tv` | [webhook.md](./webhook.md#indicator-指标) |
| 拉取待确认信号 | `GET /api/custom/ibkr/signals/pending` | [ibkr_signals.md](./ibkr_signals.md#qc-拉取待确认信号) |
| 确认信号 | `POST /api/custom/ibkr/signals/ack` | [ibkr_signals.md](./ibkr_signals.md#qc-确认信号已处理) |
| 飞书确认按钮 | `GET /webhook/signal/confirm` | [ibkr_signals.md](./ibkr_signals.md#飞书按钮---确认信号) |
| 飞书取消按钮 | `GET /webhook/signal/cancel` | [ibkr_signals.md](./ibkr_signals.md#飞书按钮---取消信号) |
| Upsert 订单 | `POST /api/custom/ibkr/orders/upsert` | [orders.md](./orders.md#upsert-订单) |
| 获取待处理订单 | `GET /api/custom/orders/pending` | [orders.md](./orders.md#获取待处理订单) |
| 确认订单操作 | `POST /api/custom/orders/ack` | [orders.md](./orders.md#确认订单操作完成) |
| 飞书取消订单 | `GET /webhook/order/cancel` | [ibkr_signals.md](./ibkr_signals.md#飞书按钮---取消订单) |
| 飞书平仓订单 | `GET /webhook/order/close` | [ibkr_signals.md](./ibkr_signals.md#飞书按钮---平仓订单) |
| 计算逆向信号 | `POST /api/custom/ibkr/reverse/calculate` | [reverse_signals.md](./reverse_signals.md#计算逆向信号) |
| 查询逆向信号列表 | `GET /api/custom/ibkr/reverse/list` | [reverse_signals.md](./reverse_signals.md#reverselist--reversepending-响应关键字段) |
| 获取逆向信号 | `GET /api/custom/ibkr/reverse/pending` | [reverse_signals.md](./reverse_signals.md#获取待处理逆向信号) |
| 请求执行逆向信号 | `POST /api/custom/ibkr/reverse/dispatch` | [reverse_signals.md](./reverse_signals.md#reversedispatch) |
| 确认逆向信号 | `POST /api/custom/ibkr/reverse/ack` | [reverse_signals.md](./reverse_signals.md#确认逆向信号处理) |
| 配置信号有效期 | `POST /api/collections/config/records` | [config.md](./config.md#signal_validity_minutes) |

---

## 快速测试

### 1. 测试 Webhook 连接

```bash
curl -X POST "https://pb.lzw-glory.top/webhook/tv" \
    -H "Content-Type: application/json" \
    -d '{
      "type": "signal",
      "symbol": "TEST",
      "direction": "long",
      "entry": 100.00,
      "stop_loss": 99.00,
      "take_profit": 105.00,
      "signal_id": "test_001"
    }'
```

### 2. 拉取待确认信号

```bash
curl -X GET "https://pb.lzw-glory.top/api/custom/ibkr/signals/pending"
```

### 3. 确认信号

```bash
curl -X POST "https://pb.lzw-glory.top/api/custom/ibkr/signals/ack" \
    -H "Content-Type: application/json" \
    -d '{
      "signal_id": "test_001",
      "status": "executed"
    }'
```

---

## 环境信息

| 属性 | 值 |
|------|-----|
| **API 域名** | `https://pb.lzw-glory.top` |
| **Webhook** | `/webhook/tv` |
| **认证** | 暂时关闭，以后可能会加 |

---

## 路由与当前归属

| 路由族 | 当前归属 | 历史 / 兼容 Hook |
|-----------|------|------|
| `POST /webhook/tv` | `ibkr-api` 原生 | `webhook_tv.pb.js` |
| `GET /webhook/signal/*` | `ibkr-api` 原生 | `ibkr_signal_actions.pb.js` |
| `GET /webhook/order/*` | `ibkr-api` 原生 | `ibkr_signal_actions.pb.js` |
| `GET /api/custom/ibkr/signals/pending` / `POST /api/custom/ibkr/signals/ack` | `ibkr-api` 原生 | `ibkr_signal_actions.pb.js` |
| `POST /api/custom/ibkr/signal` / `POST /api/custom/ibkr/signals` | `ibkr-api` 原生 | `ibkr_actions.js` 兼容壳 |
| `POST /api/custom/ibkr/reverse/*` / `GET /api/custom/ibkr/reverse/*` | `ibkr-api` 原生 | `ibkr_reverse_signals.pb.js` |
| `signal_expiry_check` / `order_detail_integrity_guard` | `ibkr-scheduler` + `ibkr-api` 原生 | `ibkr_signal_scheduler.js` / `order_scheduler.js` 兼容壳 |
| `POST /api/custom/ibkr/orders/*` | `ibkr-api` 原生 | `order_manage.js` 兼容壳 |
| 飞书通知模块 | `ibkr-api` / PocketBase 兼容混合阶段 | `feishu.js` |

---

## 数据流向图

```
TradingView
     │
     ▼ POST /webhook/tv
┌────────────────┐
│  ibkr_signals 表    │ ◀── 写入信号
└───────┬────────┘
        │ GET /signals/pending
        ▼
   ┌─────────┐
   │   IBKR    │ ──▶ IBKR/券商下单
   └────┬────┘
        │ POST /orders/upsert
        ▼
┌────────────────┐
│  orders 表     │ ◀── 写入订单
└───────┬────────┘
        │
        │ 状态变化自动记录
        ▼
┌────────────────┐
│ order_details 表│ ◀── 写入历史
└────────────────┘
```

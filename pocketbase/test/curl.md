# PocketBase API 测试用例

> **注意**: 本文档已拆分，详细用例请查看各独立文档。
> - [总览与架构图](./README.md)
> - [Webhook 接收](./webhook.md)
> - [信号管理 API](./signals.md)
> - [订单管理 API](./orders.md)
> - [逆向信号 API](./reverse_signals.md)
> - [配置参考](./config.md)

---

## 快速索引

| 功能 | 端点 | 文档章节 |
|------|------|----------|
| TradingView 信号 | `POST /webhook/tv` | [webhook.md](./webhook.md#signal-信号) |
| TradingView 指标 | `POST /webhook/tv` | [webhook.md](./webhook.md#indicator-指标) |
| 拉取待确认信号 | `GET /api/custom/signals/pending` | [signals.md](./signals.md#qc-拉取待确认信号) |
| 确认信号 | `POST /api/custom/signals/ack` | [signals.md](./signals.md#qc-确认信号已处理) |
| 飞书确认按钮 | `GET /webhook/signal/confirm` | [signals.md](./signals.md#飞书按钮---确认信号) |
| 飞书取消按钮 | `GET /webhook/signal/cancel` | [signals.md](./signals.md#飞书按钮---取消信号) |
| Upsert 订单 | `POST /api/custom/orders/upsert` | [orders.md](./orders.md#upsert-订单) |
| 获取待处理订单 | `GET /api/custom/orders/pending` | [orders.md](./orders.md#获取待处理订单) |
| 确认订单操作 | `POST /api/custom/orders/ack` | [orders.md](./orders.md#确认订单操作完成) |
| 飞书取消订单 | `GET /webhook/order/cancel` | [signals.md](./signals.md#飞书按钮---取消订单) |
| 飞书平仓订单 | `GET /webhook/order/close` | [signals.md](./signals.md#飞书按钮---平仓订单) |
| 计算逆向信号 | `POST /api/custom/reverse/calculate` | [reverse_signals.md](./reverse_signals.md#计算逆向信号) |
| 获取逆向信号 | `GET /api/custom/reverse/pending` | [reverse_signals.md](./reverse_signals.md#获取待处理逆向信号) |
| 确认逆向信号 | `POST /api/custom/reverse/ack` | [reverse_signals.md](./reverse_signals.md#确认逆向信号处理) |
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
curl -X GET "https://pb.lzw-glory.top/api/custom/signals/pending"
```

### 3. 确认信号

```bash
curl -X POST "https://pb.lzw-glory.top/api/custom/signals/ack" \
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

## 路由与 Hook 对应关系

| Hook 文件 | 路由 |
|-----------|------|
| `webhook_tv.pb.js` | `POST /webhook/tv` |
| `signal_actions.pb.js` | `GET /webhook/signal/*`, `GET /webhook/order/*`, `GET /api/custom/signals/*` |
| `signal_expiry.pb.js` | 定时任务（每分钟） |
| `order_manage.pb.js` | `POST /api/custom/orders/*`, `GET /api/custom/orders/*` |
| `reverse_signals.pb.js` | `POST /api/custom/reverse/*`, `GET /api/custom/reverse/*` |
| `feishu.js` | 被其他 hook 引用（通知模块） |

---

## 数据流向图

```
TradingView
     │
     ▼ POST /webhook/tv
┌────────────────┐
│  signals 表    │ ◀── 写入信号
└───────┬────────┘
        │ GET /signals/pending
        ▼
   ┌─────────┐
   │   QC    │ ──▶ IBKR/券商下单
   └────┬────┘
        │ POST /orders/upsert
        ▼
┌────────────────┐
│  orders 表     │ ◀── 写入订单
└───────┬────────┘
        │ GET /orders/pending
        ▼
   ┌─────────┐
   │   QC    │ ──▶ 执行 action
   └────┬────┘
        │ POST /orders/ack
        ▼
   (清除 action)

┌────────────────┐
│ reverse_signals│ ◀── 自动检测/计算
└────────────────┘
```

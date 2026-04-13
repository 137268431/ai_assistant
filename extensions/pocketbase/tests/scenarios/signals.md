# 信号管理 API

## 端点

| 方法 | 端点 | 说明 |
|------|------|------|
| `POST` | `/api/custom/ibkr/signal` | 正式 IBKR 信号写入入口，会写 `ibkr_signals` 并触发飞书通知链 |
| `POST` | `/webhook/tv` | TradingView 兼容入口，主要写 `tv_signals` / `tv_indicators` |
| `GET` | `/api/custom/ibkr/signals/pending?date=YYYY-MM-DD` | IBKR 拉取待执行信号 |
| `POST` | `/api/custom/ibkr/signals/ack` | IBKR 确认信号并创建交易组骨架 |
| `POST` | `/webhook/feishu/callback` | 飞书确认 / 拒绝信号 |

---

## 信号状态流转

常见状态：

- `awaiting_confirm`
- `pending`
- `executed`
- `rejected`
- `expired`
- `closed`

其中订单链路真正开始的节点是：

- 信号已进入 `pending`
- IBKR 拉取后调用 `signals/ack`

---

## `signals/ack` 的实际作用

`POST /api/custom/ibkr/signals/ack` 不是只改信号状态。

它会同时做两件事：

1. 把 `ibkr_signals.status` 改成 `executed`
2. 在 `orders` 中创建交易组骨架：
   - `Entry + Init + active`
   - `TakeProfit + Init + planned`
   - `StopLoss + Init + planned`

因此 `order` 和 `child_orders` 必须从一开始就带正式关系字段，而不是只靠 `extra`。

---

## 请求示例

```bash
curl -X POST "https://pb.lzw-glory.top/api/custom/ibkr/signals/ack" \
  -H "Content-Type: application/json" \
  -d '{
    "signal_id": "AAPL_20260331_093000_sig",
    "status": "executed",
    "note": "IBKR ack signal and create init entry",
    "order": {
      "unique_id": "test_AAPL_20260331_093000_sig_entry",
      "order_id": "IB_test_AAPL_20260331_093000_sig_entry",
      "broker_order_id": "IB_test_AAPL_20260331_093000_sig_entry",
      "order_type": "Entry",
      "direction": "long",
      "position_side": "long",
      "trade_group_id": "test_AAPL_20260331_093000_sig_entry",
      "entry_order_unique_id": "test_AAPL_20260331_093000_sig_entry",
      "role": "entry",
      "relation_status": "active",
      "quantity": 100,
      "limit_price": 185.00,
      "filled_qty": 0,
      "fill_price": 0,
      "stop_loss": 181.00,
      "take_profit": 192.50,
      "order_time": "2026-03-31 09:30:00",
      "us_time": "2026-03-31 09:30:00",
      "cn_time": "2026-03-31 21:30:00",
      "bar_time_ms": 1774949400000,
      "extra": {
        "reason": "init entry from signals/ack"
      }
    },
    "child_orders": [
      {
        "unique_id": "test_AAPL_20260331_093000_sig_take_profit",
        "order_type": "TakeProfit",
        "role": "take_profit",
        "relation_status": "planned",
        "trade_group_id": "test_AAPL_20260331_093000_sig_entry",
        "entry_order_unique_id": "test_AAPL_20260331_093000_sig_entry",
        "parent_order_unique_id": "test_AAPL_20260331_093000_sig_entry",
        "sibling_order_unique_id": "test_AAPL_20260331_093000_sig_stop_loss",
        "quantity": 100,
        "limit_price": 192.50,
        "status": "Init"
      },
      {
        "unique_id": "test_AAPL_20260331_093000_sig_stop_loss",
        "order_type": "StopLoss",
        "role": "stop_loss",
        "relation_status": "planned",
        "trade_group_id": "test_AAPL_20260331_093000_sig_entry",
        "entry_order_unique_id": "test_AAPL_20260331_093000_sig_entry",
        "parent_order_unique_id": "test_AAPL_20260331_093000_sig_entry",
        "sibling_order_unique_id": "test_AAPL_20260331_093000_sig_take_profit",
        "quantity": 100,
        "limit_price": 181.00,
        "status": "Init"
      }
    ]
  }'
```

---

## `order` 对象关键字段

| 字段 | 说明 |
|------|------|
| `unique_id` | 主入场单的 IBKR 唯一 ID |
| `order_id` / `broker_order_id` | broker 原始订单 ID |
| `trade_group_id` | 整个交易组 ID，当前实现等于 `entry_order_unique_id` |
| `entry_order_unique_id` | 主入场单 ID |
| `role` | 主单为 `entry`，子单分别是 `take_profit` / `stop_loss` |
| `relation_status` | Entry 初始为 `active`，TP / SL 初始为 `planned` |
| `position_side` | `long` / `short` |

---

## 创建后的结果

调用成功后：

- `ibkr_signals.status = executed`
- `orders` 新增三条交易组记录：`Entry + Init`、`TP + Init`、`SL + Init`
- `order_details` 为三条记录分别新增第一条初始化事件

后续状态更新由 `orders/upsert` 接手，不再重复调用 `signals/ack`。

---

## 与测试脚本的映射

`pb-flow.sh` 中：

- 步骤 `1` 通过 `/api/custom/ibkr/signal` 发送正式 IBKR 信号
- 步骤 `3` / `4` 模拟飞书确认或拒绝
- 步骤 `5` 调用 `signals/ack` 创建带正式关系字段的 Entry / TP / SL Init
- 步骤 `I` 调用官方 `signals/pending` 接口验证 IBKR 拉取链路

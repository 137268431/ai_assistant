# 逆向信号 API

## 端点

| 方法 | 端点 | 说明 |
|------|------|------|
| `POST` | `/api/custom/ibkr/reverse/calculate` | 基于最新指标计算并写入反转信号 |
| `GET` | `/api/custom/ibkr/reverse/list` | 获取某日反转信号列表（页面使用） |
| `GET` | `/api/custom/ibkr/reverse/pending` | 拉取待处理反转信号（IBKR 使用） |
| `POST` | `/api/custom/ibkr/reverse/dispatch` | 页面请求执行或取消反转信号 |
| `POST` | `/api/custom/ibkr/reverse/ack` | IBKR 回写执行结果 |

---

## 统一关系字段

反转信号必须定位到整笔交易，而不是孤立的一条字符串订单。

关键字段：

- `trade_group_id`
- `entry_order_unique_id`
- `order_unique_id`
- `broker_order_id`
- `signal_id`
- `origin_signal_id`
- `target_state`
- `reverse_kind`

其中：

- `reverse_kind` = `signal_conflict` / `indicator_conflict`
- `target_state` = `pending_entry` / `filled_position`

---

## `reverse/list` / `reverse/pending` 响应关键字段

```json
{
  "ibkr_signals": [
    {
      "id": "reverse_record_id",
      "symbol": "AAPL",
      "direction": "long",
      "current_direction": "long",
      "new_direction": "short",
      "source": "signal",
      "reverse_kind": "signal_conflict",
      "target_state": "filled_position",
      "target_order_status": "Filled",
      "priority": 1,
      "strength": "strong",
      "score": 10,
      "action_type": "close",
      "status": "pending",
      "signal_id": "AAPL_20260331_093000_sig",
      "origin_signal_id": "AAPL_20260331_094500_sig",
      "trade_group_id": "test_AAPL_20260331_093000_sig_entry",
      "entry_order_unique_id": "test_AAPL_20260331_093000_sig_entry",
      "order_unique_id": "test_AAPL_20260331_093000_sig_entry",
      "broker_order_id": "IB_test_AAPL_20260331_093000_sig_entry",
      "entry_price": 100.1,
      "quantity": 100,
      "take_profit": 105.0,
      "stop_loss": 98.0,
      "triggered_signals": ["信号反转"]
    }
  ]
}
```

---

## `reverse/calculate`

### 入参

必填：

- `symbol`
- `direction`

可选：

- `origin_signal_id`
- `force_action_type`
- `score_override`
- `triggered_signals`

`force_action_type` 主要用于测试或手动场景，允许显式生成：

- `cancel`
- `close`
- `adjust_sl`
- `adjust_tp`

### 关键语义

- 若当前 `symbol + direction` 找不到对应活跃交易目标，则返回 `created=false, reason=no_conflict_target`
- 若未命中任何反转条件，则返回 `created=false, reason=no_reverse_conditions`
- `pending_entry` 默认只会落 `cancel`
- `filled_position` 默认按分数映射：
  - `strong -> close`
  - `medium -> adjust_sl`
  - `weak -> cancel`

---

## `reverse/dispatch`

页面不再直接更新 `reverse_signals` 表，而是统一走：

```bash
curl -X POST "https://quant.lzw-glory.top/api/custom/ibkr/reverse/dispatch" \
  -H "Content-Type: application/json" \
  -d '{
    "reverse_id": "reverse_record_id",
    "action": "execute",
    "reason": "page requested"
  }'
```

### `action`

- `execute`：保留 `pending`，写入 `manual_requested`，由 IBKR 尽快处理
- `cancel`：直接将该 reverse 置为 `cancelled`

---

## `reverse/ack`

IBKR 完成后统一回写：

```bash
curl -X POST "https://quant.lzw-glory.top/api/custom/ibkr/reverse/ack" \
  -H "Content-Type: application/json" \
  -d '{
    "signal_id": "reverse_record_id",
    "status": "confirmed",
    "reason": "processed by QC",
    "order_id": "IB_test_AAPL_20260331_093000_sig_entry",
    "broker_order_id": "IB_test_AAPL_20260331_093000_sig_entry",
    "order_unique_id": "test_AAPL_20260331_093000_sig_entry",
    "signal_id_orig": "AAPL_20260331_093000_sig",
    "trade_group_id": "test_AAPL_20260331_093000_sig_entry",
    "entry_order_unique_id": "test_AAPL_20260331_093000_sig_entry",
    "executed_action": "adjust_sl",
    "result_status": "updated",
    "target_state": "filled_position",
    "target_order_status": "Filled",
    "current_direction": "long",
    "old_sl": 98.0,
    "new_sl": 99.2,
    "old_tp": 105.0,
    "new_tp": 105.0,
    "entry_price": 100.1,
    "quantity": 100,
    "stop_loss": 99.2,
    "take_profit": 105.0
  }'
```

---

## 场景覆盖

### 1. 信号反转

`webhook/tv` 写入新信号时，若发现同标的存在反方向 `Entry Submitted/Filled`：

- 自动写入 `reverse_signals`
- 自动补齐交易组 / 主单 / broker id
- 发送 reverse 飞书卡片到独立群组

### 2. 指标反转

`reverse/calculate` 会先定位当前活跃交易目标，再决定是否写入 reverse。

### 3. 页面请求执行

页面点击“执行”只会写 `manual_requested`，不会伪造 IBKR 已执行状态。

### 4. IBKR 执行结果

IBKR 会根据 `action_type` 执行：

- `cancel`
- `close`
- `adjust_sl`
- `adjust_tp`

并在 `reverse/ack` 中回写 old/new 价格和最终结果。

---

## 与测试脚本的映射

`pb-flow.sh` 中：

- `A` 生成 reverse signal，可选强制 `force_action_type`
- `B` 查询 `reverse/list`
- `C` 先 `dispatch execute`，再模拟 IBKR `reverse/ack`
- `H` 在保留当前交易组的情况下发送反向新信号，直接触发 `signal_conflict`

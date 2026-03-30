# 逆向信号 API

## 端点

| 方法 | 端点 | 说明 |
|------|------|------|
| `POST` | `/api/custom/reverse/calculate` | 根据最新指标生成逆向信号 |
| `GET` | `/api/custom/reverse/pending` | 拉取待处理逆向信号 |
| `POST` | `/api/custom/reverse/ack` | QC 回写处理结果 |

---

## 逆向信号和交易组的关系

逆向信号不应该只指向“某一条订单字符串”，而应该能落回整笔交易。

因此：

- `reverse/pending` 会返回 `trade_group_id`
- `reverse/pending` 会返回 `entry_order_unique_id`
- `reverse/ack` 也支持把这两个字段写回 `reverse_signals.extra`

这样页面和测试脚本才能把 reverse signal 和对应主单 / 子单链路连起来。

---

## `reverse/pending` 响应关键字段

```json
{
  "signals": [
    {
      "id": "reverse_record_id",
      "symbol": "AAPL",
      "direction": "long",
      "source": "indicator",
      "priority": 5,
      "signal_id": "AAPL_20260331_093000_sig",
      "order_id": "IB_test_AAPL_20260331_093000_sig_entry",
      "trade_group_id": "test_AAPL_20260331_093000_sig_entry",
      "entry_order_unique_id": "test_AAPL_20260331_093000_sig_entry",
      "strength": "strong",
      "score": 8,
      "action_type": "close",
      "status": "pending"
    }
  ]
}
```

---

## `reverse/ack` 请求示例

```bash
curl -X POST "https://pb.lzw-glory.top/api/custom/reverse/ack" \
  -H "Content-Type: application/json" \
  -d '{
    "signal_id": "reverse_record_id",
    "status": "confirmed",
    "reason": "processed by QC",
    "order_id": "IB_test_AAPL_20260331_093000_sig_entry",
    "signal_id_orig": "AAPL_20260331_093000_sig",
    "trade_group_id": "test_AAPL_20260331_093000_sig_entry",
    "entry_order_unique_id": "test_AAPL_20260331_093000_sig_entry"
  }'
```

### 字段说明

| 字段 | 说明 |
|------|------|
| `signal_id` | `reverse_signals.id` |
| `status` | `confirmed` / `cancelled` / `expired` |
| `reason` | 处理说明 |
| `order_id` | 对应主单的 broker 订单 ID |
| `signal_id_orig` | 原始交易信号 ID |
| `trade_group_id` | 交易组 ID |
| `entry_order_unique_id` | 主入场单 unique id |

---

## 触发来源

### 1. 指标型 reverse signal

`POST /api/custom/reverse/calculate`

- 从 `indicators.extra` 读取技术指标
- 计算 `score`
- 根据分数映射 `action_type`

### 2. 交易信号冲突

当新信号方向与当前持仓 / 挂单冲突时，也可能生成 reverse signal。

这时 QC 在处理完成后，应把交易组定位字段带回 `reverse/ack`。

---

## 与测试脚本的映射

`pb-flow.sh` 中：

- `A` 生成 reverse signal
- `B` 查询 pending reverse signal
- `C` 用 `order_id + trade_group_id + entry_order_unique_id` 回写 ack

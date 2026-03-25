# 逆向信号 API

## 目录
- [端点总览](#端点总览)
- [计算逆向信号](#计算逆向信号)
- [获取待处理逆向信号](#获取待处理逆向信号)
- [确认逆向信号处理](#确认逆向信号处理)
- [自动检测机制](#自动检测机制)
- [评分规则](#评分规则)

---

## 端点总览

| 方法 | 端点 | 认证 | 说明 |
|------|------|------|------|
| POST | `/api/custom/reverse/calculate` | - | 从指标计算逆向信号 |
| GET | `/api/custom/reverse/pending` | - | 获取待处理逆向信号 |
| POST | `/api/custom/reverse/ack` | - | 标记处理完成 |

---

## 计算逆向信号

根据技术指标计算逆向信号分数，并写入 `reverse_signals` 表。

### 请求

```
POST /api/custom/reverse/calculate
```

### 请求示例

```bash
curl -X POST "https://pb.lzw-glory.top/api/custom/reverse/calculate" \
    -H "Content-Type: application/json" \
    -d '{
      "symbol": "AAPL",
      "direction": "long"
    }'
```

### 请求字段

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `symbol` | string | 是 | 股票代码 |
| `direction` | string | 是 | `"long"`（持有多头找反转）或 `"short"`（持有空头找反转） |

### 响应

```json
{
  "success": true,
  "signal": {
    "id": "record_id",
    "symbol": "AAPL",
    "direction": "long",
    "strength": "medium",
    "score": 5,
    "action_type": "close",
    "triggered_signals": ["背离", "分形/SD通道"]
  }
}
```

### 强度等级

| 强度 | 分数范围 | 建议操作 |
|------|----------|----------|
| `weak` | 0-2 | 观察 |
| `medium` | 3-5 | 调整止损 |
| `strong` | ≥6 | 平仓 |

---

## 获取待处理逆向信号

查询所有状态为 `pending` 的逆向信号。

### 请求

```
GET /api/custom/reverse/pending
```

### 请求示例

```bash
curl -X GET "https://pb.lzw-glory.top/api/custom/reverse/pending"
```

### 响应

```json
{
  "signals": [
    {
      "id": "record_id",
      "symbol": "AAPL",
      "direction": "long",
      "source": "indicator",
      "priority": 5,
      "signal_id": "xxx",
      "origin_signal_id": "signal_aapl_long_20260202",
      "order_id": "order_xxx",
      "strength": "medium",
      "score": 5,
      "action_type": "close",
      "status": "pending",
      "reason": "",
      "triggered_signals": ["背离", "分形/SD通道"],
      "crsi": 75.5,
      "obv_rsi": 62.3,
      "vwap_dist": 2.5,
      "close": 185.50,
      "bar_time_ms": 1738411200000,
      "created": "2026-02-02 10:00:00"
    }
  ]
}
```

---

## 确认逆向信号处理

QC 处理完逆向信号后回调，更新状态。

### 请求

```
POST /api/custom/reverse/ack
```

### 请求示例

```bash
curl -X POST "https://pb.lzw-glory.top/api/custom/reverse/ack" \
    -H "Content-Type: application/json" \
    -d '{
      "signal_id": "reverse_signal_id_123",
      "status": "confirmed",
      "reason": "Processed by QC",
      "order_id": "order_aapl_entry_20260202_1",
      "signal_id_orig": "signal_aapl_long_20260202"
    }'
```

### 请求字段

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `signal_id` | string | 是 | reverse_signals 表的 `id` |
| `status` | string | 否 | `"confirmed"` / `"cancelled"` / `"expired"` |
| `reason` | string | 否 | 处理原因 |
| `order_id` | string | 否 | 关联的订单ID |
| `signal_id_orig` | string | 否 | 原始信号ID |

### 状态流转

```
pending → confirmed  (QC 已处理)
pending → cancelled  (QC 忽略)
pending → expired    (超时)
```

---

## 自动检测机制

### 触发时机

1. **指标计算触发**：QC 定期调用 `/api/custom/reverse/calculate`
2. **信号冲突触发**：新信号来时自动检测（见下方）

### 信号冲突检测（webhook_tv.pb.js）

当收到新信号时，自动检测是否有冲突持仓：

```
新信号方向 ≠ 持仓方向 → 写入 reverse_signals
```

| 持仓状态 | 订单状态 | action_type |
|----------|----------|-------------|
| `Filled` | 已成交 | `close` |
| `Submitted` | 挂单中 | `cancel` |

### 优先级

| 来源 | priority | 说明 |
|------|----------|------|
| `signal` | 1 | 信号冲突（最高） |
| `indicator` | 5 | 指标计算 |

---

## 评分规则

根据多个指标综合评分：

| 指标 | 条件 | 分数 | 说明 |
|------|------|------|------|
| cRSI 超买超卖 | long时 cRSI>70 或 short时 cRSI<30 | +2 | 极端超买/超卖 |
| cRSI/OBV 背离 | 底背离(多) 或 顶背离(空) | +3 | 强劲反转信号 |
| 分形/SD通道 | fractal 或 sd_channel 反向信号 | +2 | 结构反转 |
| VWAP偏离 | \|vwap_dist\| > 2% | +1 | 极端偏离 |
| EMA 触碰 | ema_bear_touch(多) 或 ema_bull_touch(空) | +1 | EMA 压力/支撑 |

### 示例计算

```
AAPL 多头持仓，指标显示：
- cRSI = 75 (>70) → +2
- crsi_bear_div = true → +3
- fractal_bear = true → +2
- vwap_dist = 2.5 (>2) → +1
----------------------------------------
总分 = 8 → strength = "strong" → action_type = "close"
```

---

## action_type 操作建议

| action_type | 说明 | QC 应执行 |
|--------------|------|-----------|
| `close` | 平仓 | 市价平掉全部持仓 |
| `adjust_sl` | 调整止损 | 收紧止损至成本价附近 |
| `cancel` | 取消挂单 | 取消反向挂单 |

---

## 飞书通知

当 score ≥ 配置的阈值（默认 6）时，自动发送飞书通知：

```
⚠️ 指标逆向信号 - AAPL

标的: AAPL
方向: 多 📈
强度: strong (score=8)
触发: cRSI超买, 背离, 分形/SD通道
时间: 2026-02-02 10:00:00
```

---

## 相关文档

- [Webhook API](./webhook.md) - 了解信号如何产生
- [信号管理 API](./signals.md) - 了解信号状态流转
- [订单管理 API](./orders.md) - 了解逆向信号如何触发订单操作
- [配置参考](./config.md) - 了解逆向信号阈值配置

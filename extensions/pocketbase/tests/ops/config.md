# 配置参考

## 目录
- [配置表结构](#配置表结构)
- [信号相关配置](#信号相关配置)
- [逆向信号配置](#逆向信号配置)
- [系统监控配置](#系统监控配置)
- [飞书相关配置](#飞书相关配置)
- [配置管理 API](#配置管理-api)

---

## 配置表结构

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | string | 记录ID |
| `key` | string | 配置键（唯一） |
| `value` | string | 配置值 |
| `created` | datetime | 创建时间 |
| `updated` | datetime | 更新时间 |

---

## 信号相关配置

### signal_validity_minutes

信号有效期（分钟），超时未操作的 `pending` 信号自动标记为 `expired`。

```bash
# 设置信号有效期为 30 分钟
curl -X POST "https://pb.lzw-glory.top/api/collections/config/records" \
    -H "Content-Type: application/json" \
    -d '{
      "key": "signal_validity_minutes",
      "value": "30"
    }'
```

| 值 | 说明 |
|----|------|
| `30` | 默认，30分钟后过期 |
| `60` | 1小时 |
| `5` | 5分钟（激进） |

---

### signal_auto_confirm

信号自动确认开关。

```bash
# 关闭自动确认，需要手动确认
curl -X POST "https://pb.lzw-glory.top/api/collections/config/records" \
    -H "Content-Type: application/json" \
    -d '{
      "key": "signal_auto_confirm",
      "value": "false"
    }'
```

| 值 | 信号初始状态 |
|----|--------------|
| `"true"` 或未配置 | `pending` |
| `"false"` | `awaiting_confirm` |

---

### signal_action_token

飞书按钮权限验证 Token（暂时关闭）。

```bash
curl -X POST "https://pb.lzw-glory.top/api/collections/config/records" \
    -H "Content-Type: application/json" \
    -d '{
      "key": "signal_action_token",
      "value": "your_secure_token_here"
    }'
```

> 注意：认证功能暂时关闭，此配置暂时无效。

---

## 逆向信号配置

### reverse_signal_threshold

逆向信号飞书通知阈值（分数）。

```bash
curl -X POST "https://pb.lzw-glory.top/api/collections/config/records" \
    -H "Content-Type: application/json" \
    -d '{
      "key": "reverse_signal_threshold",
      "value": "6"
    }'
```

| 值 | 行为 |
|----|------|
| `6` | 默认，score ≥ 6 才发送飞书通知 |
| `3` | 更敏感，score ≥ 3 就通知 |
| `10` | 仅严重情况 |

---

## 系统监控配置

### system_monitor_host_load_consecutive_count

主机 `load / CPU` 连续命中告警阈值。

```bash
curl -X POST "https://pb.lzw-glory.top/api/collections/config/records" \
    -H "Content-Type: application/json" \
    -d '{
      "key": "system_monitor_host_load_consecutive_count",
      "value": "2"
    }'
```

| 值 | 行为 |
|----|------|
| `1` | 保持旧行为，单次命中 `host_load_high/critical` 就告警 |
| `2` | 默认；PocketBase Monitor Cron 每 5 分钟检查一次，约连续 10 分钟异常才告警 |
| `3` | 更保守；约连续 15 分钟异常才告警 |

> 仅影响 `host_load_high` / `host_load_critical`，不影响 Gateway、WebSocket、Session 等即时告警。

---

### system_monitor_ws_message_age_*

WebSocket 最近一条消息静默阈值，按交易时段区分：

- `regular`：默认 `warning=60s`、`critical=180s`
- `close_transition / afterhours`：默认 `warning=600s`、`critical=1200s`
- `closed`：不触发 `market_data_silent*`

```bash
curl -X POST "https://pb.lzw-glory.top/api/collections/config/records" \
    -H "Content-Type: application/json" \
    -d '{
      "key": "system_monitor_ws_message_age_late_session_warn_sec",
      "value": "600"
    }'
```

| key | 默认值 | 行为 |
|-----|--------|------|
| `system_monitor_ws_message_age_regular_warn_sec` | `"60"` | 盘中超过多少秒触发 `market_data_silent` |
| `system_monitor_ws_message_age_regular_critical_sec` | `"180"` | 盘中超过多少秒触发 `market_data_silent_critical` |
| `system_monitor_ws_message_age_late_session_warn_sec` | `"600"` | 收盘过渡 / 盘后超过多少秒触发 `market_data_silent` |
| `system_monitor_ws_message_age_late_session_critical_sec` | `"1200"` | 收盘过渡 / 盘后超过多少秒触发 `market_data_silent_critical` |

> 同一时段内必须满足 `critical > warn`；若配置非法，会自动回退到默认值。

---

## 飞书相关配置

历史 PocketBase `feishu.js` 已删除；PocketBase 侧只剩 `feishu.pb.js` no-op 壳。下面的 Webhook 分组仅保留为迁移 / 排障参考。

### Webhook 分组

| 用途 | Webhook URL |
|------|-------------|
| 信号通知 | `https://open.feishu.cn/open-apis/bot/v2/hook/298ce054-9666-4a30-a59b-b14ac0faa8f1` |
| 订单通知 | `https://open.feishu.cn/open-apis/bot/v2/hook/eee38484-b055-483b-87c8-b918ec8a178d` |
| 异常/逆向信号 | `https://open.feishu.cn/open-apis/bot/v2/hook/98f6f9a5-5d5a-4974-b363-5bc6384aadd4` |

---

## 配置管理 API

### 查看所有配置

```bash
curl -X GET "https://pb.lzw-glory.top/api/collections/config/records"
```

### 查看单个配置

```bash
curl -X GET "https://pb.lzw-glory.top/api/collections/config/records?filter=key='signal_auto_confirm'"
```

### 创建配置

```bash
curl -X POST "https://pb.lzw-glory.top/api/collections/config/records" \
    -H "Content-Type: application/json" \
    -d '{
      "key": "your_key",
      "value": "your_value"
    }'
```

### 读取配置

```bash
curl -X GET "https://pb.lzw-glory.top/api/collections/config/records?filter=key='your_key'"
```

### 更新配置

```bash
curl -X PATCH "https://pb.lzw-glory.top/api/collections/config/records/CONFIG_ID" \
    -H "Content-Type: application/json" \
    -d '{
      "value": "new_value"
    }'
```

### 删除配置

```bash
curl -X DELETE "https://pb.lzw-glory.top/api/collections/config/records/CONFIG_ID"
```

---

## 完整配置列表

| key | 默认值 | 说明 |
|-----|--------|------|
| `signal_validity_minutes` | `"30"` | 信号有效期（分钟） |
| `signal_auto_confirm` | `"true"` | 自动确认信号 |
| `signal_action_token` | `""` | 飞书按钮验证 Token |
| `reverse_signal_threshold` | `"6"` | 逆向信号通知阈值 |
| `system_monitor_host_load_consecutive_count` | `"2"` | Host load 连续命中告警阈值 |
| `system_monitor_ws_message_age_regular_warn_sec` | `"60"` | WebSocket 盘中 warning 阈值（秒） |
| `system_monitor_ws_message_age_regular_critical_sec` | `"180"` | WebSocket 盘中 critical 阈值（秒） |
| `system_monitor_ws_message_age_late_session_warn_sec` | `"600"` | WebSocket 盘后 warning 阈值（秒） |
| `system_monitor_ws_message_age_late_session_critical_sec` | `"1200"` | WebSocket 盘后 critical 阈值（秒） |

---

## 相关文档

- [Webhook API](./webhook.md) - 了解配置如何影响信号处理
- [信号管理 API](./ibkr_signals.md) - 了解信号状态流转
- [订单管理 API](./orders.md) - 了解订单相关配置
- [逆向信号 API](./reverse_signals.md) - 了解逆向信号阈值配置

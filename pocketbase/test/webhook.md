# TradingView Webhook API

## 目录
- [端点信息](#端点信息)
- [请求格式](#请求格式)
- [Signal 信号](#signal-信号)
- [Indicator 指标](#indicator-指标)
- [响应格式](#响应格式)

---

## 端点信息

| 属性 | 值 |
|------|-----|
| **URL** | `POST https://pb.lzw-glory.top/webhook/tv` |
| **认证** | 无需认证 |
| **频率限制** | TradingView 建议 1-5秒/次 |

---

## 请求格式

```json
{
  "type": "signal" | "indicator",
  // ... 其他字段
}
```

根据 `type` 字段区分写入**信号表**还是**指标表**。

---

## Signal 信号

TradingView 发送交易信号时使用。

### 请求示例

```bash
curl -X POST "https://pb.lzw-glory.top/webhook/tv" \
    -H "Content-Type: application/json" \
    -d '{
      "type": "signal",
      "symbol": "AAPL",
      "direction": "long",
      "signal": "trend_sdUpper",
      "limit_price": 185.00,
      "entry": 185.00,
      "stop_loss": 183.00,
      "take_profit": 190.00,
      "rr": "2.5:1",
      "shares": 100,
      "us_time": "2026-02-01 10:00:00",
      "cn_time": "2026-02-01 23:00:00",
      "signal_id": "signal_aapl_long_20260201_trend_U",
      "exchange": "NASDAQ",
      "interval": "5",
      "extra": {
        "industry": "Technology",
        "reason": "SD上轨→顺势做多(fractal↑+EMA-touch↑)",
        "sd_zone": "upper",
        "sd_trend": "trend",
        "dtp_dir": "up",
        "dtp_phase": "accumulation",
        "crsi_state": "normal",
        "day_change_pct": 1.25,
        "prev_close_change_pct": 0.85,
        "change_7d": 5.32,
        "script_tag": "SAC_v2_diag_20260303",
        "chart_tf": "15",
        "bar_time_ms": 1738411200000,
        "bar_index": 100,
        "close": 185.50,
        "atr": 2.35,
        "atr_pct": 1.27,
        "sl_dist_pct": 1.08,
        "sl_atr_ratio": 1.06
      }
    }'
```

### 必填字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `type` | string | 固定为 `"signal"` |
| `symbol` | string | 股票代码，如 `"AAPL"` |
| `direction` | string | `"long"` 或 `"short"` |
| `entry` | number | 入场价格（正数） |
| `stop_loss` | number | 止损价格（正数） |
| `take_profit` | number | 止盈价格（正数） |
| `signal_id` | string | 全局唯一信号ID |

### 可选字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `limit_price` | number | 限价（默认等于 entry） |
| `shares` | number | 股数 |
| `rr` | string | 风险回报比，如 `"2.5:1"` |
| `signal` | string | 信号类型标识 |
| `exchange` | string | 交易所，如 `"NASDAQ"` |
| `interval` | string | 策略时间周期 |
| `us_time` | string | 美国时间 `"YYYY-MM-DD HH:MM:SS"` |
| `cn_time` | string | 中国时间 `"YYYY-MM-DD HH:MM:SS"` |
| `extra` | object | 附加数据（见下方） |

### extra 常用字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `reason` | string | 信号原因描述 |
| `day_change_pct` | number | 当日涨跌幅 % |
| `prev_close_change_pct` | number | 前一日涨跌幅 % |
| `change_7d` | number | 近7日涨跌幅 % |
| `atr` | number | ATR 值 |
| `atr_pct` | number | ATR 占价格百分比 % |
| `sl_atr_ratio` | number | 止损 ATR 倍数 |
| `bar_time_ms` | number | K线时间戳（毫秒） |
| `bar_index` | number | K线索引 |

### 自动处理逻辑

1. **去重**：若 `signal_id` 已存在，跳过并返回 `"duplicate, skipped"`
2. **初始状态**：根据 `config.signal_auto_confirm` 决定：
   - `"true"` 或未配置 → `status = "pending"`
   - `"false"` → `status = "awaiting_confirm"`
3. **飞书通知**：自动发送带确认/取消按钮的交互卡片
4. **逆向信号检测**：检查是否有冲突持仓，自动写入 `reverse_signals` 表

---

## Indicator 指标

TradingView 发送技术指标数据时使用。

### 请求示例

```bash
curl -X POST "https://pb.lzw-glory.top/webhook/tv" \
    -H "Content-Type: application/json" \
    -d '{
      "type": "indicator",
      "symbol": "SPY",
      "exchange": "NYSE",
      "interval": "5",
      "script_tag": "SAC_v2_diag_20260303",
      "us_time": "2026-02-01 09:00:00",
      "cn_time": "2026-02-01 22:00:00",
      "bar_time_ms": 1738407600000,
      "bar_index": 50,
      "extra": {
        "close": 520.50,
        "high": 522.00,
        "low": 518.00,
        "open": 519.00,
        "volume": 100000000,
        "day_change_pct": 1.25,
        "vwap": 520.00,
        "ema_trend": "bullish",
        "trend_dir": 1,
        "atr": 3.5,
        "atr_pct": 0.67,
        "crsi": 65.0,
        "crsi_bull_div": false,
        "crsi_bear_div": false,
        "fractal_bull": false,
        "fractal_bear": false,
        "ema_bull_touch": false,
        "ema_bear_touch": false
      }
    }'
```

### 去重规则

以 `bar_time_ms + symbol + interval` 作为去重 key，已存在则跳过。

### extra 常用字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `close` | number | 收盘价 |
| `high` | number | 最高价 |
| `low` | number | 最低价 |
| `open` | number | 开盘价 |
| `volume` | number | 成交量 |
| `vwap` | number | VWAP |
| `ema_fast/slow/longest` | number | EMA 系列 |
| `trend_dir` | number | 趋势方向（1=多头，-1=空头） |
| `crsi` | number | CRSI 指标值 |
| `crsi_bull_div` | boolean | CRSI 底背离 |
| `crsi_bear_div` | boolean | CRSI 顶背离 |
| `fractal_bull` | boolean | 分形做多信号 |
| `fractal_bear` | boolean | 分形做空信号 |
| `ema_bull_touch` | boolean | EMA 支撑 |
| `ema_bear_touch` | boolean | EMA 压力 |

---

## 响应格式

### 成功

```json
{ "ok": true, "type": "signal" }
{ "ok": true, "type": "indicator" }
{ "ok": true, "msg": "duplicate, skipped" }
{ "ok": true, "msg": "duplicate indicator, skipped", "key": "xxx" }
```

### 失败

```json
{
  "ok": false,
  "error": "Missing required field: symbol",
  "signal_id": "xxx"
}
```

---

## 相关文档

- [信号管理 API](./signals.md)
- [订单管理 API](./orders.md)
- [逆向信号 API](./reverse_signals.md)
- [配置参考](./config.md)

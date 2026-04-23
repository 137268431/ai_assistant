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
| **URL** | `POST https://quant.lzw-glory.top/webhook/tv` |
| **交易系统入口域名** | `https://quant.lzw-glory.top` |
| **PocketBase/auth/collections 域名** | `https://pb.lzw-glory.top` |
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
curl -X POST "https://quant.lzw-glory.top/webhook/tv" \
    -H "Content-Type: application/json" \
    -d '{
      "type": "signal",
      "symbol": "AAPL",
      "direction": "long",
      "signal": "trend_sdUpper",
      "limit_price": 244,
      "entry": 242.79,
      "stop_loss": 241.14,
      "take_profit": 245.26,
      "rr": "1.5:1",
      "shares": 42,
      "us_time": "2026-02-02 10:00:00",
      "cn_time": "2026-02-02 23:00:00",
      "signal_id": "AAPL_20260202_1000_trend_U",
      "exchange": "NASDAQ",
      "interval": "5",
      "extra": {
        "industry": "Technology",
        "reason": "SD上轨→顺势做多(fractal↑+EMA-touch↑[ema的慢线]+div↑[cRSI+OBV])",
        "sd_zone": "normal",
        "sd_trend": "up",
        "dtp_dir": "bullish",
        "dtp_phase": "confirmed",
        "crsi_state": "normal",
        "day_change_pct": 1.27,
        "prev_close_change_pct": 0.92,
        "change_7d": -1.17,
        "script_tag": "SAC_v2_diag_20260303",
        "chart_tf": "5",
        "bar_time_ms": 1770015600000,
        "bar_index": 20044,
        "close": 243.61,
        "atr": 0.5498,
        "atr_pct": 0.23,
        "sl_dist_pct": 0.68,
        "sl_atr_ratio": 3
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
   - `"false"` → `status = "awaiting_confirm"`（需手动确认）
   - 其他值或未配置 → `status = "pending"`（自动确认）
3. **飞书通知**：
   - `awaiting_confirm` 状态：发送带 ✅确认 / ❌拒绝 按钮的交互卡片
   - `pending` 状态：发送 ⚙️自动确认 文案提示的卡片
4. **逆向信号检测**：检查是否有冲突持仓，自动写入 `reverse_signals` 表

---

## Indicator 指标

TradingView 发送技术指标数据时使用。

### 请求示例

**AAPL 股票指标：**
```bash
curl -X POST "https://quant.lzw-glory.top/webhook/tv" \
    -H "Content-Type: application/json" \
    -d '{
      "type": "indicator",
      "symbol": "AAPL",
      "exchange": "NASDAQ",
      "interval": "5",
      "script_tag": "SAC_v2_diag_20260303",
      "us_time": "2026-02-02 10:00:00",
      "cn_time": "2026-02-02 23:00:00",
      "bar_time_ms": 1770015600000,
      "bar_index": 20044,
      "extra": {
        "close": 243.61,
        "high": 244.50,
        "low": 242.00,
        "open": 242.30,
        "volume": 50000000,
        "day_change_pct": 1.27,
        "prev_close_change_pct": 0.92,
        "change_7d": -1.17,
        "vwap": 243.20,
        "vwap_upper1": 244.00,
        "vwap_lower1": 242.40,
        "vwap_bullish": true,
        "ema_fast": 243.00,
        "ema_slow": 242.50,
        "ema_trend": 1,
        "ema_longest": 240.00,
        "slope_slow": 0.25,
        "trend_dir": 1,
        "sd_zone": 1,
        "sd_trend": 1,
        "dtp_avg": 0.42,
        "dtp_atr": 0.55,
        "dtp_dir": 1,
        "dtp_phase": "confirmed",
        "atr": 0.55,
        "atr_raw": 0.5498,
        "atr_pct": 0.23,
        "crsi": 72.0,
        "crsi_ub": 80,
        "crsi_db": 20,
        "crsi_ob": true,
        "crsi_os": false,
        "crsi_bull_div": true,
        "crsi_bear_div": false,
        "fractal_bull": true,
        "fractal_bear": false,
        "sd_lower": false,
        "sd_upper": true,
        "ema_bull_touch": true,
        "ema_bear_touch": false
      }
    }'
```

**SPY 大盘指标：**
```bash
curl -X POST "https://quant.lzw-glory.top/webhook/tv" \
    -H "Content-Type: application/json" \
    -d '{
      "type": "indicator",
      "symbol": "SPY",
      "exchange": "AMEX",
      "interval": "5",
      "script_tag": "SAC_v2_diag_20260303",
      "us_time": "2026-02-02 09:30:00",
      "cn_time": "2026-02-02 22:30:00",
      "bar_time_ms": 1738409400000,
      "bar_index": 100,
      "extra": {
        "close": 520.50,
        "high": 522.00,
        "low": 518.00,
        "open": 519.00,
        "volume": 100000000,
        "day_change_pct": 1.25,
        "prev_close_change_pct": 0.85,
        "change_7d": 2.30,
        "vwap": 520.00,
        "vwap_upper1": 521.00,
        "vwap_lower1": 519.00,
        "vwap_bullish": true,
        "ema_fast": 519.50,
        "ema_slow": 518.20,
        "ema_trend": 1,
        "ema_longest": 515.00,
        "slope_slow": 0.15,
        "trend_dir": 1,
        "sd_zone": 1,
        "sd_trend": 1,
        "dtp_avg": 0.52,
        "dtp_atr": 0.48,
        "dtp_dir": 1,
        "dtp_phase": "confirmed",
        "atr": 3.50,
        "atr_raw": 3.52,
        "atr_pct": 0.67,
        "crsi": 65.0,
        "crsi_ub": 80,
        "crsi_db": 20,
        "crsi_ob": true,
        "crsi_os": false,
        "crsi_bull_div": true,
        "crsi_bear_div": false,
        "fractal_bull": true,
        "fractal_bear": false,
        "sd_lower": true,
        "sd_upper": false,
        "ema_bull_touch": true,
        "ema_bear_touch": false
      }
    }'
```

**QQQ 纳指指标：**
```bash
curl -X POST "https://quant.lzw-glory.top/webhook/tv" \
    -H "Content-Type: application/json" \
    -d '{
      "type": "indicator",
      "symbol": "QQQ",
      "exchange": "NASDAQ",
      "interval": "5",
      "script_tag": "SAC_v2_diag_20260303",
      "us_time": "2026-02-02 09:30:00",
      "cn_time": "2026-02-02 22:30:00",
      "bar_time_ms": 1738409400000,
      "bar_index": 100,
      "extra": {
        "close": 440.50,
        "high": 442.00,
        "low": 438.00,
        "open": 439.00,
        "volume": 80000000,
        "day_change_pct": -2.39,
        "prev_close_change_pct": -1.85,
        "change_7d": -3.20,
        "vwap": 440.00,
        "vwap_upper1": 441.50,
        "vwap_lower1": 438.50,
        "vwap_bullish": false,
        "ema_fast": 442.00,
        "ema_slow": 445.50,
        "ema_trend": -1,
        "ema_longest": 450.00,
        "slope_slow": -0.35,
        "trend_dir": -1,
        "sd_zone": -1,
        "sd_trend": -1,
        "dtp_avg": 0.62,
        "dtp_atr": 0.58,
        "dtp_dir": -1,
        "dtp_phase": "confirmed",
        "atr": 4.20,
        "atr_raw": 4.18,
        "atr_pct": 0.95,
        "crsi": 28.0,
        "crsi_ub": 80,
        "crsi_db": 20,
        "crsi_ob": false,
        "crsi_os": true,
        "crsi_bull_div": false,
        "crsi_bear_div": true,
        "fractal_bull": false,
        "fractal_bear": true,
        "sd_lower": false,
        "sd_upper": true,
        "ema_bull_touch": false,
        "ema_bear_touch": true
      }
    }'
```

**VIX 恐慌指数：**
```bash
curl -X POST "https://quant.lzw-glory.top/webhook/tv" \
    -H "Content-Type: application/json" \
    -d '{
      "type": "indicator",
      "symbol": "VIX",
      "exchange": "CBOE",
      "interval": "5",
      "script_tag": "SAC_v2_diag_20260303",
      "us_time": "2026-02-02 09:30:00",
      "cn_time": "2026-02-02 22:30:00",
      "bar_time_ms": 1738409400000,
      "bar_index": 100,
      "extra": {
        "close": 18.50,
        "high": 19.20,
        "low": 17.80,
        "open": 18.00,
        "volume": 0,
        "day_change_pct": 7.60,
        "prev_close_change_pct": 3.20,
        "change_7d": -5.10,
        "vwap": 18.40,
        "vwap_upper1": 19.00,
        "vwap_lower1": 17.50,
        "vwap_bullish": false,
        "ema_fast": 17.80,
        "ema_slow": 16.50,
        "ema_trend": 1,
        "ema_longest": 15.00,
        "slope_slow": 0.45,
        "trend_dir": 1,
        "sd_zone": 1,
        "sd_trend": 1,
        "dtp_avg": 0.32,
        "dtp_atr": 0.28,
        "dtp_dir": 1,
        "dtp_phase": "confirmed",
        "atr": 0.85,
        "atr_raw": 0.82,
        "atr_pct": 4.59,
        "crsi": 75.0,
        "crsi_ub": 80,
        "crsi_db": 20,
        "crsi_ob": true,
        "crsi_os": false,
        "crsi_bull_div": false,
        "crsi_bear_div": true,
        "fractal_bull": false,
        "fractal_bear": false,
        "sd_lower": true,
        "sd_upper": false,
        "ema_bull_touch": true,
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
| `day_change_pct` | number | 当日涨跌幅 % |
| `prev_close_change_pct` | number | 前一日涨跌幅 % |
| `change_7d` | number | 近7日涨跌幅 % |
| `vwap` | number | VWAP 当前价 |
| `vwap_upper1/2` | number | VWAP 通道上轨 |
| `vwap_lower1/2` | number | VWAP 通道下轨 |
| `vwap_bullish` | boolean | VWAP 多头信号 |
| `ema_fast` | number | 快线 EMA |
| `ema_slow` | number | 慢线 EMA |
| `ema_longest` | number | 最长周期 EMA |
| `ema_trend` | number | EMA 趋势方向（1=多头，-1=空头） |
| `slope_slow` | number | 慢线斜率 |
| `trend_dir` | number | 趋势方向（1=多头，-1=空头） |
| `sd_zone` | number | SD 区域（1=上轨，-1=下轨，0=中轨） |
| `sd_trend` | number | SD 趋势方向 |
| `dtp_avg` | number | DTP 平均值 |
| `dtp_atr` | number | DTP ATR |
| `dtp_dir` | number | DTP 方向 |
| `dtp_phase` | string | DTP 阶段（"confirmed"/"forming"） |
| `atr` | number | ATR 值 |
| `atr_raw` | number | ATR 原始值 |
| `atr_pct` | number | ATR 占价格百分比 % |
| `crsi` | number | CRSI 指标值 |
| `crsi_ub` | number | CRSI 上轨 |
| `crsi_db` | number | CRSI 下轨 |
| `crsi_ob` | boolean | CRSI 超买 |
| `crsi_os` | boolean | CRSI 超卖 |
| `crsi_bull_div` | boolean | CRSI 底背离 |
| `crsi_bear_div` | boolean | CRSI 顶背离 |
| `crsi_hid_bull` | boolean | CRSI 隐性底背离 |
| `crsi_hid_bear` | boolean | CRSI 隐性顶背离 |
| `obv_rsi` | number | OBV RSI 震荡指标 |
| `obv_bull_div` | boolean | OBV 底背离 |
| `obv_bear_div` | boolean | OBV 顶背离 |
| `fractal_bull` | boolean | 分形做多信号 |
| `fractal_bear` | boolean | 分形做空信号 |
| `sd_lower` | boolean | SD 下轨触及 |
| `sd_upper` | boolean | SD 上轨触及 |
| `ema_bull_touch` | boolean | EMA 支撑（慢线触及） |
| `ema_bear_touch` | boolean | EMA 压力（慢线触及） |

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

- [信号管理 API](./ibkr_signals.md) - 了解信号接收后的处理流程
- [订单管理 API](./orders.md) - 了解 IBKR 下单后的订单流程
- [逆向信号 API](./reverse_signals.md) - 了解逆向信号检测机制
- [配置参考](./config.md) - 了解系统配置项

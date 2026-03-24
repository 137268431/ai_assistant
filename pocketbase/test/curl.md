# PocketBase API 测试用例

## 目录
- [1. Webhook TV - TradingView 信号/指标接收](#1-webhook-tv---tradingview-信号指标接收)
- [2. 订单管理 API](#2-订单管理-api)
- [3. 信号操作](#3-信号操作)
- [4. 订单操作](#4-订单操作)
- [5. 逆向信号 API](#5-逆向信号-api)
- [6. 配置参考](#6-配置参考)

---

## 1. Webhook TV - TradingView 信号/指标接收

### 1.1 发送交易信号 (Signal)
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

### 1.2 发送交易信号 (Short)
```bash
curl -X POST "https://pb.lzw-glory.top/webhook/tv" \
    -H "Content-Type: application/json" \
    -d '{
      "type": "signal",
      "symbol": "TSLA",
      "direction": "short",
      "signal": "trend_sdLower",
      "limit_price": 250.00,
      "entry": 250.00,
      "stop_loss": 255.00,
      "take_profit": 240.00,
      "rr": "2.0:1",
      "shares": 50,
      "us_time": "2026-02-01 14:30:00",
      "cn_time": "2026-02-02 03:30:00",
      "signal_id": "signal_tsla_short_20260201_trend_L",
      "exchange": "NASDAQ",
      "interval": "5",
      "extra": {
        "industry": "Automotive",
        "reason": "SD下轨→顺势做空(fractal↓+EMA-touch↓)",
        "sd_zone": "lower",
        "sd_trend": "trend",
        "dtp_dir": "down",
        "dtp_phase": "distribution",
        "crsi_state": "oversold",
        "day_change_pct": -2.15,
        "prev_close_change_pct": -1.32,
        "change_7d": -8.45,
        "script_tag": "SAC_v2_diag_20260303",
        "chart_tf": "15",
        "bar_time_ms": 1738424600000,
        "bar_index": 150,
        "close": 249.80,
        "atr": 5.20,
        "atr_pct": 2.08,
        "sl_dist_pct": 2.08,
        "sl_atr_ratio": 0.96
      }
    }'
```

### 1.3 发送技术指标数据 (Indicator) SPY 大盘
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
        "prev_close_change_pct": 0.85,
        "change_7d": 5.32,
        "vwap": 520.00,
        "vwap_upper1": 521.50,
        "vwap_upper2": 522.50,
        "vwap_lower1": 518.50,
        "vwap_lower2": 517.50,
        "vwap_dist": 0.5,
        "vwap_bullish": true,
        "ema_fast": 519.80,
        "ema_slow": 518.50,
        "ema_longest": 516.20,
        "ema_trend": "bullish",
        "slope_slow": 0.025,
        "slope_trend": 0.030,
        "slope_longest": 0.015,
        "ema_bullish": true,
        "ema_bearish": false,
        "trend_dir": 1,
        "sd_reg": 520.00,
        "sd_std_dev": 2.5,
        "sd_zone": 0,
        "sd_trend": "neutral",
        "dtp_avg": 519.50,
        "dtp_atr": 3.5,
        "dtp_dir": "up",
        "dtp_phase": "accumulation",
        "dtp_phase_bars": 8,
        "atr": 3.5,
        "atr_raw": 3.5,
        "atr_pct": 0.67,
        "crsi": 65.0,
        "crsi_ub": 70.0,
        "crsi_db": 30.0,
        "crsi_ob": false,
        "crsi_os": false,
        "obv_rsi": 58.0,
        "crsi_bull_div": false,
        "crsi_bear_div": false,
        "crsi_hid_bull": false,
        "crsi_hid_bear": false,
        "obv_bull_div": false,
        "obv_bear_div": false,
        "obv_hid_bull": false,
        "obv_hid_bear": false,
        "fractal_bull": false,
        "fractal_bear": false,
        "sd_lower": false,
        "sd_upper": false,
        "ema_bull_touch": false,
        "ema_bear_touch": false
      }
    }'
```

### 1.4 发送技术指标数据 (Indicator) AAPL
```bash
curl -X POST "https://pb.lzw-glory.top/webhook/tv" \
    -H "Content-Type: application/json" \
    -d '{
      "type": "indicator",
      "symbol": "AAPL",
      "exchange": "NASDAQ",
      "interval": "5",
      "script_tag": "SAC_v2_diag_20260303",
      "us_time": "2026-02-01 09:30:00",
      "cn_time": "2026-02-01 22:30:00",
      "bar_time_ms": 1738410600000,
      "bar_index": 50,
      "extra": {
        "close": 185.50,
        "high": 186.00,
        "low": 184.80,
        "open": 185.20,
        "volume": 1000000,
        "day_change_pct": 1.25,
        "prev_close_change_pct": 0.85,
        "change_7d": 5.32,
        "vwap": 185.00,
        "vwap_upper1": 186.50,
        "vwap_upper2": 187.20,
        "vwap_lower1": 183.50,
        "vwap_lower2": 182.80,
        "vwap_dist": 0.5,
        "vwap_bullish": true,
        "ema_fast": 184.80,
        "ema_slow": 183.50,
        "ema_longest": 182.20,
        "ema_trend": "bullish",
        "slope_slow": 0.015,
        "slope_trend": 0.020,
        "slope_longest": 0.008,
        "ema_bullish": true,
        "ema_bearish": false,
        "trend_dir": 1,
        "sd_reg": 185.00,
        "sd_std_dev": 1.5,
        "sd_zone": 0,
        "sd_trend": "neutral",
        "dtp_avg": 185.20,
        "dtp_atr": 2.5,
        "dtp_dir": "up",
        "dtp_phase": "accumulation",
        "dtp_phase_bars": 5,
        "atr": 2.5,
        "atr_raw": 2.5,
        "atr_pct": 1.35,
        "crsi": 65.0,
        "crsi_ub": 70.0,
        "crsi_db": 30.0,
        "crsi_ob": false,
        "crsi_os": false,
        "obv_rsi": 58.0,
        "crsi_bull_div": false,
        "crsi_bear_div": false,
        "crsi_hid_bull": false,
        "crsi_hid_bear": false,
        "obv_bull_div": false,
        "obv_bear_div": false,
        "obv_hid_bull": false,
        "obv_hid_bear": false,
        "fractal_bull": false,
        "fractal_bear": false,
        "sd_lower": false,
        "sd_upper": false,
        "ema_bull_touch": false,
        "ema_bear_touch": false
      }
    }'
```

### 1.5 发送技术指标数据 (Indicator) TSLA
```bash
curl -X POST "https://pb.lzw-glory.top/webhook/tv" \
    -H "Content-Type: application/json" \
    -d '{
      "type": "indicator",
      "symbol": "TSLA",
      "exchange": "NASDAQ",
      "interval": "5",
      "script_tag": "SAC_v2_diag_20260303",
      "us_time": "2026-02-01 13:00:00",
      "cn_time": "2026-02-02 02:00:00",
      "bar_time_ms": 1738421400000,
      "bar_index": 80,
      "extra": {
        "close": 249.80,
        "high": 251.50,
        "low": 248.20,
        "open": 250.00,
        "volume": 5000000,
        "day_change_pct": -2.15,
        "prev_close_change_pct": -1.32,
        "change_7d": -8.45,
        "vwap": 249.50,
        "vwap_upper1": 252.00,
        "vwap_upper2": 254.00,
        "vwap_lower1": 247.00,
        "vwap_lower2": 245.00,
        "vwap_dist": -0.5,
        "vwap_bullish": false,
        "ema_fast": 250.80,
        "ema_slow": 252.50,
        "ema_longest": 255.20,
        "ema_trend": "bearish",
        "slope_slow": -0.035,
        "slope_trend": -0.042,
        "slope_longest": -0.025,
        "ema_bullish": false,
        "ema_bearish": true,
        "trend_dir": -1,
        "sd_reg": 249.80,
        "sd_std_dev": 4.2,
        "sd_zone": 0,
        "sd_trend": "neutral",
        "dtp_avg": 250.20,
        "dtp_atr": 5.2,
        "dtp_dir": "down",
        "dtp_phase": "distribution",
        "dtp_phase_bars": 12,
        "atr": 5.2,
        "atr_raw": 5.2,
        "atr_pct": 2.08,
        "crsi": 28.0,
        "crsi_ub": 70.0,
        "crsi_db": 30.0,
        "crsi_ob": false,
        "crsi_os": true,
        "obv_rsi": 42.0,
        "crsi_bull_div": false,
        "crsi_bear_div": true,
        "crsi_hid_bull": false,
        "crsi_hid_bear": true,
        "obv_bull_div": false,
        "obv_bear_div": true,
        "obv_hid_bull": false,
        "obv_hid_bear": true,
        "fractal_bull": false,
        "fractal_bear": true,
        "sd_lower": true,
        "sd_upper": false,
        "ema_bull_touch": false,
        "ema_bear_touch": true
      }
    }'
```

---

## 2. 订单管理 API

### 2.1 Upsert 订单 - 提交 (需认证)
```bash
curl -X POST "https://pb.lzw-glory.top/api/custom/orders/upsert" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJjb2xsZWN0aW9uSWQiOiJwYmNfMzE0MjYzNTgyMyIsImV4cCI6MTg2ODk0MDM2NSwiaWQiOiJsbGRuMGc5cTN6Z2EwczgiLCJyZWZyZXNoYWJsZSI6dHJ1ZSwidHlwZSI6ImF1dGgifQ.3gy1d1Er-GlXkNyRjXNceMIWB77bB20wgbKmT60Z3b4" \
    -d '{
      "unique_id": "order_aapl_entry_20260201_1",
      "order_type": "Entry",
      "order_id": "IB_12345",
      "symbol": "AAPL",
      "direction": "long",
      "quantity": 100,
      "limit_price": 185.00,
      "status": "Submitted",
      "filled_qty": 0,
      "fill_price": 0,
      "signal_id": "signal_aapl_long_20260201",
      "extra": {
        "reason": "Test order",
        "bar_time_ms": 1738411200000
      }
    }'
```

### 2.2 Upsert 订单 - 成交 (需认证)
```bash
curl -X POST "https://pb.lzw-glory.top/api/custom/orders/upsert" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJjb2xsZWN0aW9uSWQiOiJwYmNfMzE0MjYzNTgyMyIsImV4cCI6MTg2ODk0MDM2NSwiaWQiOiJsbGRuMGc5cTN6Z2EwczgiLCJyZWZyZXNoYWJsZSI6dHJ1ZSwidHlwZSI6ImF1dGgifQ.3gy1d1Er-GlXkNyRjXNceMIWB77bB20wgbKmT60Z3b4" \
    -d '{
      "unique_id": "order_aapl_entry_20260201_1",
      "order_type": "Entry",
      "order_id": "IB_12345",
      "symbol": "AAPL",
      "direction": "long",
      "quantity": 100,
      "limit_price": 185.00,
      "status": "Filled",
      "filled_qty": 100,
      "fill_price": 185.20,
      "signal_id": "signal_aapl_long_20260201",
      "pnl": 0,
      "commission": 1.5,
      "rr_ratio": 2.5,
      "extra": {
        "reason": "Order filled",
        "bar_time_ms": 1738411500000
      }
    }'
```

### 2.3 Upsert 订单 - 止盈成交 (需认证)
```bash
curl -X POST "https://pb.lzw-glory.top/api/custom/orders/upsert" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJjb2xsZWN0aW9uSWQiOiJwYmNfMzE0MjYzNTgyMyIsImV4cCI6MTg2ODk0MDM2NSwiaWQiOiJsbGRuMGc5cTN6Z2EwczgiLCJyZWZyZXNoYWJsZSI6dHJ1ZSwidHlwZSI6ImF1dGgifQ.3gy1d1Er-GlXkNyRjXNceMIWB77bB20wgbKmT60Z3b4" \
    -d '{
      "unique_id": "order_aapl_exit_tp_20260201_1",
      "order_type": "Exit",
      "order_id": "IB_67890",
      "symbol": "AAPL",
      "direction": "long",
      "quantity": 100,
      "limit_price": 190.00,
      "status": "Filled",
      "filled_qty": 100,
      "fill_price": 190.00,
      "signal_id": "signal_aapl_long_20260201",
      "pnl": 480,
      "commission": 1.5,
      "rr_ratio": 2.5,
      "extra": {
        "reason": "Take profit reached",
        "exit_type": "take_profit",
        "bar_time_ms": 1738412400000
      }
    }'
```

### 2.4 Upsert 订单 - 止损成交 (需认证)
```bash
curl -X POST "https://pb.lzw-glory.top/api/custom/orders/upsert" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJjb2xsZWN0aW9uSWQiOiJwYmNfMzE0MjYzNTgyMyIsImV4cCI6MTg2ODk0MDM2NSwiaWQiOiJsbGRuMGc5cTN6Z2EwczgiLCJyZWZyZXNoYWJsZSI6dHJ1ZSwidHlwZSI6ImF1dGgifQ.3gy1d1Er-GlXkNyRjXNceMIWB77bB20wgbKmT60Z3b4" \
    -d '{
      "unique_id": "order_aapl_exit_sl_20260201_1",
      "order_type": "Exit",
      "order_id": "IB_67891",
      "symbol": "AAPL",
      "direction": "long",
      "quantity": 100,
      "limit_price": 183.00,
      "status": "Filled",
      "filled_qty": 100,
      "fill_price": 183.00,
      "signal_id": "signal_aapl_long_20260201",
      "pnl": -220,
      "commission": 1.5,
      "rr_ratio": 2.5,
      "extra": {
        "reason": "Stop loss hit",
        "exit_type": "stop_loss",
        "bar_time_ms": 1738413600000
      }
    }'
```

### 2.5 获取待处理订单操作 (需认证)
```bash
curl -X GET "https://pb.lzw-glory.top/api/custom/orders/pending" \
    -H "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJjb2xsZWN0aW9uSWQiOiJwYmNfMzE0MjYzNTgyMyIsImV4cCI6MTg2ODk0MDM2NSwiaWQiOiJsbGRuMGc5cTN6Z2EwczgiLCJyZWZyZXNoYWJsZSI6dHJ1ZSwidHlwZSI6ImF1dGgifQ.3gy1d1Er-GlXkNyRjXNceMIWB77bB20wgbKmT60Z3b4"
```

### 2.6 确认订单操作完成 (需认证)
```bash
curl -X POST "https://pb.lzw-glory.top/api/custom/orders/ack" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJjb2xsZWN0aW9uSWQiOiJwYmNfMzE0MjYzNTgyMyIsImV4cCI6MTg2ODk0MDM2NSwiaWQiOiJsbGRuMGc5cTN6Z2EwczgiLCJyZWZyZXNoYWJsZSI6dHJ1ZSwidHlwZSI6ImF1dGgifQ.3gy1d1Er-GlXkNyRjXNceMIWB77bB20wgbKmT60Z3b4" \
    -d '{
      "unique_id": "order_aapl_entry_20260201_1",
      "result": "completed"
    }'
```

---

## 3. 信号操作

### 3.1 获取待执行信号 (需认证)
```bash
curl -X GET "https://pb.lzw-glory.top/api/custom/signals/pending" \
    -H "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJjb2xsZWN0aW9uSWQiOiJwYmNfMzE0MjYzNTgyMyIsImV4cCI6MTg2ODk0MDM2NSwiaWQiOiJsbGRuMGc5cTN6Z2EwczgiLCJyZWZyZXNoYWJsZSI6dHJ1ZSwidHlwZSI6ImF1dGgifQ.3gy1d1Er-GlXkNyRjXNceMIWB77bB20wgbKmT60Z3b4"
```

### 3.2 获取指定日期待执行信号 (需认证)
```bash
curl -X GET "https://pb.lzw-glory.top/api/custom/signals/pending?date=2026-02-01" \
    -H "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJjb2xsZWN0aW9uSWQiOiJwYmNfMzE0MjYzNTgyMyIsImV4cCI6MTg2ODk0MDM2NSwiaWQiOiJsbGRuMGc5cTN6Z2EwczgiLCJyZWZyZXNoYWJsZSI6dHJ1ZSwidHlwZSI6ImF1dGgifQ.3gy1d1Er-GlXkNyRjXNceMIWB77bB20wgbKmT60Z3b4"
```

### 3.3 确认信号已处理 (需认证)
```bash
curl -X POST "https://pb.lzw-glory.top/api/custom/signals/ack" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJjb2xsZWN0aW9uSWQiOiJwYmNfMzE0MjYzNTgyMyIsImV4cCI6MTg2ODk0MDM2NSwiaWQiOiJsbGRuMGc5cTN6Z2EwczgiLCJyZWZyZXNoYWJsZSI6dHJ1ZSwidHlwZSI6ImF1dGgifQ.3gy1d1Er-GlXkNyRjXNceMIWB77bB20wgbKmT60Z3b4" \
    -d '{
      "signal_id": "signal_aapl_long_20260201",
      "status": "executed",
      "note": "Order placed successfully"
    }'
```

### 3.4 飞书按钮 - 确认信号 (需配置 token)
```bash
curl -X GET "https://pb.lzw-glory.top/webhook/signal/confirm?id=signal_aapl_long_20260201&token=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJjb2xsZWN0aW9uSWQiOiJwYmNfMzE0MjYzNTgyMyIsImV4cCI6MTg2ODk0MDM2NSwiaWQiOiJsbGRuMGc5cTN6Z2EwczgiLCJyZWZyZXNoYWJsZSI6dHJ1ZSwidHlwZSI6ImF1dGgifQ.3gy1d1Er-GlXkNyRjXNceMIWB77bB20wgbKmT60Z3b4"
```

### 3.5 飞书按钮 - 取消信号 (需配置 token)
```bash
curl -X GET "https://pb.lzw-glory.top/webhook/signal/cancel?id=signal_aapl_long_20260201&token=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJjb2xsZWN0aW9uSWQiOiJwYmNfMzE0MjYzNTgyMyIsImV4cCI6MTg2ODk0MDM2NSwiaWQiOiJsbGRuMGc5cTN6Z2EwczgiLCJyZWZyZXNoYWJsZSI6dHJ1ZSwidHlwZSI6ImF1dGgifQ.3gy1d1Er-GlXkNyRjXNceMIWB77bB20wgbKmT60Z3b4"
```

---

## 4. 订单操作

### 4.1 飞书按钮 - 取消订单 (需配置 token)
```bash
curl -X GET "https://pb.lzw-glory.top/webhook/order/cancel?id=order_aapl_entry_20260201_1&token=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJjb2xsZWN0aW9uSWQiOiJwYmNfMzE0MjYzNTgyMyIsImV4cCI6MTg2ODk0MDM2NSwiaWQiOiJsbGRuMGc5cTN6Z2EwczgiLCJyZWZyZXNoYWJsZSI6dHJ1ZSwidHlwZSI6ImF1dGgifQ.3gy1d1Er-GlXkNyRjXNceMIWB77bB20wgbKmT60Z3b4"
```

### 4.2 飞书按钮 - 平仓订单 (需配置 token)
```bash
curl -X GET "https://pb.lzw-glory.top/webhook/order/close?id=order_aapl_entry_20260201_1&token=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJjb2xsZWN0aW9uSWQiOiJwYmNfMzE0MjYzNTgyMyIsImV4cCI6MTg2ODk0MDM2NSwiaWQiOiJsbGRuMGc5cTN6Z2EwczgiLCJyZWZyZXNoYWJsZSI6dHJ1ZSwidHlwZSI6ImF1dGgifQ.3gy1d1Er-GlXkNyRjXNceMIWB77bB20wgbKmT60Z3b4"
```

---

## 5. 逆向信号 API

### 5.1 计算逆向信号 (需认证)
```bash
curl -X POST "https://pb.lzw-glory.top/api/custom/reverse/calculate" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJjb2xsZWN0aW9uSWQiOiJwYmNfMzE0MjYzNTgyMyIsImV4cCI6MTg2ODk0MDM2NSwiaWQiOiJsbGRuMGc5cTN6Z2EwczgiLCJyZWZyZXNoYWJsZSI6dHJ1ZSwidHlwZSI6ImF1dGgifQ.3gy1d1Er-GlXkNyRjXNceMIWB77bB20wgbKmT60Z3b4" \
    -d '{
      "symbol": "AAPL",
      "direction": "long"
    }'
```

### 5.2 获取待处理逆向信号 (需认证)
```bash
curl -X GET "https://pb.lzw-glory.top/api/custom/reverse/pending" \
    -H "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJjb2xsZWN0aW9uSWQiOiJwYmNfMzE0MjYzNTgyMyIsImV4cCI6MTg2ODk0MDM2NSwiaWQiOiJsbGRuMGc5cTN6Z2EwczgiLCJyZWZyZXNoYWJsZSI6dHJ1ZSwidHlwZSI6ImF1dGgifQ.3gy1d1Er-GlXkNyRjXNceMIWB77bB20wgbKmT60Z3b4"
```

### 5.3 确认逆向信号处理 (需认证)
```bash
curl -X POST "https://pb.lzw-glory.top/api/custom/reverse/ack" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJjb2xsZWN0aW9uSWQiOiJwYmNfMzE0MjYzNTgyMyIsImV4cCI6MTg2ODk0MDM2NSwiaWQiOiJsbGRuMGc5cTN6Z2EwczgiLCJyZWZyZXNoYWJsZSI6dHJ1ZSwidHlwZSI6ImF1dGgifQ.3gy1d1Er-GlXkNyRjXNceMIWB77bB20wgbKmT60Z3b4" \
    -d '{
      "signal_id": "reverse_signal_id_123",
      "status": "confirmed",
      "reason": "Processed by QC system",
      "order_id": "order_aapl_entry_20260201_1",
      "signal_id_orig": "signal_aapl_long_20260201"
    }'
```

---

## 6. 配置参考

### 6.1 配置信号有效期 (分钟)
```bash
# 设置信号有效期为 30 分钟
curl -X POST "https://pb.lzw-glory.top/api/collections/config/records" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer YOUR_ADMIN_TOKEN" \
    -d '{
      "key": "signal_validity_minutes",
      "value": "30"
    }'
```

### 6.2 配置信号自动确认
```bash
# true = 自动确认, false = 需手动确认
curl -X POST "https://pb.lzw-glory.top/api/collections/config/records" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer YOUR_ADMIN_TOKEN" \
    -d '{
      "key": "signal_auto_confirm",
      "value": "false"
    }'
```

### 6.3 配置信号操作 Token
```bash
# 用于飞书按钮权限验证
curl -X POST "https://pb.lzw-glory.top/api/collections/config/records" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer YOUR_ADMIN_TOKEN" \
    -d '{
      "key": "signal_action_token",
      "value": "your_secure_token_here"
    }'
```

### 6.4 配置逆向信号阈值
```bash
curl -X POST "https://pb.lzw-glory.top/api/collections/config/records" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer YOUR_ADMIN_TOKEN" \
    -d '{
      "key": "reverse_signal_threshold",
      "value": "6"
    }'
```

---

## 使用说明

1. **替换域名**: 将 `https://pb.lzw-glory.top` 替换为您的实际域名
2. **认证 Token**:
   - 已配置 JWT Token: `eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJjb2xsZWN0aW9uSWQiOiJwYmNfMzE0MjYzNTgyMyIsImV4cCI6MTg2ODk0MDM2NSwiaWQiOiJsbGRuMGc5cTN6Z2EwczgiLCJyZWZyZXNoYWJsZSI6dHJ1ZSwidHlwZSI6ImF1dGgifQ.3gy1d1Er-GlXkNyRjXNceMIWB77bB20wgbKmT60Z3b4`
   - 配置 API 需要使用管理员 Token
3. **信号 ID**: 使用固定的测试 ID（2026-02-01 格式）
4. **路由前缀**: 自定义路由必须使用 `/api/webhook/tv` 格式

## 注意事项

- 所有 `*/api/custom/*` 端点都需要超级用户认证
- `/api/webhook/tv` 无需认证
- 飞书按钮端点 (`/webhook/signal/*`, `/webhook/order/*`) 需要配置 token 验证
- 信号有效期检查为定时任务（每分钟执行），超时信号自动标记为 `expired`
- 逆向信号检测在信号接收时自动触发（检查是否有冲突持仓）

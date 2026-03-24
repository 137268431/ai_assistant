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
      "entry": 185.00,
      "stop_loss": 183.00,
      "take_profit": 190.00,
      "signal_id": "signal_aapl_long_20260201",
      "rr": "2.5",
      "shares": 100,
      "us_time": "2026-02-01 10:00:00",
      "reason": "Test signal"
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
      "entry": 250.00,
      "stop_loss": 255.00,
      "take_profit": 240.00,
      "signal_id": "signal_tsla_short_20260201",
      "rr": "2.0",
      "shares": 50,
      "us_time": "2026-02-01 14:30:00",
      "reason": "Bearish divergence"
    }'
```

### 1.3 发送技术指标数据 (Indicator)
```bash
curl -X POST "https://pb.lzw-glory.top/webhook/tv" \
    -H "Content-Type: application/json" \
    -d '{
      "type": "indicator",
      "symbol": "SPY",
      "exchange": "NYSE",
      "interval": "1D",
      "scriptTag": "main",
      "usTime": "2026-03-19 10:00:00",
      "cnTime": "2026-03-19 23:00:00",
      "barTimeMs": 1710842400000,
      "barIndex": 100,
      "close": 520.50,
      "high": 522.00,
      "low": 518.00,
      "open": 519.00,
      "volume": 100000000,
      "dayChangePct": 1.2,
      "prevCloseChangePct": 1.0,
      "change7d": 2.5,
      "vwap": 520.00,
      "atr": 3.5,
      "atrRaw": 3.5,
      "atrPct": 0.67
    }'
```

### 1.4 发送技术指标数据 (AAPL 示例)
```bash
curl -X POST "https://pb.lzw-glory.top/webhook/tv" \
    -H "Content-Type: application/json" \
    -d '{
      "type": "indicator",
      "symbol": "AAPL",
      "exchange": "NASDAQ",
      "interval": "15",
      "scriptTag": "main",
      "usTime": "2026-03-19 10:00:00",
      "cnTime": "2026-03-19 23:00:00",
      "barTimeMs": 1710842400000,
      "barIndex": 100,
      "close": 185.50,
      "high": 186.00,
      "low": 184.80,
      "open": 185.20,
      "volume": 1000000,
      "dayChangePct": 1.5,
      "prevCloseChangePct": 1.2,
      "change7d": 5.3,
      "vwap": 185.00,
      "vwapUpper1": 186.50,
      "vwapLower1": 183.50,
      "vwapDist": 0.5,
      "vwapBullish": true,
      "emaFast": 184.80,
      "emaSlow": 183.50,
      "emaTrend": "bullish",
      "emaBullish": true,
      "emaBearish": false,
      "slopeTrend": "up",
      "sdReg": 185.00,
      "sdStdDev": 1.5,
      "sdZone": "middle",
      "sdTrend": "neutral",
      "sdUpper": false,
      "sdLower": false,
      "dtpAvg": 185.20,
      "dtpAtr": 2.5,
      "dtpDir": "up",
      "dtpPhase": "accumulation",
      "dtpPhaseBars": 5,
      "atr": 2.5,
      "atrRaw": 2.5,
      "atrPct": 1.35,
      "crsi": 65.0,
      "crsiUb": 70.0,
      "crsiDb": 30.0,
      "crsiOB": false,
      "crsiOS": false,
      "obvRsi": 58.0,
      "crsiBullDiv": false,
      "crsiBearDiv": false,
      "crsiHidBull": false,
      "crsiHidBear": false,
      "obvBullDiv": false,
      "obvBearDiv": false,
      "obvHidBull": false,
      "obvHidBear": false,
      "fractalBull": false,
      "fractalBear": false,
      "emaBullTouch": false,
      "emaBearTouch": false
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

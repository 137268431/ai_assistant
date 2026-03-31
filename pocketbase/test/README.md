# PocketBase 测试与订单关系总览

## 核心目标

当前链路的重点不再是“单条订单状态同步”，而是“一个 trade group 内主单、止盈单、止损单的完整关系与事件时间线”。

这套模型同时覆盖：

- `quant_trading` 内部订单生命周期
- PocketBase 的 `orders` / `order_details`
- `pb_public/orders.html` 与 `pb_public/order_details.html`
- `ai_assistant/pocketbase/test/pb-flow.sh`

---

## 统一关系字段

以下字段是正式字段，优先级高于 `extra.*` 中的兼容字段：

| 字段 | 说明 |
|------|------|
| `unique_id` | QC 侧唯一订单 ID，PocketBase 以它做单条订单主键 |
| `broker_order_id` | 券商 / broker 原始订单 ID |
| `trade_group_id` | 同一笔交易组 ID，当前实现中等于主入场单 `unique_id` |
| `entry_order_unique_id` | 主入场单 `unique_id` |
| `parent_order_unique_id` | 子单的父单，通常是主入场单 |
| `sibling_order_unique_id` | 同级对手单，例如 TP 的 sibling 是 SL |
| `role` | `entry` / `take_profit` / `stop_loss` / `repair_tp` / `repair_sl` |
| `relation_status` | `planned` / `active` / `closed` / `orphaned` |
| `position_side` | `long` / `short` |

---

## 真实执行链路

### 1. 信号进入 PB

TradingView 调用 `POST /webhook/tv`：

- 写入 `signals`
- 如需人工确认，则飞书卡片先停在 `awaiting_confirm`
- 如已存在反向持仓或反向挂单，可额外生成 `reverse_signals`

### 2. QC 认领信号并创建 Init 交易组

QC 调用 `POST /api/custom/signals/ack`：

- 将 `signals.status` 更新为 `executed`
- 创建 `orders` 交易组骨架：
  - Entry = `Init + active`
  - TP = `Init + planned`
  - SL = `Init + planned`
- 三条记录都各自写入第一条 `order_details`

主单与子单在这一刻就必须携带：

- `trade_group_id`
- `entry_order_unique_id`
- `parent_order_unique_id`
- `sibling_order_unique_id`
- `role`
- `relation_status`
- `position_side`
- `broker_order_id`

### 3. QC 持续同步主单状态

QC 后续通过 `POST /api/custom/orders/upsert` 更新同一条主单：

- `Submitted`
- `Filled`
- `Canceled`
- `Closed`

每次 upsert 都会追加一条 `order_details` 事件。

### 4. 主单成交后激活 TP / SL 子单

一旦 Entry 成交：

- QC 在本地创建真实 broker bracket orders
- PocketBase 不再首次创建子单，而是更新已存在的两条子单：
  - TP: `Init + planned` -> `Submitted + active`
  - SL: `Init + planned` -> `Submitted + active`
- 同时回写真实 `broker_order_id`

### 5. 子单成交时处理对手单

实际逻辑不是“只更新成交那一条单”，而是：

- TP 成交：TP = `Filled`，SL = `Canceled`
- SL 成交：SL = `Filled`，TP = `Canceled`

这两个事件都要写入 `orders` 和 `order_details`，页面才能正确展示父子关系和收尾状态。

### 6. 页面动作语义

`/webhook/order/cancel`

- 只允许取消主入场单
- 子单不能直接取消

`/webhook/order/close`

- 不是只改一条记录
- 会关闭整个 `trade_group_id`
- 主单变为 `Closed`
- 相关子单变为 `Canceled`

### 7. 逆向信号

QC 处理 `reverse_signals` 后回写 `POST /api/custom/reverse/ack`：

- `order_id`
- `signal_id_orig`
- `trade_group_id`
- `entry_order_unique_id`

这样页面和测试数据能定位到对应交易组，而不是只看到一条孤立 reverse 记录。

---

## 页面对应关系

### `pb_public/orders.html`

- 以 `trade_group_id` 聚合展示
- 一个卡片代表一个交易组
- 卡片内完整展示主单、TP、SL、父子关系、sibling 关系、broker id、relation status

### `pb_public/order_details.html`

- 列表模式下展示事件卡片
- 支持 `trade_group_id` / `order_id` / `signal_id` / `id` 查询
- trade group 查询模式下展示：
  - 订单关系树
  - 全量事件时间线

---

## `pb-flow.sh` 当前测试语义

| 步骤 | 语义 |
|------|------|
| `1` | 发送测试信号 |
| `5` | QC 确认信号，创建带正式关系字段的 Entry / TP / SL = Init |
| `6` | Entry 更新为 `Submitted` |
| `7` | Entry 更新为 `Filled`，同时 TP / SL 更新为 `Submitted` |
| `8` | TP 更新为 `Filled`，同时 SL 更新为 `Canceled` |
| `9` | SL 更新为 `Filled`，同时 TP 更新为 `Canceled` |
| `O` | 页面取消主入场单 |
| `P` | 页面关闭整个交易组 |
| `C` | reverse ack，回写交易组定位字段 |

---

## 相关文档

- [signals.md](./signals.md)
- [orders.md](./orders.md)
- [reverse_signals.md](./reverse_signals.md)

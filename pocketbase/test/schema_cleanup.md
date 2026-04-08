# PocketBase Schema Cleanup Notes

## 当前分层

### 1. 业务主表

- `orders`
  - 当前角色：应用层订单主表
  - 状态：保留
  - 原因：页面、hooks、对账、订单回写都在直接使用

- `order_details`
  - 当前角色：应用层订单事件明细
  - 状态：暂不删除，后续可重命名
  - 建议目标名：`ibkr_order_details`
  - 原因：名字有歧义，但仍是活跃主表

- `reverse_signals`
  - 当前角色：反向动作工作流主表
  - 状态：暂不删除，后续可重命名
  - 建议目标名：`ibkr_reverse_signals`
  - 原因：当前 hooks、页面、compute 都直接读写它，它不是遗留垃圾表

### 2. 已退役表

- `ibkr_orders`
  - 历史角色：券商原始订单镜像 / 兼容层
  - 当前状态：已从主流程移除，且已于 2026-04-08 从远端 live 库删除
  - 2026-04-08 起：不再参与下单、跟踪、过期判定、反向撤单、回补逻辑

### 3. 隔离回测表

- `ibkr_backtest_reverse_signals`
  - 当前角色：回测专用反转记录
  - 状态：保留隔离，不参与 live 清理

## 清理原则

1. 先统一“谁是主表”，再做 rename / drop。
2. 任何 live collection 删除前，必须先确认代码引用为 0。
3. 不直接在生产上做“重命名即删除”，而是按迁移三段式执行：
   - 第一步：统一 reader / writer 的目标表
   - 第二步：确认运行时引用归零
   - 第三步：做 schema rename 或下线旧表

## 审计机制

新增脚本：

```bash
python3 pocketbase/scripts/collection_usage_audit.py
```

只检查当前代码里的引用，不改库。

单表检查示例：

```bash
python3 pocketbase/scripts/collection_usage_audit.py reverse_signals
python3 pocketbase/scripts/collection_usage_audit.py order_details
```

## 当前结论

- `reverse_signals` 和 `order_details` 一样，确实存在命名不够清晰的问题。
- 但它现在属于业务活跃主表，不是可以直接删除的无用表。
- 如果要清理，正确顺序是：
  - 先把引用收敛
  - 再做别名 / 重命名迁移
  - 最后才考虑删旧表

- `ibkr_orders` 已完成“引用收敛 -> 退出运行时 -> live 删表”。

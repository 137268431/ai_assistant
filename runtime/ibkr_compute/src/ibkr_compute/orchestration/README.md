# Orchestration 分层说明

`trading_service.py` 现在只保留三类内容：

- 运行时常量与环境默认值
- 依赖对象装配与线程状态初始化
- `main()` 入口与兼容壳

其余行为按职责拆到独立 mixin，保持“主壳稳定、子域清晰”的结构。

## 阅读顺序

建议按下面顺序阅读：

1. `trading_service.py`
2. `service_support.py`
3. `auth_recovery.py`
4. `lifecycle.py`
5. `startup.py`
6. `warmup.py`
7. `warmup_cycle.py`
8. `integrity.py`
9. `runtime_pipeline.py`
10. `runtime_ops.py`
11. `runtime_status.py`
12. `signals.py`
13. `market_universe.py`

## 文件职责

- `trading_service.py`
  兼容入口壳。集中做对象装配、共享状态字段初始化、线程句柄注册。
- `service_support.py`
  主壳所依赖的轻量支撑能力，包括运行时设置刷新、行情 tick 接线、市场日期与 quote 前收查询、基础状态属性。
- `auth_recovery.py`
  2FA / 会话恢复、manual takeover、silent probe、panic reset 与自动重启编排。
- `lifecycle.py`
  启动策略、启动卡片编排、运行门控与成功/降级状态收口。
- `startup.py`
  启动流程编排、启动进度、人工触发启动与恢复。
- `warmup.py`
  预热状态、scope 快照、session 过渡与调度入口。
- `warmup_cycle.py`
  warmup readiness 判定、preflight repair、回补与交易门开放主循环。
- `integrity.py`
  K 线完整性、历史修复、日切与巡检。
- `runtime_pipeline.py`
  实时计算流水线、官方 5m close、后台 interval prime。
- `runtime_ops.py`
  运行期操作收口，包括 fill/cancel 回调、保留清理线程、停止流程。
- `runtime_status.py`
  对外状态快照、启动/预热/鉴权状态整理。
- `signals.py`
  信号轮询、信号消费、交易窗口与 readiness 组合。
- `market_universe.py`
  watchlist、订阅集合、交易标的与市场监控标的管理。

## 当前边界原则

- 不改 Flask / systemd / 启动入口 / URL
- 不引入 bundler 或新框架
- 保持 `IBKRTradingService` 作为单一兼容门面
- 子文件按“领域职责”拆，不按“单个函数数量”机械拆分

## 后续建议

如果后面继续重构，优先顺序建议是：

1. 拆 `api/app.py`，把路由注册和页面视图注册分层。
2. 拆 `backtest/runtime_service.py`，做和 `orchestration` 类似的主壳 + 子域 mixin。
3. 针对前端页面，把页面级 CSS 从模板或大 JS 文件中继续抽到独立静态文件，遵循“页面壳 / 数据脚本 / 样式”三段式结构。

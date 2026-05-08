# API 目录地图

## Split Control Plane 边界

- `ibkr_compute/api` 现在只负责 compute、scan、recompute、history rebuild、data-quality 和共享兼容能力。
- backtest 代码仍复用 `runtime/ibkr_compute/src/ibkr_compute/backtest` 及共享 compute 源码，但运行边界是独立的 `ibkr-backtest` 服务；backtest-only 部署应走 `ops/deploy/deploy_ibkr_backtest_runtime.sh`，只重启 `ibkr-backtest`，不重启正常 `ibkr-compute` / `ibkr-runtime`。
- `ibkr-api` 兼容代理中的 `/api/custom/ibkr/backtest/*` 应转发到 `IBKR_BACKTEST_INTERNAL_URL`（默认 `http://127.0.0.1:5105`）；非 backtest 的 compute / scan / recompute / chart / history rebuild / data-quality 路由继续走 `IBKR_COMPUTE_INTERNAL_URL`。
- 控制面路由、webhook 落地页、signal/order/reverse 动作、system summary / monitor / scheduler 可见性、startup progress、Feishu-facing callback 现在归 `runtime/ibkr_api/src/ibkr_api`。
- 新的控制面需求优先落到 `ibkr_api/system`、`ibkr_api/startup`、`ibkr_api/integrations`、`ibkr_api/callbacks`、`ibkr_api/tradingview`，以及 root `signal_*` / `order_*` / `reverse_*` 模块，而不是回堆到 `ibkr_compute/api`。

`api` 现在按“入口层 / 基础设施层 / 领域实现层 / 兼容层”四层组织，目标是让阅读路径先看到高可见入口，再逐步进入路由注册、公共能力和具体业务实现，而不是在根目录堆满几十个平级文件。

## 根目录只保留 3 个入口

- `__init__.py`
  安装兼容 alias finder，保证旧导入路径继续可用。
- `app.py`
  组合根，负责 Flask app、运行时状态和各领域路由注册。
- `server.py`
  保留既有启动入口，不改部署和调用方式。

## 子目录分层

- `app_core/`
  组合根基础设施，收口 bootstrap、entrypoints、exports、logging、route registration。
- `shared/`
  路由公共层，放 request / response / runtime / common 这类跨领域 helper。
- `support/`
  基础业务 helper，放 market time、symbols、app support 聚合等辅助逻辑。
- `routes/`
  URL 绑定层，只做 transport shell 和 route registration，不承载具体业务细节。
- `account/`
  账户、持仓、历史、下单动作及账户域聚合导出。
- `account/live/`
  账户实时数据子目录，拆分 summary、orders、positions、time、runtime helper。
- `account/snapshot_builder/`
  账户快照构建子目录，拆分上下文与缓存、并发抓取、live order recovery、payload 汇总。
- `account/history_builder/`
  账户历史订单构建子目录，拆分 broker/PB normalize、对账和最终 payload 汇总。
- `account/action_builders/`
  账户动作构建子目录，拆分公共快照响应、订单动作和持仓平仓动作。
- `chart/`
  图表请求解析、route view、timeline / compare 视图拼装。
- `chart/compare/`
  compare 子目录，拆分请求周期估算、IBKR source 拉取、diff 规则和 payload 组装。
- `chart/timeline/`
  timeline 子目录，拆分 runtime、source rows、indicator/signal row 组装和 payload 构建。
- `market/`
  行情、contract search、forming bar、screener 与相关 support。
- `market/screener/`
  screener 子目录，继续按 coercion / watchlist / scoring / payload 拆开。
- `runtime/`
  gateway、control、auth、restore 以及 runtime 域聚合导出。
- `monitor/`
  host、flags、samples、snapshot 与监控聚合导出。
- `monitor/runtime/`
  monitor runtime 子目录，拆分 compute summary、未初始化状态、gateway action 和 snapshot 聚合。
- `compute/`
  compute 请求、pipeline、payload、cursor、materialize、rollup、runtime ops。
- `compute/runtime_state/`
  compute 公共运行时子目录，拆分 universe、engine、cache、timing 和 app runtime helper。
- `ops/`
  状态、运维动作和 ops 域公共 helper。
- `legacy/`
  历史 proxy 和 legacy 聚合导出。
- `compat/`
  兼容层，专门承接旧模块名到新目录结构的映射。

## compat 再细分为两层

- `compat/aliases/`
  只放 alias 注册表，并按领域继续拆成 `account.py`、`compute.py`、`runtime.py` 等小文件。
- `compat/import_hook.py`
  只放 finder / loader / register 机制。
- `compat/module_aliases.py`
  保留旧入口名，但只做聚合导出，不再堆放所有真实实现。

## 推荐阅读顺序

1. 先看 `app.py`，理解组合根和注册流程。
2. 再看 `app_core/`、`routes/`、`shared/`、`support/`，理解骨架层。
3. 再进入具体领域目录，比如 `account/`、`market/`、`compute/`。
4. 最后看 `compat/`，只在需要追踪旧导入名时进入。

## 兼容策略

- 不改 URL。
- 不改 Flask 启动方式。
- 不改 `server.py` 入口。
- 历史 `app_*`、`route_*`、`*_views.py`、`*_routes.py`、`compute_*` 等旧导入名，统一通过 `compat` 下的 alias 机制映射到新位置。
- 根目录尽量不再增加兼容壳文件，优先把真实实现留在子目录中。
- `account_snapshot.py`
  账户快照兼容壳，真实实现位于 `account/snapshot.py`。
- `account_actions.py`
  账户动作兼容壳，真实实现位于 `account/actions.py`。
- `market_routes.py`
  quotes、forming bar、官方 close ingest、contract search、screener 路由壳兼容入口。
- `market_data_views.py`
  市场数据查询、forming bar、官方 close ingest、contract search 响应拼装兼容壳。
- `market_screener_views.py`
  screener 参数校验与本地 payload 组装兼容壳；不再反向依赖 PocketBase `/api/custom/ibkr/screener`。
- `chart_routes.py`
  chart timeline / compare 路由壳兼容入口。
- `chart_request.py`
  chart 请求参数解析与范围校验兼容壳。
- `chart_route_views.py`
  chart timeline / compare 路由响应拼装兼容壳。
- `runtime_routes.py`
  runtime start/stop、gateway、2FA、monitor、dashboard 路由壳兼容入口。
- `route_request.py`
  request body / query 参数解析兼容壳，承接通用取参与基础类型归一化，包括 CSV、分页等常见模式。
- `route_runtime.py`
  app/service/runtime environment 访问与运行时上下文拼装兼容壳。
- `route_response.py`
  route 层统一 JSON 响应与 pair-response 包装兼容壳。
- `route_common.py`
  route 公共兼容导出壳，真实实现位于 `shared/route_common.py`。
- `ops_routes.py`
  health/status、history rebuild、retention、backtest、data-quality 路由壳兼容入口。
- `ops_common.py`
  ops common 兼容壳，真实实现位于 `ops/common.py`。
- `ops_status_views.py`
  ops status 兼容壳，真实实现位于 `ops/status_views.py`。
- `ops_action_views.py`
  ops action 兼容壳，真实实现位于 `ops/action_views.py`。
- `legacy_proxy_routes.py`
  兼容 PocketBase collections proxy 路由壳兼容入口。
- `legacy_proxy_views.py`
  legacy proxy 兼容壳，真实实现位于 `legacy/views.py`。
- `compute_routes.py`
  `/compute`、`/scan`、`/recompute` 路由壳兼容入口。
- `compute_request.py`
  compute request 兼容壳，真实实现位于 `compute/request.py`。
- `compute_pipeline_views.py`
  compute pipeline 兼容壳，真实实现位于 `compute/pipeline_views.py`。
- `compute_runtime_ops.py`
  compute runtime ops 兼容壳，真实实现位于 `compute/runtime_ops.py`。
- `compute_support.py`
  compute 兼容导出壳。继续暴露既有 helper 名称，真实实现位于 `compute/support.py`。
- `compute_common.py`
  compute common 兼容壳，真实实现位于 `compute/common.py`。
- `compute_cursors.py`
  compute cursor 兼容壳，真实实现位于 `compute/cursors.py`。
- `compute_materialize.py`
  compute materialize 兼容壳，真实实现位于 `compute/materialize.py`。
- `compute_rollup.py`
  compute rollup 兼容壳，真实实现位于 `compute/rollup.py`。
- `compute_payloads.py`
  compute payload 兼容壳，真实实现位于 `compute/payloads.py`。
- `compute_flush.py`
  compute flush 兼容壳，真实实现位于 `compute/flush.py`。
- `runtime_views.py`
  runtime 兼容导出壳。继续暴露旧 runtime helper 入口，真实实现位于 `runtime/views.py`。
- `runtime_common.py`
  runtime 域公共 helper 兼容壳，真实实现位于 `runtime/common.py`。
- `runtime_restore.py`
  runtime restore 兼容壳，真实实现位于 `runtime/restore.py`。
- `runtime_control_views.py`
  runtime control 兼容壳，真实实现位于 `runtime/control_views.py`。
- `runtime_gateway_views.py`
  runtime gateway 兼容壳，真实实现位于 `runtime/gateway_views.py`。
- `runtime_auth_views.py`
  runtime auth 兼容壳，真实实现位于 `runtime/auth_views.py`。
- `chart_views.py`
  图表兼容导出壳。继续暴露 timeline / compare helper，真实实现位于 `chart/views.py`。
- `chart_timeline_views.py`
  图表时间线、指标与信号展示拼装兼容壳。
- `chart_compare_views.py`
  图表 compare 数据、分组与摘要拼装兼容壳。
- `monitor_views.py`
  monitor 兼容导出壳。继续暴露监控 helper 入口，真实实现位于 `monitor/views.py`。
- `monitor_host.py`
  monitor host 兼容壳，真实实现位于 `monitor/host.py`。
- `monitor_runtime.py`
  monitor runtime 兼容导出壳。继续暴露监控 runtime helper 入口，内部转发到 `monitor/views.py`。
- `monitor_runtime_samples.py`
  monitor samples 兼容壳，真实实现位于 `monitor/samples.py`。
- `monitor_runtime_flags.py`
  monitor flags 兼容壳，真实实现位于 `monitor/flags.py`。
- `monitor_runtime_snapshot.py`
  monitor snapshot 兼容壳，真实实现位于 `monitor/snapshot.py`。
- `screener_support.py`
  screener support 兼容壳，真实实现位于 `market/support.py`。

## 建议阅读顺序

1. `server.py`
2. `app.py`
3. `api/__init__.py`
4. `compat/module_aliases.py`
5. `app_core/` 目录下的 `logging.py` / `bootstrap.py` / `entrypoints.py` / `exports.py` / `exports_support.py` / `exports_compute.py` / `exports_runtime.py` / `routes.py`
6. `routes/` 目录下的 `account.py` / `chart.py` / `compute.py` / `legacy_proxy.py` / `market.py` / `ops.py` / `runtime.py`
7. `shared/route_request.py` / `shared/route_runtime.py` / `shared/route_response.py` / `shared/route_common.py`
8. `support/market_time.py` / `support/symbols.py` / `support/app_support.py`
9. `chart/` 目录下的 `request.py` / `route_views.py` / `timeline_views.py` / `compare_views.py` / `views.py`
10. `market/` 目录下的 `data_views.py` / `screener_views.py` / `support.py`
11. `legacy/` 目录下的 `proxy_views.py` / `views.py`
12. `compute/` 目录下的 `pipeline_views.py` / `runtime_ops.py` / `support.py`
13. `ops/` 目录下的 `common.py` / `status_views.py` / `action_views.py` / `views.py`
14. `account/` 目录下的 `views.py` / `common.py` / `snapshot.py` / `history.py` / `actions.py`
15. `monitor/` 目录下的 `views.py` / `host.py` / `samples.py` / `flags.py` / `snapshot.py`
16. `runtime/` 目录下的 `views.py` / `common.py` / `restore.py` / `control_views.py` / `gateway_views.py` / `auth_views.py`
17. `compute/` 目录下的 `common.py` / `cursors.py` / `materialize.py` / `rollup.py` / `payloads.py` / `flush.py`
18. 最后再回头看 `compat/module_aliases.py` 里的历史兼容 alias 映射表

## 当前边界原则

- 不改 URL、不改 Flask、不改启动入口。
- `app.py` 只保留必须共享的全局状态、兼容导出入口和路由注册。
- 启动期的 logging / bootstrap / route registration 尽量外提到独立模块，避免组合根再次变胖。
- 兼容导出很多时，优先集中到 `app_exports.py`，不要把组合根重新拖回大文件。
- 路由函数尽量只保留“取参 + 调 helper / app 状态 + 返回响应”。
- 兼容壳文件优先保持薄，避免再次长回去。
- `app.py` 中与 Flask 无强耦合的基础 helper，优先下沉到 `app_market_time.py`、`app_symbols.py` 等小模块；`app_support.py` 只保留兼容导出职责。
- 具备稳定边界的公共层，优先进入 `app_core/`、`shared/`、`support/` 等子目录；根目录只保留兼容壳和高可见入口。
- 领域实现层在边界稳定后，优先进入 `chart/`、`market/` 等领域目录；根目录前缀文件保留兼容壳。
- route helper 的取参、query 解析优先集中到 `route_request.py`，运行时 service/app helper 优先集中到 `route_runtime.py`。
- route 层的 `jsonify + status_code` 响应包装优先集中到 `route_response.py`，避免每个路由壳重复展开。
- `route_common.py` 只保留兼容导出职责，避免重新长回“万能公共文件”。
- compute 主链优先拆成 request 解析、pipeline 执行、runtime ops 三层，避免把 batch flush / rollup / recompute 全堆在入口文件。
- 展示拼装优先下沉到 `*_views.py`。
- runtime / monitor / account / chart 的领域细节继续下沉到对应 `*_*.py` 子模块。
- 通用计算和筛选支撑优先下沉到 `*_support.py`。

## 后续优先级

下一步更适合继续拆下面两块：

1. `route_request.py` / `route_runtime.py`
   还可以继续看日期窗口、通用 query schema、legacy proxy 透传参数包装是否再抽一层。
2. `compat/module_aliases.py` 的 alias 分组
   如果后续还要继续减复杂度，重点就是按领域继续压缩和分组兼容映射，而不是重新加回根目录壳文件。

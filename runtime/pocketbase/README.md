# pocketbase compatibility target

- `PocketBase` 的最终定位仍然是：`storage + auth + admin`。
- `runtime/pocketbase/pb_public` 的最终目标是兼容镜像，不再做正式前端源码或正式页面承载。
- `runtime/pocketbase/pb_hooks` 的最终目标是兼容壳，不再做业务 API、业务调度、运行面控制或系统通知主实现。

## pb_public final state

- 当前应保留的能力：
  - 作为 `runtime/ibkr_console/static` 的兼容镜像，给旧域名、旧反向代理、旧书签和旧外链兜底。
  - 保持既有页面路径兼容，例如 `ibkr_runtime.html`、`ibkr_signals.html`、`chart.html`。
  - 只做镜像，不做源码主目录；页面改动应先落在 `runtime/ibkr_console/static`。
- 最终应移除的能力：
  - 不再作为正式控制台页面入口。
  - 不再承载独立页面逻辑、独立资源、独立环境配置。
  - 不再接受“只改 `pb_public` 不改 `ibkr_console`”的工作流。
- 最终建议状态：
  - 最理想是整体删除。
  - 如果线上仍有历史入口依赖，最多只保留极薄的 redirect 或镜像层。
- 删除 `pb_public` 的条件：
  - 公网入口、反向代理和运维检查都已经切到 `ibkr_console`。
  - Feishu 卡片、脚本、书签、文档里的页面链接都不再依赖 PocketBase 页面路径。
  - 部署脚本和 smoke check 不再同步或检查 `/opt/pocketbase/pb_public`。

## pb_hooks final state

- 过渡期允许保留的能力：
  - 兼容注册壳：继续注册旧 `/api/custom/*`、`/webhook/*`、`cronAdd` 入口。
  - 薄代理：把旧入口转发到 `ibkr_api` 或 `ibkr_scheduler`。
  - 极少量确实必须贴近 PocketBase 宿主的最小 hook；如果未来没有这类刚需，连这一层也不需要保留。
- 最终不应该继续保留的能力：
  - 页面托管和页面业务逻辑。
  - 业务 API 主实现。
  - 运行面控制动作主实现。
  - 2FA 编排与 Feishu 交互主实现。
  - 系统监控、心跳、状态摘要、日报、巡检告警主实现。
  - 业务 cron 编排和调度状态推进主实现。
- 最终建议状态：
  - `pb_hooks` 只剩兼容空壳，或者完全移除。

## current state snapshot

- `pb_public`
  - 当前定位已经是镜像层，源码主目录是 `runtime/ibkr_console/static`。
  - 只要部署和入口仍然把 PocketBase public 当兼容页源，它就还不能删除。
- `pb_hooks`
  - 已经有一批路径变成薄代理，例如：
    - `modules/actions/ibkr_signal_actions.js`
    - `modules/actions/order_manage.js`
    - `modules/actions/ibkr_reverse_signals.js`
    - `modules/actions/ibkr_actions.js` 里的 `2fa/request|result|respond`
    - `modules/actions/ibkr_actions.js` 里的 `2fa/takeover|probe|panic-reset`
    - `modules/actions/ibkr_actions.js` 里的 `state/signals|state/orders|health-report|notify`
    - `modules/integrations/webhook_tv.js`
    - `modules/integrations/feishu.js`
    - `modules/schedulers/order_scheduler.js`
    - `modules/schedulers/ibkr_signal_scheduler.js`
    - `modules/schedulers/ibkr_system_monitor.js` 里的大部分 `cronAdd` 调度转发
  - 但当前代码状态下，`pb_hooks` 还远不只是“薄兼容壳”，仍有大量业务实现留在 PocketBase JS 侧。

## current blockers before pb_hooks deletion

- `runtime/pocketbase/pb_hooks/modules/actions/ibkr_actions.js`
  - 已迁出到 `ibkr-api` 的核心落库写接口：`ping_write`、`bars`、`indicator`、`indicators`、`scan`、`data_quality/upsert`、`data_quality/truth_upsert`
  - 已迁出到 `ibkr-api` 的 universe / read-side 接口：`watchlist/upsert|remove`、`targets/upsert|remove`、`screener/targets`、`data_quality/summary|truth_summary|list|truth_list`、`orders/cancel_sync`
  - 已迁出到 `ibkr-api` 的聚合读接口：`today-targets`、`screener`、`account_snapshot`
  - 已改成 API 或 upstream 代理、但 PB 兼容壳还没完全删掉的路径：`contracts/search`、`quotes`、`quotes/forming_bar`、`ingest/close`、`history/rebuild/start|status`、`rules`、`start|stop`、`gateway/start|stop|restart`、`account`、`positions`、`orders/live|history`、`orders/cancel|cancel_all|modify|place`、`positions/close`
- `runtime/pocketbase/pb_hooks/lib/scheduler/system_notify_scheduler.js`
  - 仍在 PocketBase 侧执行 heartbeat、status summary、scan summary、daily report 的通知与状态推进逻辑。
- `runtime/pocketbase/pb_hooks/lib/system_monitor_alert_guard.js`
  - 仍在 PocketBase 侧执行系统监控告警守卫逻辑，并由 `ibkr_system_monitor.js` 的 cron 兼容壳直接调用。
- `runtime/pocketbase/pb_hooks/lib/feishu/feishu_2fa.js`
  - 仍残留旧 2FA helper；现在 PB 路由侧的 `request|result|respond|takeover|probe|panic-reset` 都已迁到 `runtime/ibkr_api/src/ibkr_api/two_factor/`，但 PB-side auth guard / scheduler 兼容逻辑还在复用这批 helper。
- `runtime/pocketbase/pb_hooks/lib/trading/*` 与若干 `lib/*`
  - 仍承载实际业务逻辑的核心剩余文件已经收缩到：PB-side 调度通知相关 helper；`ibkr_today_targets.js` 与 `account_snapshot.js` 现在只剩兼容参考实现，不再是主路由 source of truth。

## what should remain vs remove

- `pb_public` 应保留：
  - 兼容镜像职责。
  - 旧页面路径兼容，直到入口全部切完。
- `pb_public` 应移除：
  - 正式前端源码职责。
  - 独立页面逻辑和独立配置职责。

- `pb_hooks` 应保留：
  - 旧路径注册壳。
  - 指向 `ibkr_api` / `ibkr_scheduler` 的兼容代理。
  - 如果以后确实存在不可替代的 PB 原生 hook，再单独保留最小必要集合。
- `pb_hooks` 应移除：
  - 所有业务 API 主实现。
  - 所有业务 cron 主实现。
  - 所有 2FA、系统通知、系统巡检、运行面控制主实现。
  - 所有 signal/order/reverse/data-quality/screener/today-targets/history/account 聚合主实现。

## delete criteria

- 可以认为 `pb_public` 已完成使命：
  - 它不再需要从 `ibkr_console` 镜像同步。
  - `check_stack`、Playwright、部署脚本、反向代理都只认 `ibkr_console`。
  - 外部入口已经不再暴露 PocketBase 页面路径。
- 可以认为 `pb_hooks` 已完成使命：
  - 所有 `routerAdd` 业务入口都已经迁出 PocketBase，PB 侧只剩代理壳。
  - 所有 `cronAdd` 业务任务都已经迁到 `ibkr_scheduler`，PB 侧不再执行 piggyback 通知或告警守卫逻辑。
  - 2FA、系统通知、data quality、watchlist/targets CRUD、order cancel sync、运行面控制、`today-targets`、`screener`、`account_snapshot` 等 PB JS 主逻辑都已经迁到对应服务；剩余主要 blocker 收缩为 PB-side piggyback 调度通知。
- 域名边界已经收敛为：
  - `quant.lzw-glory.top`：交易系统页面与 API / webhook 公网入口，不再代理 PocketBase collections/auth
  - `pb.lzw-glory.top`：PocketBase auth / collections / admin，不再承接交易系统控制面兼容入口
  - `qc.lzw-glory.top`：不再作为活跃拓扑的一部分；系统间交互默认走内网 `http://127.0.0.1:51xx`
  - 外部流量也不再依赖 PocketBase 域名下的兼容业务入口。

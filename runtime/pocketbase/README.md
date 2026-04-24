# pocketbase compatibility target

- `PocketBase` 的最终定位仍然是：`storage + auth + admin`。
- `runtime/pocketbase/pb_public` 现在只保留 PocketBase 自己的 landing/admin 入口；仓库里保留的历史 console HTML 文件也已经降为跳转到 `quant` 的 redirect shim，不再镜像交易系统页面。
- `runtime/pocketbase/pb_hooks` 现在只保留 no-op 入口文件，停止注册业务 API、业务调度、运行面控制或系统通知。

## pb_public final state

- 当前应保留的能力：
  - 作为 PocketBase 自己的最小 public 入口，展示 auth/data/admin 的角色边界。
  - 保留 `/` 与 `/_/` 这类 PocketBase 自身入口。
  - 对历史 `ibkr_*.html` / `login.html` / 旧别名页面，仅保留跳转到 `quant.lzw-glory.top` 的兼容 shim。
- 最终应移除的能力：
  - 不再作为正式控制台页面入口。
  - 不再承载任何交易页面、交易页面资源或交易环境配置。
- 最终建议状态：
  - 保留极薄的 PocketBase landing/admin 层即可。
- 删除 `pb_public` 的条件：
  - PocketBase 根路径不再需要 landing 页面，只保留 `/_/` admin 和 `/api/*`。
  - 外部文档与运维流程完全不再引用 `pb.lzw-glory.top/` 根页面。

## pb_hooks final state

- 过渡期允许保留的能力：
  - 仅保留空入口文件，避免 PocketBase 启动时缺少 hooks 目录结构。
- 最终不应该继续保留的能力：
  - 页面托管和页面业务逻辑。
  - 业务 API 主实现。
  - 运行面控制动作主实现。
  - 2FA 编排与 Feishu 交互主实现。
  - 系统监控、心跳、状态摘要、日报、巡检告警主实现。
  - 业务 cron 编排和调度状态推进主实现。
- 最终建议状态：
  - `pb_hooks` 只剩空壳，验证稳定后整体删除。

## current state snapshot

- `pb_public`
  - 当前定位已经变成 PocketBase 自身 landing 层，不再同步 `ibkr_console`。
  - 历史 console 页面文件名仍保留在仓库里，但内容已降为 redirect shim，用于兼容旧书签、旧反向代理路径和直接访问 `/opt/pocketbase/pb_public/*.html` 的场景。
  - 原来的 `common.js` 与 `assets/**` 旧 console 资源树已经从 `pb_public` 清掉，避免继续给人“PB 还在托管前端资源”的错觉。
- `pb_hooks`
  - 顶层 `*.pb.js` 入口文件都已降为 no-op，不再向 PocketBase 注册自定义业务路由或 cron。
  - 历史 `lib/**` 与 `modules/**` 已经从仓库删除，避免继续暗示 PocketBase 还保留业务逻辑或 proxy 实现。

## current blockers before pb_hooks deletion

- `runtime/pocketbase/pb_hooks/*.pb.js`
  - 现在只剩顶层 no-op 兼容入口壳，目录里已经没有历史业务模块。
  - 真正删除整个 `pb_hooks` 目录前，只需要再确认部署单元、文档说明、以及 PocketBase 侧是否还想保留这些显式占位文件。

## current blockers before pb_public deletion

- `runtime/pocketbase/pb_public/*.html`（除 `index.html` 外）
  - 这些历史文件名现在只是 redirect shim，本身已经不承载交易系统资源。
  - 真正删空前，需要先确认没有旧书签、人工直连 PocketBase 端口的排障流程、或外部脚本还依赖这些 legacy 页面路径名。

## what should remain vs remove

- `pb_public` 应保留：
  - PocketBase 自己的 landing/admin 职责。
- `pb_public` 应移除：
  - 正式前端源码职责。
  - 独立页面逻辑和独立配置职责。

- `pb_hooks` 应保留：
  - 当前只需要空入口壳。
  - 如果以后确实存在不可替代的 PB 原生 hook，再单独恢复最小必要集合。
- `pb_hooks` 应移除：
  - 所有业务 API 主实现。
  - 所有业务 cron 主实现。
  - 所有 2FA、系统通知、系统巡检、运行面控制主实现。
  - 所有 signal/order/reverse/data-quality/screener/today-targets/history/account 聚合主实现与历史 proxy/helper 代码。

## delete criteria

- 可以认为 `pb_public` 已完成使命：
  - 它不再需要承载交易系统页面。
  - `check_stack`、Playwright、部署脚本、反向代理都把交易页面只指向 `quant.lzw-glory.top`。
- 可以认为 `pb_hooks` 已完成使命：
  - 所有 `routerAdd` / `cronAdd` 业务入口都已经停止注册。
  - 所有业务 API、cron、系统通知与监控守卫都已经由 `ibkr-api` / `ibkr-scheduler` 原生接管。
  - 仓库里的 `pb_hooks` 只剩 no-op 入口壳，不再保留可执行业务 JS 模块。
- 域名边界已经收敛为：
  - `quant.lzw-glory.top`：交易系统页面与 API / webhook 公网入口，不再代理 PocketBase collections/auth
  - `pb.lzw-glory.top`：PocketBase auth / collections / admin 与最小 landing 页面，不再承接交易系统控制面兼容入口
  - `qc.lzw-glory.top`：不再作为活跃拓扑的一部分；系统间交互默认走内网 `http://127.0.0.1:51xx`
  - 外部流量也不再依赖 PocketBase 域名下的兼容业务入口。

# pocketbase compatibility target

- `PocketBase` 的最终定位仍然是：`storage + auth + admin`。
- `runtime/pocketbase/pb_public` 现在只保留 PocketBase 自己的 landing/admin 入口；仓库里只剩 `index.html`，`pb.lzw-glory.top` 只暴露 root/admin/api，其他路径在公网直接 404。
- `runtime/pocketbase/pb_hooks` 现在只保留空目录占位，不再保留任何 JS 入口文件；业务 API、业务调度、运行面控制和系统通知都已经迁到 `ibkr-api` / `ibkr-scheduler` / `ibkr-runtime`。

## bootstrap flow

- 一键 bootstrap（基础软件 + PocketBase + Gateway + split-stack deploy + health）：
  - `bash ai_assistant/ops/bootstrap/bootstrap_split_stack_remote.sh`
  - 注意：这个命令不包含 PocketBase schema import，也不包含 `config` / `_superusers` 最小状态迁移；这两步仍需单独执行。
  - 如果要一起串上 PB schema/state，可改成：`bash ai_assistant/ops/bootstrap/bootstrap_split_stack_remote.sh --with-schema-state --pb-email '<pb-superuser-email>' --pb-password '<pb-superuser-password>'`
  - `--with-schema-state` 现在会先完成 PB schema/state，再跑 split-stack deploy，避免 fresh host 在 runtime/api/scheduler 启动时因为缺少 collections 而直接 health fail。
- 新机 env 种子（把旧机运行必需 `.env` 拷到新机 split-stack 根目录）：
  - `bash ai_assistant/ops/bootstrap/seed_split_stack_env_remote.sh --status-only`
  - `bash ai_assistant/ops/bootstrap/seed_split_stack_env_remote.sh`
- 新机器基础软件安装：
  - `bash ai_assistant/ops/bootstrap/install_base_runtime_remote.sh`
- PocketBase 最新版本安装 / 升级：
  - `bash ai_assistant/ops/pocketbase/install/bootstrap_pocketbase_remote.sh`
- PocketBase runtime 载荷部署（landing、empty hooks dir、systemd）：
  - `bash ai_assistant/ops/deploy/deploy_pocketbase_runtime.sh`
- 最小状态迁移（仅 `config` + `_superusers`，不迁业务历史数据）：
  - `bash ai_assistant/ops/pocketbase/migrate/migrate_minimal_state_remote.sh`
- PocketBase schema + minimal state bootstrap（先 `_superusers`，再 schema，最后 `config`）：
  - `bash ai_assistant/ops/pocketbase/migrate/import_schema_remote.sh --email '<pb-superuser-email>' --password '<pb-superuser-password>'`
- 如果旧机已经清掉、无法再复制 `_superusers` / `config`，可先用 `--skip-superusers --skip-config --keep-temp-superuser` 只导 schema，再用 `python3 ai_assistant/extensions/pocketbase/scripts/data/seed_default_records.py --base-url '<pb-base-url>' --email '<temp-superuser-email>' --password '<temp-superuser-password>'` 回填 repo 默认 `config` / `watchlist`。
- 旧交易主机清理（停服务 / 删目录，不碰 3xui / openclaw / crs）：
  - `bash ai_assistant/ops/deploy/prune_old_trading_stack_remote.sh --status-only`
  - `bash ai_assistant/ops/deploy/prune_old_trading_stack_remote.sh --purge-data`
- 公开入口约束：
  - `pb.lzw-glory.top` 根路径必须继续落在 PocketBase 自己的 origin；健康检查现在会把“根路径被重定向到 quant 域名”视为失败。

## pb_public final state

- 当前应保留的能力：
  - 作为 PocketBase 自己的最小 public 入口，展示 auth/data/admin 的角色边界。
  - 保留 `/` 与 `/_/` 这类 PocketBase 自身入口。
- 公网入口不再保留历史 `ibkr_*.html` / `login.html` / 旧别名页面兼容；这些路径现在由反向代理直接返回 404。
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
  - 仅保留空目录占位，避免 PocketBase 启动时缺少 hooks 目录结构。
  - 作为部署兼容载荷随标准 PocketBase runtime 一起同步，但不再单独代表一个运维模式。
- 最终不应该继续保留的能力：
  - 页面托管和页面业务逻辑。
  - 业务 API 主实现。
  - 运行面控制动作主实现。
  - 2FA 编排与 Feishu 交互主实现。
  - 系统监控、心跳、状态摘要、日报、巡检告警主实现。
  - 业务 cron 编排和调度状态推进主实现。
- 最终建议状态：
  - `pb_hooks` 只剩空目录占位；如果确认 PocketBase 对缺失 `hooksDir` 也完全无要求，再整体删除。

## current state snapshot

- `pb_public`
  - 当前定位已经变成 PocketBase 自身 landing 层，不再同步 `ibkr_console`。
  - 仓库里现在只保留 `index.html`，公网代理也只暴露 root/admin/api。
  - 原来的 `common.js` 与 `assets/**` 旧 console 资源树已经从 `pb_public` 清掉，避免继续给人“PB 还在托管前端资源”的错觉。
- `pb_hooks`
  - 目录里已经不再保留任何 `*.pb.js` 入口文件。
  - 历史 `lib/**` 与 `modules/**` 已经从仓库删除，避免继续暗示 PocketBase 还保留业务逻辑或 proxy 实现。
  - 未匹配的 `/api/custom/*` 与 `/webhook/*` 兼容流量现在也会直接在 `ibkr-api` 返回 404，不再回退到 PocketBase 兜底。

## current blockers before pb_hooks deletion

- `runtime/pocketbase/pb_hooks/`
  - 现在只剩空目录占位和 `.gitkeep`。
  - 真正删除整个目录前，只需要再确认 PocketBase 在 `--hooksDir` 指向不存在目录时是否完全无差异。

## current blockers before pb_public deletion

- `runtime/pocketbase/pb_public/index.html`
  - 现在只剩 PocketBase landing。
  - 真正删空前，只需要确认你是否还想保留一个可点开的 PocketBase root landing，而不是只留 `/_/` 和 `/api/*`。

## what should remain vs remove

- `pb_public` 应保留：
  - PocketBase 自己的 landing/admin 职责。
- `pb_public` 应移除：
  - 正式前端源码职责。
  - 独立页面逻辑和独立配置职责。
  - 历史 console 页面文件名。

- `pb_hooks` 应保留：
  - 当前只需要空目录占位。
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
  - 未匹配的 control-plane `/api/custom/*` 与 `/webhook/*` 请求已由 `ibkr-api` 直接拒绝，不再委托 PocketBase。
  - 仓库里的 `pb_hooks` 只剩空目录占位，不再保留可执行业务 JS 模块。
- 域名边界已经收敛为：
  - `quant.lzw-glory.top`：交易系统页面与 API / webhook 公网入口，不再代理 PocketBase collections/auth
  - `pb.lzw-glory.top`：PocketBase auth / collections / admin 与最小 landing 页面，不再承接交易系统控制面兼容入口
  - `qc.lzw-glory.top`：不再作为活跃拓扑的一部分；系统间交互默认走内网 `http://127.0.0.1:51xx`
  - 外部流量也不再依赖 PocketBase 域名下的兼容业务入口。

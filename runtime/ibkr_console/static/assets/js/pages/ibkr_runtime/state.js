let currentBrokerMode = getCurrentBrokerMode();
let currentDataEnvironment = getSharedDataEnvironment();
let currentEnvironment = currentBrokerMode;
        let refreshTimer = null;
        let refreshMode = 'steady';
        let refreshBoostStartedAt = 0;
        let refreshInFlight = false;
        let actionPending = false;
        let latestRuntimeStatus = {};
        let latestTwoFactorState = {};
        let latestStartupState = {};
        let latestBrokerModeSwitchPreview = {};
        let latestServiceMonitorPayload = {};
        let latestServiceActionStates = {};
        let latestNextActionModel = null;
        let latestRuntimeLoadId = 0;
        let hasLoadedRuntimeData = false;
        let runtimePageClosing = false;
        let authActionFeedback = null;
        let actionPendingLabel = '';
        let activeRuntimeOperation = null;
        const RUNTIME_OPERATION_WATCH_ACTIONS = new Set(['start', 'gateway_restart', 'panic_reset_2fa']);
        const RUNTIME_OPERATION_LOCK_ACTIONS = new Set(['start', 'gateway_restart', 'reauth_force_new', 'panic_reset_2fa']);
        const RUNTIME_OPERATION_STORAGE_PREFIX = 'ibkr_runtime_operation_watch';
        const RUNTIME_OPERATION_TTL_MS = 3 * 60 * 1000;
        const RUNTIME_OPERATION_SUCCESS_VISIBLE_MS = 45 * 1000;
        const CHALLENGE_RESET_RECOMMEND_MS = 120 * 1000;
        const RUNTIME_ACTION_LABELS = {
            start: '启动 Runtime 线程',
            stop: '停止 Runtime 线程',
            gateway_restart: '重启 IB Gateway',
            reauth: '刷新飞书卡片（不触发手机）',
            reauth_force_new: '开始 2FA（触发手机）',
            probe: '立即探测认证状态',
            app_login_handoff: '准备登录 IBKR App',
            panic_reset_2fa: '重开 2FA',
            broker_mode_switch: '切换 Paper / Live',
            emergency_all: '全部急停',
            recover_all: '恢复运行开关',
        };
        const MANUAL_AUTH_REASON_LABELS = {
            weekly_reauth: '每周重登提醒',
            manual_start: '启动验证',
            startup: '启动验证',
            manual_reauth: '手动重登验证',
            manual_gateway_restart: '网关重启验证',
            panic_reset_2fa: '重开验证'
        };
        const STARTUP_STEP_LABELS = {
            service_boot: '服务拉起',
            card_ready: '准备 2FA 卡片',
            manual_trigger: '在飞书手动触发 2FA',
            manual_confirm: '完成当前 2FA 验证',
            runtime_resume: '恢复 Runtime',
            health_check: '启动后健康检查'
        };
        const SERVICE_CONTROL_MODULES = [
            {
                service: 'ibkr-runtime',
                title: 'Runtime Service',
                kicker: 'DATA PLANE',
                copy: 'broker session / execution runtime state',
                highRiskRestart: true,
            },
            {
                service: 'ibkr-gateway',
                title: 'IB Gateway',
                kicker: 'BROKER',
                copy: 'IBC + IB Gateway GUI/API',
                highRiskRestart: true,
            },
            {
                service: 'ibkr-scheduler',
                title: 'Scheduler Service',
                kicker: 'SCHEDULER',
                copy: 'cron registry / cursor dispatch',
                highRiskRestart: false,
            },
        ];

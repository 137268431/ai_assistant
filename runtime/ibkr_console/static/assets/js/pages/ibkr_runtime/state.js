let currentEnvironment = getCurrentRuntimeEnvironment();
        let refreshTimer = null;
        let refreshMode = 'steady';
        let refreshBoostStartedAt = 0;
        let refreshInFlight = false;
        let actionPending = false;
        let latestRuntimeStatus = {};
        let latestTwoFactorState = {};
        let latestStartupState = {};
        let latestServiceMonitorPayload = {};
        let latestServiceActionStates = {};
        let latestNextActionModel = null;
        let latestRuntimeLoadId = 0;
        let hasLoadedRuntimeData = false;
        let latestRuntimeBarsSnapshot = [];
        let latestRuntimeIndicatorSnapshot = [];
        let runtimeRecentDataLoading = false;
        let runtimePageClosing = false;
        let authActionFeedback = null;
        const CHALLENGE_RESET_RECOMMEND_MS = 120 * 1000;
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
                copy: 'broker session / live bars / runtime state',
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
                service: 'ibkr-compute',
                title: 'Compute Service',
                kicker: 'COMPUTE',
                copy: 'indicators / signals / backtests',
                highRiskRestart: false,
            },
            {
                service: 'ibkr-scheduler',
                title: 'Scheduler Service',
                kicker: 'SCHEDULER',
                copy: 'cron registry / cursor dispatch',
                highRiskRestart: false,
            },
        ];


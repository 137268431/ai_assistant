let currentEnvironment = getCurrentRuntimeEnvironment();
        let activeStatus = null;
        let batchList = [];
        let selectedBatchId = '';
        let selectedBatch = null;
        let runList = [];
        let selectedRunId = '';
        let selectedRun = null;
        let selectedTrades = [];
        let selectedTargets = [];
        let selectedTargetsLoading = false;
        let selectedSignals = [];
        let selectedReverseSignals = [];
        let backtestRowPageState = {};
        let backtestClientPageState = {};
        let selectedTrackingModel = null;
        let trackingLoading = false;
        let trackingFilterRunId = '';
        let selectedReplayRows = [];
        let equityChart = null;
        let replayChart = null;
        let refreshTimer = null;
        let actionPending = false;
        let activeBacktestTab = 'runs';
        const runFilters = {
            search: '',
            status: 'all',
            source: 'all',
        };
        const BACKTEST_REFRESH_INTERVAL_RUNNING_MS = 10000;
        const BACKTEST_REFRESH_INTERVAL_IDLE_MS = 60000;
        const BACKTEST_ROW_PAGE_CONFIG = Object.freeze({
            trades: { label: 'Trades', noun: '笔交易', perPage: 200 },
            targets: { label: 'Targets', noun: '条 targets', perPage: 200 },
            signals: { label: 'Signals', noun: '条 signals', perPage: 200 },
            reverseSignals: { label: 'Reverse', noun: '条 reverse rows', perPage: 200 },
        });
        const BACKTEST_CLIENT_PAGE_CONFIG = Object.freeze({
            dailyFunnel: {
                label: 'Daily Funnel',
                noun: '个交易日',
                pageSize: 12,
                pageSizeOptions: [12, 24, 48],
            },
        });
        const trackingFilters = {
            date: '',
            symbol: '',
            eventType: '',
        };
        const BACKTEST_TABLE_PREVIEW_LIMITS = Object.freeze({
            runs: 8,
            leaderboard: 50,
            trades: 20,
            targets: 60,
            dataQuality: 12,
            replay: 40,
            trackingTimeline: 120,
            trackingFlows: 36,
        });
        const backtestTableExpandedState = {
            runs: false,
            leaderboard: false,
            trades: false,
            targets: false,
            dataQuality: false,
            replay: false,
            trackingTimeline: false,
            trackingFlows: false,
        };
        const BACKTEST_TEXT_PREVIEW_LIMITS = Object.freeze({
            batchVariants: 1800,
            strategyParams: 1800,
            runtimeExtra: 2200,
        });
        const backtestTextExpandedState = {
            batchVariants: false,
            strategyParams: false,
            runtimeExtra: false,
        };

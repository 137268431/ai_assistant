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
        let selectedTrackingModel = null;
        let trackingLoading = false;
        let trackingFilterRunId = '';
        let selectedReplayRows = [];
        let equityChart = null;
        let replayChart = null;
        let refreshTimer = null;
        let actionPending = false;
        let activeBacktestTab = 'runs';
        const BACKTEST_REFRESH_INTERVAL_RUNNING_MS = 10000;
        const BACKTEST_REFRESH_INTERVAL_IDLE_MS = 60000;
        const trackingFilters = {
            date: '',
            symbol: '',
            eventType: '',
        };
        const BACKTEST_TABLE_PREVIEW_LIMITS = Object.freeze({
            leaderboard: 50,
            trades: 20,
            replay: 40,
            trackingTimeline: 120,
            trackingFlows: 36,
        });
        const backtestTableExpandedState = {
            leaderboard: false,
            trades: false,
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

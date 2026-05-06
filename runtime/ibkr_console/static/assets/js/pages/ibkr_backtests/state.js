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
        let selectedReplayRows = [];
        let equityChart = null;
        let replayChart = null;
        let refreshTimer = null;
        let actionPending = false;
        let activeBacktestTab = 'runs';
        const BACKTEST_TABLE_PREVIEW_LIMITS = Object.freeze({
            leaderboard: 50,
            trades: 20,
            replay: 40,
        });
        const backtestTableExpandedState = {
            leaderboard: false,
            trades: false,
            replay: false,
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

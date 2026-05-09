const SUPPORTED_INTERVALS = ['5m', '15m', '30m', '1h', '4h', '1d'];
        const CHART_UI_PREFS_KEY = 'ibkr_chart_ui_prefs_v2';
        const CHART_MOBILE_HINT_KEY = 'ibkr_chart_mobile_hint_seen_v1';
        const RANGE_PRESETS = [
            { key: '1d', shortLabel: '1D', longLabel: '最近1天', durationMs: 24 * 60 * 60 * 1000 },
            { key: '3d', shortLabel: '3D', longLabel: '最近3天', durationMs: 3 * 24 * 60 * 60 * 1000 },
            { key: '1w', shortLabel: '1W', longLabel: '最近1周', durationMs: 7 * 24 * 60 * 60 * 1000 },
            { key: '1m', shortLabel: '1M', longLabel: '最近1月', durationMs: 31 * 24 * 60 * 60 * 1000 },
            { key: 'custom', shortLabel: '自定义', longLabel: '自定义范围', durationMs: null },
            { key: 'all', shortLabel: 'ALL', longLabel: '全部历史', durationMs: 0 },
        ];
        let currentEnvironment = getCurrentRuntimeEnvironment();
        let currentSymbol = '';
        let currentInterval = '5m';
        let currentRangeKey = '1d';
        let currentBacktestRunId = '';
        let currentAnchorMs = 0;
        let customRangeStartMs = 0;
        let customRangeEndMs = 0;
        let customRangeFormOpen = false;
        let customRangeStartPicker = null;
        let customRangeEndPicker = null;
        let pendingFocusBarTimeMs = 0;
        let currentIndicatorId = '';
        let chartInstance = null;
        let lastPayload = null;
        let chartDisplayPayload = null;
        let chartWorkspaceState = 'idle';
        let realtimeQuoteSnapshot = null;
        let formingBarSnapshot = null;
        let chartRealtimeQuoteError = '';
        let chartRealtimeQuoteFailureCount = 0;
        let chartRealtimePreviewError = '';
        let chartRealtimePollTimer = 0;
        let comparePayload = null;
        let compareLoading = false;
        let compareError = '';
        let realtimeComputedPayload = null;
        let selectedBarIndex = -1;
        let selectedSignalId = '';
        let hoverBarIndex = -1;
        let activeDrawerSignalId = '';
        let chartZoomState = null;
        let chartViewportState = { start: 0, end: 100, startIndex: 0, endIndex: 0, visibleBars: 0, totalBars: 0 };
        let activeRailCardIndex = 0;
        let mobileQuickPanelOpen = false;
        let chartFocusMode = false;
        let chartPointerLocked = false;
        let inspectorDrawerOpen = false;
        const TRACE_PANEL_PAGE_SIZE = 20;
        let chartTracePanelOpen = false;
        let tracePanelPage = 1;
        let tracePanelManualPage = false;
        let chartLegendCollapsed = true;
        let chartMarkerDensityTier = '';
        let chartTooltipSyncRaf = 0;
        let chartTooltipSyncTimer = 0;
        let chartTooltipPinned = false;
        let chartFocusMarkerSyncRaf = 0;
        let mobileGestureHintSeen = false;
        let suppressNextChartClick = false;
        let chartSelectionMode = false;
        let chartSelectionSession = null;
        let chartTouchSession = {
            timer: 0,
            startX: 0,
            startY: 0,
            moved: false,
            lastTapAt: 0,
            lastTapX: 0,
            lastTapY: 0,
        };
        let chartLayerState = {
            ema: true,
            vwap: true,
            sdChannel: true,
            fractal: true,
            emaTouch: true,
            divergence: true,
            tradeSignals: true,
            riskLevels: true,
            lifecycle: true,
            volume: true,
        };
        const SIGNAL_LIFECYCLE_Y = Object.freeze({
            upperWindow: 4.15,
            lowerWindow: 3.8,
            dtp: 3.02,
            component: 2.05,
            decision: 1.08,
            cleared: 0.46,
        });
        const CHART_REALTIME_WARN_INTERVAL_MS = 60 * 1000;
        const chartRealtimeWarnState = {
            quote: { key: '', at: 0 },
            preview: { key: '', at: 0 },
        };

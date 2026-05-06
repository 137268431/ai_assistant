let currentEnvironment = getCurrentRuntimeEnvironment();
    let activeTab = 'screener';
    let activeScreenerView = 'current';
    let screenerPayload = { items: [], summary: {}, filters: {} };
    let todayTargetsPayload = {
      items: [],
      summary: {},
      market_date: '',
      filtered_total: 0,
      filtered_summary: {},
      page: 1,
      total_pages: 1,
    };
    let windowProgressPayload = {
      items: [],
      summary: {},
      market_date: '',
      computed_at_us: '',
    };
    let runtimeCurrentMarketDate = '';
    let rulesPayload = { selection: null, signals: null, computed_at_us: '' };
    let rulesLoadError = '';
    let filteredRows = [];
    let filteredCurrentTargetRows = [];
    let activeWindowProgressStatus = 'all';
    const selectedSymbols = new Set();
    const dailyTargetsState = {
      selectedDate: '',
      items: [],
      searchResults: [],
      loaded: false,
      loadedDate: '',
      lastRefresh: '尚未加载'
    };
    const watchlistState = {
      items: [],
      searchResults: [],
      loaded: false,
      lastRefresh: '尚未加载',
      loadedRole: '',
      configLoadError: ''
    };
    const CONFIG_MONITOR_SOURCE = 'config_market_ws_symbols';
    const currentTargetState = {
      page: 1,
      perPage: 10,
      requestToken: 0,
      searchDebounceId: 0,
    };
    const windowProgressState = {
      requestToken: 0,
      loadedKey: '',
    };
    const WINDOW_PROGRESS_STATUS_TABS = [
      { key: 'all', label: '全部' },
      { key: 'candidate', label: '候选' },
      { key: 'blocked', label: '阻塞' },
      { key: 'near_expiry', label: '临期' },
      { key: 'active', label: '活跃' },
      { key: 'no_window', label: '无窗口' },
      { key: 'other', label: '其他' },
    ];
    const manualDailyScanState = {
      running: false,
      lastResult: null,
      status: 'idle',
      message: '等待补跑',
    };
    const screenerPaginationState = {
      page: 1,
      perPage: 10,
    };
    const watchlistPaginationState = {
      page: 1,
      perPage: 10,
    };
    let currentFiltersExpanded = false;
    let rulesLoaded = false;
    let rulesLoadingPromise = null;
    let screenerLoadKey = '';
    let responsiveStateBound = false;
    let lastPhoneViewport = null;
    const expandedRulesPanels = new Set();
    let activeReadyTipId = '';
    let activeReadyTipTrigger = null;
    let activeReadyTipHoverRoot = null;
    let readyTipCloseTimer = 0;


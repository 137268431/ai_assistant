    async function ensureActiveScreenerDataLoaded({ force = false } = {}) {
      if (activeScreenerView === 'universe') {
        return loadScreener(false, { loadCurrentTargetsAfter: false, force });
      }
      if (activeScreenerView === 'window-progress') {
        await loadWindowProgress(false, { force });
        return;
      }
      await loadTodayTargets(false);
    }

    function renderScreenerSummary() {
      const summary = screenerPayload.summary || {};
      renderSummaryCards([
        { label: 'TOTAL', value: summary.total || 0, copy: '标的总数' },
        { label: 'LIVE BARS', value: summary.with_live_bars || 0, copy: '已有 5m bar', className: 'teal' },
        { label: 'OPERABLE', value: summary.operable || 0, copy: '可操作标的', className: 'good' },
        { label: 'CANDIDATES', value: summary.candidate_targets || 0, copy: 'candidate 数', className: 'accent' },
        { label: 'ACTIVE', value: summary.active_targets || 0, copy: 'active 数', className: 'good' },
        { label: 'DATA GAP', value: summary.incomplete_data || 0, copy: summary.incomplete_data ? '异步 API 补偿中' : '数据完整', className: summary.incomplete_data ? 'accent' : 'good' }
      ]);
    }

    function renderCurrentTargetsSummary() {
      const summary = todayTargetsPayload.summary || {};
      const readyTip = buildReadyDefinitionTip(todayTargetsPayload.workflow?.ready_definition || {});
      renderSummaryCards([
        { label: 'TOTAL', value: summary.total || 0, copy: '当日标的数' },
        { label: 'READY', value: summary.technical_ready_count || 0, copy: 'ready 标的', className: 'good', tip: readyTip },
        { label: 'SIGNALLED', value: summary.signaled_count || 0, copy: '已有信号', className: 'teal' },
        {
          label: 'NEEDS ACTION',
          value: (summary.awaiting_confirm_count || 0) + (summary.pending_count || 0) + (summary.protection_incomplete_count || 0),
          copy: '待确认/待执行/保护异常',
          className: 'accent'
        },
        {
          label: 'SUBMITTED',
          value: (summary.submitted_count || 0) + (summary.protected_active_count || 0) + (summary.protection_incomplete_count || 0),
          copy: '券商订单/保护单',
          className: 'teal'
        },
        { label: 'EXECUTED', value: summary.executed_count || 0, copy: '已执行', className: 'good' },
        { label: 'STALE', value: summary.stale_count || 0, copy: '数据过期', className: 'accent' }
      ]);
    }

    function firstFiniteSummaryValue(values, fallback = 0) {
      for (const value of values || []) {
        const numberValue = Number(value);
        if (Number.isFinite(numberValue)) return numberValue;
      }
      return fallback;
    }

    function getWindowProgressSummaryCounts(rows, summary) {
      const rowCounts = getWindowProgressStatusCounts(rows);
      const traceStageCounts = summary.trace_stage_counts || windowProgressPayload.trace_stage_counts || {};
      const windowStatusCounts = summary.window_status_counts || windowProgressPayload.window_status_counts || {};
      return {
        latest5m: firstFiniteSummaryValue([
          summary.latest_5m_count,
          summary.with_live_bar_count,
          summary.live_5m_count,
        ], rows.filter((row) => hasWindowProgressLatestBar(row) && !isWindowProgressStale(row)).length),
        stale: firstFiniteSummaryValue([
          summary.stale_count,
          windowStatusCounts.stale,
        ], rowCounts.stale || 0),
        signalCandidate: firstFiniteSummaryValue([
          summary.current_candidate_signal_count,
          traceStageCounts.signal_candidate,
          traceStageCounts.candidate,
          windowStatusCounts.signal_candidate,
          windowStatusCounts.candidate,
          summary.candidate_signal_count,
        ], rowCounts.signal_candidate || 0),
        confirmed: firstFiniteSummaryValue([
          summary.confirmed_count,
          traceStageCounts.confirmed,
          windowStatusCounts.confirmed,
        ], rowCounts.confirmed || 0),
        blocked: firstFiniteSummaryValue([
          summary.blocked_count,
          traceStageCounts.blocked,
          windowStatusCounts.blocked,
        ], rowCounts.blocked || 0),
        near: firstFiniteSummaryValue([
          summary.near_expiry_count,
          windowStatusCounts.near_expiry,
        ], rowCounts.near_expiry || 0),
        targetCandidate: firstFiniteSummaryValue([
          summary.target_candidate_count,
          summary.candidate_count,
        ], rowCounts.target_candidate || 0),
      };
    }

    function renderWindowProgressSummary() {
      const rows = getSortedWindowProgressRows();
      const summary = windowProgressPayload.summary || {};
      const counts = getWindowProgressSummaryCounts(rows, summary);
      renderSummaryCards([
        { label: 'LATEST 5M', value: counts.latest5m, copy: `最新 5m 可用 / total ${summary.total ?? rows.length}`, className: 'teal' },
        { label: 'STALE', value: counts.stale, copy: `freshness > ${WINDOW_PROGRESS_STALE_MINUTES}m 或无 bar`, className: counts.stale ? 'accent' : 'good' },
        { label: 'SIGNAL CANDIDATE', value: counts.signalCandidate, copy: 'trace/signal 候选，不含目标池 candidate', className: 'accent' },
        { label: 'CONFIRMED', value: counts.confirmed, copy: 'trace 已确认', className: 'good' },
        { label: 'BLOCKED', value: counts.blocked, copy: '过滤阻塞/failed gates', className: 'accent' },
        { label: 'NEAR', value: counts.near, copy: '窗口临期', className: 'teal' },
        { label: 'TARGET CANDIDATE', value: counts.targetCandidate, copy: '目标池 candidate', className: 'accent' },
      ]);
    }

    function formatRecordEnvironment(environment) {
      const normalized = String(environment || '').trim().toLowerCase();
      if (!normalized) return 'LIVE*';
      return getEnvironmentLabel(normalized, true);
    }

    function formatWatchlistRole(role) {
      return normalizeWatchlistRole(role) === 'market_monitor' ? 'market_monitor' : 'trade';
    }

    function formatWatchlistMember(item) {
      if (isConfigMonitorItem(item)) return 'CONFIG';
      const raw = item ? item.manual_member : undefined;
      const normalized = String(raw == null ? '' : raw).trim().toLowerCase();
      if (raw === false || normalized === 'false' || normalized === '0' || normalized === 'no') {
        return 'AUTO(TARGET)';
      }
      return 'MANUAL';
    }

    function normalizeWatchlistRole(role) {
      return String(role || '').trim().toLowerCase() === 'market_monitor' ? 'market_monitor' : 'trade';
    }

    function resolveRecordEnvClass(environment) {
      const normalized = String(environment || '').trim().toLowerCase();
      if (!normalized) return 'env-live';
      return `env-${normalizeConfigEnvironment(normalized, 'global')}`;
    }

    function getFilteredWatchlistItems() {
      const keyword = String(document.getElementById('listSearchInput').value || '').trim().toUpperCase();
      const activeRole = getWatchlistRoleForTab();
      const rank = (environment) => {
        const normalized = String(environment || '').trim().toLowerCase();
        if (normalized === currentEnvironment) return 3;
        if (!normalized) return 2;
        if (normalized === 'global') return 1;
        return 0;
      };

      return watchlistState.items
        .filter((item) => normalizeWatchlistRole(item.symbol_role) === activeRole)
        .filter((item) => {
          if (!keyword) return true;
          const displayItem = typeof withEligibilityDisplayFields === 'function' ? withEligibilityDisplayFields(item) : item;
          return [
            displayItem.symbol,
            displayItem.exchange,
            displayItem.industry,
            displayItem.note,
            formatWatchlistRole(displayItem.symbol_role),
            formatRecordEnvironment(displayItem.environment),
            formatWatchlistMember(displayItem),
            renderSymbolProfileSummary(displayItem),
            getFailedGates(displayItem).join(' '),
            getAdmissionScore(displayItem),
          ].some((value) => String(value || '').toUpperCase().includes(keyword));
        })
        .sort((left, right) => {
          const rankDiff = rank(right.environment) - rank(left.environment);
          if (rankDiff !== 0) return rankDiff;
          return String(left.symbol || '').localeCompare(String(right.symbol || ''));
        });
    }

    function renderWatchlistSummary() {
      const items = getFilteredWatchlistItems();
      const currentCount = items.filter((item) => String(item.environment || '').trim().toLowerCase() === currentEnvironment).length;
      const globalCount = items.filter((item) => String(item.environment || '').trim().toLowerCase() === 'global').length;
      const unscopedCount = items.filter((item) => !String(item.environment || '').trim()).length;
      const configCount = items.filter((item) => isConfigMonitorItem(item)).length;
      const roleLabel = getWatchlistRoleLabel();
      const cards = [
        { label: 'VISIBLE', value: items.length, copy: `可见${roleLabel}数` },
        { label: 'CURRENT ENV', value: currentCount, copy: '当前环境', className: 'good' },
        { label: 'GLOBAL', value: globalCount, copy: 'GLOBAL 共享', className: 'teal' },
      ];
      if (getWatchlistRoleForTab() === 'market_monitor') {
        cards.push({ label: 'CONFIG', value: configCount, copy: '配置监控', className: 'accent' });
      } else {
        cards.push({ label: 'UNSCOPED', value: unscopedCount, copy: '未分区记录', className: 'accent' });
      }
      renderSummaryCards(cards);
    }

    function getDailyTargetDate() {
      return document.getElementById('dailyTargetDate')?.value
        || dailyTargetsState.selectedDate
        || document.getElementById('marketDate')?.value
        || getUsDate();
    }

    function getManualTargetAllowedDate() {
      return runtimeCurrentMarketDate || getUsDate();
    }

    function isManualTargetDateAllowed(date = getDailyTargetDate()) {
      return String(date || '').trim() === String(getManualTargetAllowedDate() || '').trim();
    }

    function ensureManualTargetDateAllowed() {
      const selectedDate = getDailyTargetDate();
      const allowedDate = getManualTargetAllowedDate();
      if (isManualTargetDateAllowed(selectedDate)) {
        return true;
      }
      showToast(`手动加入目标池只支持当前交易日 ${allowedDate}`);
      return false;
    }

    function getFilteredDailyTargetItems() {
      const keyword = String(document.getElementById('dailyTargetTableSearchInput')?.value || '').trim().toUpperCase();
      return dailyTargetsState.items
        .filter((item) => {
          if (!keyword) return true;
          return [
            item.symbol,
            item.exchange,
            item.scan_reason,
            item.status,
            item.direction_bias
          ].some((value) => String(value || '').toUpperCase().includes(keyword));
        })
        .sort((left, right) => {
          const scoreDiff = Number(right.score || 0) - Number(left.score || 0);
          if (scoreDiff !== 0) return scoreDiff;
          return String(left.symbol || '').localeCompare(String(right.symbol || ''));
        });
    }

    function renderTargetsSummary() {
      const items = getFilteredDailyTargetItems();
      const active = items.filter((item) => String(item.status || '') === 'active').length;
      const candidate = items.filter((item) => String(item.status || '') === 'candidate').length;
      const removed = items.filter((item) => String(item.status || '') === 'removed').length;
      const avgScore = items.length
        ? (items.reduce((sum, item) => sum + Number(item.score || 0), 0) / items.length).toFixed(1)
        : '0.0';
      renderSummaryCards([
        { label: 'VISIBLE', value: items.length, copy: '可见记录' },
        { label: 'ACTIVE', value: active, copy: 'active 记录', className: 'good' },
        { label: 'CANDIDATE', value: candidate, copy: 'candidate 记录', className: 'accent' },
        { label: 'REMOVED', value: removed, copy: 'removed 记录', className: 'teal' },
        { label: 'AVG SCORE', value: avgScore, copy: '平均 score', className: 'accent' }
      ]);
    }

    function updateHero() {
      syncManualDailyScanButton();
      if (isWatchlistRoleTab()) {
        const visible = getFilteredWatchlistItems().length;
        const roleLabel = getWatchlistRoleLabel();
        const isMonitorTab = getWatchlistRoleForTab() === 'market_monitor';
        const configCount = watchlistState.items.filter((item) => isConfigMonitorItem(item)).length;
        document.getElementById('heroTitle').textContent = isMonitorTab ? '维护市场监控池。' : '维护运行标池。';
        document.getElementById('heroCopy').textContent = isMonitorTab
          ? '显示 ibkr_market_ws_symbols 配置监控 + 手动 market_monitor 记录；只监控不交易。'
          : '搜索 IBKR 合约，写入环境或 GLOBAL。';
        setPageContextMeta([
          { label: '环境', value: getEnvironmentLabel(currentEnvironment), tone: currentEnvironment },
          { label: 'Scope', value: `${getEnvironmentLabel(currentEnvironment)} + GLOBAL` },
          { label: '角色', value: roleLabel },
          ...(isMonitorTab ? [{ label: '配置', value: `${configCount} 个` }] : []),
          { label: '可见', value: `${visible} 条` },
        ]);
        setHeroMetaLine('marketDateMeta');
        setHeroMetaLine('refreshInfo', watchlistState.lastRefresh);
        setHeroMetaLine('selectionInfo');
        renderWatchlistSummary();
        document.getElementById('watchlistSearchPanelTitle').textContent = isMonitorTab ? '搜索可加入的市场监控标的' : '搜索可加入的标的';
        document.getElementById('watchlistSearchPanelCopy').textContent = isMonitorTab
          ? '支持 ticker 或公司名；只监控不交易。'
          : '支持 ticker 或公司名。';
        document.getElementById('watchlistListPanelTitle').textContent = isMonitorTab ? '已有市场监控记录' : '已有 watchlist 记录';
        document.getElementById('watchlistListPanelCopy').textContent = isMonitorTab
          ? '配置项来自 ibkr_market_ws_symbols，手动项来自 watchlist.market_monitor。'
          : '管理 trade watchlist 记录。';
        document.getElementById('searchMeta').textContent = isMonitorTab ? '输入后回车或点击搜索，加入市场监控。' : '输入后回车或点击搜索。';
        document.getElementById('batchMeta').textContent = isMonitorTab
          ? '批量搜索并写入 market_monitor。'
          : '批量搜索并写入当前 scope。';
        document.getElementById('deleteMeta').textContent = isMonitorTab ? '适合清理一组市场监控标的。' : '适合清理一组手工加入的标的。';
        return;
      }

      if (activeTab === 'targets') {
        const visible = getFilteredDailyTargetItems().length;
        document.getElementById('heroTitle').textContent = '维护每日标的。';
        document.getElementById('heroCopy').textContent = '搜索合约，补录、修正和清理 ibkr_targets。';
        setPageContextMeta([
          { label: '环境', value: getEnvironmentLabel(currentEnvironment), tone: currentEnvironment },
          { label: 'Target Date', value: getDailyTargetDate() },
          { label: '当前', value: `${visible} 条` },
          { label: '搜索候选', value: `${dailyTargetsState.searchResults.length} 个` },
        ]);
        setHeroMetaLine('marketDateMeta');
        setHeroMetaLine('refreshInfo', dailyTargetsState.lastRefresh);
        setHeroMetaLine('selectionInfo');
        renderTargetsSummary();
        return;
      }

      if (activeScreenerView === 'current') {
        const summary = todayTargetsPayload.summary || {};
        const visible = Array.isArray(filteredCurrentTargetRows) ? filteredCurrentTargetRows.length : 0;
        const visibleReady = (filteredCurrentTargetRows || []).filter((row) => row.technical_state === 'ready').length;
        const visibleActionable = (filteredCurrentTargetRows || []).filter((row) => ['awaiting_confirm', 'pending', 'submitted', 'protected_active', 'protection_incomplete'].includes(String(row.latest_signal_status || ''))).length;
        const currentPage = Math.max(1, Number(todayTargetsPayload.page || currentTargetState.page || 1) || 1);
        const totalPages = Math.max(1, Number(todayTargetsPayload.total_pages || 1) || 1);
        const filteredTotal = Number(todayTargetsPayload.filtered_total || visible || 0) || 0;
        document.getElementById('heroTitle').textContent = '当前标的工作台。';
        document.getElementById('heroCopy').textContent = '聚合 targets、技术状态和待处理信号。';
        setPageContextMeta([
          { label: '环境', value: getEnvironmentLabel(currentEnvironment), tone: currentEnvironment },
          { label: 'Market Date', value: todayTargetsPayload.market_date || screenerPayload.market_date || document.getElementById('marketDate').value || '--' },
          { label: '页码', value: `${currentPage}/${totalPages}` },
          { label: '可见', value: `${visible} 条` },
          { label: 'ready/action', value: `${visibleReady}/${visibleActionable}` },
          { label: '过滤', value: `${filteredTotal}/${summary.total || 0}` },
        ]);
        setHeroMetaLine('marketDateMeta');
        setHeroMetaLine('refreshInfo', todayTargetsPayload.computed_at_us
          ? '数据已加载'
          : '数据未刷新');
        setHeroMetaLine('selectionInfo');
        renderCurrentTargetsSummary();
        return;
      }

      if (activeScreenerView === 'window-progress') {
        const rows = getSortedWindowProgressRows();
        const marketDate = windowProgressPayload.market_date || todayTargetsPayload.market_date || screenerPayload.market_date || document.getElementById('marketDate').value || '--';
        const summaryCounts = getWindowProgressSummaryCounts(rows, windowProgressPayload.summary || {});
        document.getElementById('heroTitle').textContent = '窗口进度工作台。';
        document.getElementById('heroCopy').textContent = '跟踪上下轨窗口、组件收集、剩余 bars 和 trace 链路。';
        setPageContextMeta([
          { label: '环境', value: getEnvironmentLabel(currentEnvironment), tone: currentEnvironment },
          { label: 'Market Date', value: marketDate },
          { label: '窗口', value: `${rows.length} 条` },
          { label: 'signal', value: `${summaryCounts.signalCandidate}` },
          { label: 'confirmed', value: `${summaryCounts.confirmed}` },
          { label: 'blocked', value: `${summaryCounts.blocked}` },
          { label: 'target candidate', value: `${summaryCounts.targetCandidate}` },
        ]);
        setHeroMetaLine('marketDateMeta');
        setHeroMetaLine('refreshInfo', windowProgressPayload.computed_at_us ? '窗口进度已加载' : '窗口进度未刷新');
        setHeroMetaLine('selectionInfo');
        renderWindowProgressSummary();
        return;
      }

      const selectedRows = (screenerPayload.items || []).filter((row) => selectedSymbols.has(String(row.symbol || '').trim().toUpperCase()));
      const operableCount = selectedRows.filter((row) => row.is_operable).length;
      document.getElementById('heroTitle').textContent = '先筛选，再入池。';
      document.getElementById('heroCopy').textContent = '查看 bars、量能和方向，并维护 watchlist。';
      setPageContextMeta([
        { label: '环境', value: getEnvironmentLabel(currentEnvironment), tone: currentEnvironment },
        { label: 'Market Date', value: screenerPayload.market_date || document.getElementById('marketDate').value || '--' },
        { label: '已选择', value: `${selectedRows.length || selectedSymbols.size} 个` },
        { label: '可操作', value: `${operableCount} 个` },
      ]);
      setHeroMetaLine('marketDateMeta');
      setHeroMetaLine('refreshInfo', screenerPayload.computed_at_us
        ? '数据已加载'
        : '数据未刷新');
      setHeroMetaLine('selectionInfo');
      renderScreenerSummary();
    }

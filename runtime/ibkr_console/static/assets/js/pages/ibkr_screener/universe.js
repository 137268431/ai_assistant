    function mergeScreenerRowWithRealtimeQuote(row) {
      const quote = getRealtimeQuote(row?.symbol);
      return {
        ...row,
        display_price: quote?.last_price != null ? quote.last_price : row?.price,
        display_day_change_pct: quote?.day_change_pct != null ? quote.day_change_pct : row?.day_change_pct,
        display_price_source: quote?.last_price != null ? 'realtime_quote' : (row?.price_source || '--'),
        realtime_quote_age_s: quote?.quote_age_s != null ? quote.quote_age_s : null,
      };
    }

    async function refreshScreenerQuotes(items, loadKey) {
      if (!Array.isArray(items) || !items.length) return;
      try {
        await fetchRealtimeQuotesIfNeeded(items.map((row) => row?.symbol).filter(Boolean), {
          reset: false,
          maxAgeMs: 15000,
        });
      } catch (error) {
        console.warn('加载 screener 实时报价失败:', error);
        return;
      }
      if (screenerLoadKey !== loadKey) return;
      screenerPayload = {
        ...screenerPayload,
        items: Array.isArray(screenerPayload.items)
          ? screenerPayload.items.map((row) => mergeScreenerRowWithRealtimeQuote(row))
          : [],
      };
      applyFilters({ resetPage: false });
      if (activeTab === 'screener') updateHero();
    }

    function statusChip(label, className) {
      return `<span class="status-chip ${escapeHtml(className || '')}">${escapeHtml(label || '--')}</span>`;
    }

    function getScreenerPageSize() {
      const rawValue = Number(document.getElementById('screenerPageSize')?.value || screenerPaginationState.perPage || 10);
      if (!Number.isFinite(rawValue) || rawValue <= 0) return 10;
      return Math.max(1, Math.min(100, Math.trunc(rawValue)));
    }

    function getScreenerPageButtons(page, totalPages) {
      const pages = [];
      const pushPage = (value) => {
        if (pages.includes(value)) return;
        pages.push(value);
      };
      pushPage(1);
      for (let index = page - 1; index <= page + 1; index += 1) {
        if (index > 1 && index < totalPages) pushPage(index);
      }
      if (totalPages > 1) pushPage(totalPages);
      return pages.sort((left, right) => left - right);
    }

    function getScreenerPageRows() {
      screenerPaginationState.perPage = getScreenerPageSize();
      const totalRows = Array.isArray(filteredRows) ? filteredRows.length : 0;
      const totalPages = Math.max(1, Math.ceil(totalRows / screenerPaginationState.perPage));
      screenerPaginationState.page = Math.max(1, Math.min(totalPages, Number(screenerPaginationState.page) || 1));
      const startIndex = (screenerPaginationState.page - 1) * screenerPaginationState.perPage;
      return filteredRows.slice(startIndex, startIndex + screenerPaginationState.perPage);
    }

    function renderScreenerPagination(pageRows) {
      screenerPaginationState.perPage = getScreenerPageSize();
      const totalRows = Array.isArray(filteredRows) ? filteredRows.length : 0;
      const totalPages = Math.max(1, Math.ceil(totalRows / screenerPaginationState.perPage));
      const page = Math.max(1, Math.min(totalPages, Number(screenerPaginationState.page) || 1));
      screenerPaginationState.page = page;
      const returnedCount = Array.isArray(pageRows) ? pageRows.length : 0;
      const pageButtons = getScreenerPageButtons(page, totalPages);
      const shouldShowPagination = totalRows > screenerPaginationState.perPage;
      const controls = [];

      controls.push(`<button class="mini-btn pagination-btn" type="button" onclick="setScreenerPage(${page - 1})" ${page <= 1 ? 'disabled' : ''}>上一页</button>`);
      let lastPage = 0;
      pageButtons.forEach((value) => {
        if (lastPage && value - lastPage > 1) {
          controls.push('<span class="pagination-ellipsis">...</span>');
        }
        controls.push(`<button class="mini-btn pagination-btn ${value === page ? 'active' : ''}" type="button" onclick="setScreenerPage(${value})">${value}</button>`);
        lastPage = value;
      });
      controls.push(`<button class="mini-btn pagination-btn" type="button" onclick="setScreenerPage(${page + 1})" ${page >= totalPages ? 'disabled' : ''}>下一页</button>`);

      ['screenerPaginationTop', 'screenerPaginationBottom'].forEach((id) => {
        const mount = document.getElementById(id);
        if (!mount) return;
        const bar = mount.closest('.pagination-bar');
        if (bar) bar.hidden = !shouldShowPagination;
        mount.innerHTML = shouldShowPagination ? controls.join('') : '';
      });

      const statusText = totalRows
        ? `第 ${page} / ${totalPages} 页 · 本页 ${returnedCount} 条 · 过滤后 ${totalRows} 条`
        : '第 1 / 1 页 · 当前没有结果';
      ['screenerPaginationStatusTop', 'screenerPaginationStatusBottom'].forEach((id) => {
        const mount = document.getElementById(id);
        if (mount) mount.textContent = shouldShowPagination ? statusText : '';
      });
    }

    window.setScreenerPage = function setScreenerPage(page) {
      const totalPages = Math.max(1, Math.ceil((filteredRows.length || 0) / getScreenerPageSize()));
      const nextPage = Math.max(1, Math.min(totalPages, Number(page) || 1));
      if (nextPage === screenerPaginationState.page) return;
      screenerPaginationState.page = nextPage;
      renderTable();
    };

    function renderTable() {
      const tbody = document.getElementById('screenerTable');
      if (!Array.isArray(filteredRows) || !filteredRows.length) {
        tbody.innerHTML = '<tr><td colspan="9" class="empty">当前条件下没有符合的标的</td></tr>';
        renderScreenerCards([]);
        renderScreenerPagination([]);
        document.getElementById('tableMeta').textContent = '0 条结果';
        document.getElementById('tableMetaSecondary').textContent = '';
        return;
      }

      const pageRows = getScreenerPageRows();
      tbody.innerHTML = pageRows.map((row) => {
        const checked = selectedSymbols.has(String(row.symbol || '').trim().toUpperCase()) ? 'checked' : '';
        return `
          <tr>
            <td>
              <input class="row-check" type="checkbox" ${checked} onchange="toggleSelection('${escapeHtml(row.symbol)}', this.checked)" />
            </td>
            <td>
              <a class="symbol-link" href="${buildPageUrl('/ibkr_chart.html', { symbol: row.symbol, interval: '5m' }, { environment: currentEnvironment })}">${escapeHtml(row.symbol)}</a><br>
              <span class="muted mono">${escapeHtml(row.display_price_source || row.price_source || '--')}</span>
              ${renderSymbolProfileSummary(row) ? `<div style="margin-top:8px;">${renderSymbolProfileSummary(row)}</div>` : ''}
            </td>
            <td>
              ${escapeHtml(row.exchange || '--')}<br>
              <span class="muted">${escapeHtml(row.industry || '--')}</span>
            </td>
            <td>
              <strong>${escapeHtml(formatPrice(row.display_price ?? row.price))}</strong><br>
              <span class="mono">${escapeHtml(formatPct(row.display_day_change_pct ?? row.day_change_pct))}</span><br>
              <span class="muted">7D ${escapeHtml(formatPct(row.change_7d))}</span>
            </td>
            <td>
              <span class="mono">ATR ${escapeHtml(formatPct(row.atr_pct))}</span><br>
              <span class="muted">10D ${escapeHtml(formatVolume(row.avg_10d_volume))}</span><br>
              <span class="muted">PRE ${escapeHtml(formatVolume(row.premarket_volume))} · DAY ${escapeHtml(formatVolume(row.today_volume))}</span>
            </td>
            <td>
              ${row.has_live_bar ? statusChip(formatFreshness(row.freshness_min), Number(row.freshness_min) <= 30 ? 'active' : 'candidate') : statusChip('无当日bar', '')}<br>
              ${dataQualityChip(row)}<br>
              <span class="muted">${escapeHtml(row.latest_us_time || '--')}</span>
            </td>
            <td>
              ${statusChip(row.target_status || 'none', row.target_status || '')}<br>
              ${statusChip(row.direction_bias || 'neutral', row.direction_bias || 'neutral')}<br>
              <span class="muted">score ${escapeHtml(formatNumber(row.target_score || 0, 1))}</span>
              ${renderAdmissionControlRow(row)}
            </td>
            <td>
              ${buildScorePill(row)}<br>
              <span class="muted">${row.is_operable ? '可操作' : '人工复核'}</span>
            </td>
            <td>
              <div class="reason-block">
                <div class="reason-label">筛选理由</div>
                <div class="reason-copy">${escapeHtml(row.scan_reason || row.note || '--')}</div>
              </div>
              <div class="reason-block" style="margin-top:10px;">
                <div class="reason-label">可操作依据</div>
                ${buildReasonPills(row)}
              </div>
              ${renderAdmissionDiagnosticsBlock(row)}
              <div style="margin-top:10px;">
                <a class="mini-link" href="${buildPageUrl('/ibkr_chart.html', { symbol: row.symbol, interval: '5m' }, { environment: currentEnvironment })}">Chart</a>
              </div>
            </td>
          </tr>
        `;
      }).join('');
      renderScreenerCards(pageRows);
      renderScreenerPagination(pageRows);

      document.getElementById('tableMeta').textContent = `过滤 ${filteredRows.length} · 本页 ${pageRows.length} · 可操作 ${filteredRows.filter((row) => row.is_operable).length} · live bars ${filteredRows.filter((row) => row.has_live_bar).length} · 补偿中 ${filteredRows.filter((row) => Boolean(row?.data_quality?.needs_repair)).length}`;
      document.getElementById('tableMetaSecondary').textContent = `已选 ${filteredRows.filter((row) => selectedSymbols.has(String(row.symbol || '').trim().toUpperCase())).length} 条`;
    }

    function applyFilters({ resetPage = true } = {}) {
      if (resetPage) screenerPaginationState.page = 1;
      const filters = getRowFilters();
      const rows = (screenerPayload.items || []).filter((row) => {
        if (filters.symbol_search) {
          const haystack = `${row.symbol || ''} ${row.exchange || ''} ${row.industry || ''}`.toUpperCase();
          if (!haystack.includes(filters.symbol_search)) return false;
        }
        if (filters.exchange && row.exchange !== filters.exchange) return false;
        if (filters.industry && row.industry !== filters.industry) return false;
        if (filters.target_status && row.target_status !== filters.target_status) return false;
        if (filters.direction && row.direction_bias !== filters.direction) return false;
        if (!inRange(row.display_price ?? row.price, filters.price_min, filters.price_max)) {
          if (Number.isFinite(filters.price_min) || Number.isFinite(filters.price_max)) return false;
        }
        if (!inRange(row.display_day_change_pct ?? row.day_change_pct, filters.day_change_min, filters.day_change_max)) {
          if (Number.isFinite(filters.day_change_min) || Number.isFinite(filters.day_change_max)) return false;
        }
        if (Number.isFinite(filters.atr_pct_min) && Number(row.atr_pct || 0) < filters.atr_pct_min) return false;
        if (Number.isFinite(filters.avg_volume_min) && Number(row.avg_10d_volume || 0) < filters.avg_volume_min) return false;
        if (Number.isFinite(filters.premarket_volume_min) && Number(row.premarket_volume || 0) < filters.premarket_volume_min) return false;
        if (Number.isFinite(filters.target_score_min) && Number(row.target_score || 0) < filters.target_score_min) return false;
        if (Number.isFinite(filters.freshness_max)) {
          if (!Number.isFinite(Number(row.freshness_min)) || Number(row.freshness_min) > filters.freshness_max) return false;
        }
        if (filters.operable_only && !row.is_operable) return false;
        return true;
      });

      filteredRows = sortRows(rows, filters.sort_by);
      renderTable();
      updateHero();
    }

    function updateSelectionInfo() {
      if (activeTab === 'screener') updateHero();
    }

    window.toggleSelection = function(symbol, checked) {
      const normalized = String(symbol || '').trim().toUpperCase();
      if (!normalized) return;
      if (checked) selectedSymbols.add(normalized);
      else selectedSymbols.delete(normalized);
      updateSelectionInfo();
      renderTable();
    };

    window.selectVisibleRows = function() {
      filteredRows.forEach((row) => selectedSymbols.add(String(row.symbol || '').trim().toUpperCase()));
      renderTable();
      updateSelectionInfo();
      showToast(`已选择 ${filteredRows.length} 个过滤结果标的`);
    };

    window.clearSelection = function() {
      selectedSymbols.clear();
      renderTable();
      updateSelectionInfo();
      showToast('选择已清空');
    };

    window.copyVisibleSymbols = async function() {
      const text = filteredRows.map((row) => row.symbol).filter(Boolean).join(',');
      if (!text) {
        showToast('当前没有可复制的标的');
        return;
      }
      try {
        await navigator.clipboard.writeText(text);
        showToast(`已复制 ${filteredRows.length} 个过滤结果 symbols`);
      } catch (_) {
        showToast('复制失败，请检查浏览器权限');
      }
    };

    window.pushSelectedTargets = async function() {
      const marketDate = document.getElementById('marketDate').value || screenerPayload.market_date || '';
      const items = (screenerPayload.items || []).filter((row) => selectedSymbols.has(String(row.symbol || '').trim().toUpperCase()));
      if (!items.length) {
        showToast('请先选择要写入的标的');
        return;
      }
      try {
        showLoading('正在写入今日 targets...');
        const payload = await requestJson('/api/custom/ibkr/screener/targets', {
          method: 'POST',
          body: {
            environment: currentEnvironment,
            market_date: marketDate,
            items
          }
        });
        showToast(`写入完成: created ${payload.created || 0}, updated ${payload.updated || 0}, skipped ${payload.skipped || 0}`);
        await loadScreener(false, { force: true });
      } catch (error) {
        showToast(`写入失败: ${error.message || error}`);
      } finally {
        hideLoading();
      }
    };

    function summarizeManualDailyScanResult(payload) {
      const schedulerResult = payload?.scheduler_result && typeof payload.scheduler_result === 'object'
        ? payload.scheduler_result
        : {};
      const scanResult = payload?.scan_result && typeof payload.scan_result === 'object'
        ? payload.scan_result
        : (schedulerResult.payload && typeof schedulerResult.payload === 'object' ? schedulerResult.payload : {});
      if (schedulerResult.skipped) {
        return `补跑跳过: ${schedulerResult.reason || payload?.error || 'scheduler skipped'}`;
      }
      const active = Number(scanResult.active || 0) || 0;
      const candidates = Number(scanResult.candidates || 0) || 0;
      const removed = Number(scanResult.removed || 0) || 0;
      const errors = Number(scanResult.errors || 0) || 0;
      return `补跑完成: active ${active}, candidate ${candidates}, removed ${removed}, errors ${errors}`;
    }

    function didManualDailyScanSucceed(payload) {
      const schedulerResult = payload?.scheduler_result && typeof payload.scheduler_result === 'object'
        ? payload.scheduler_result
        : {};
      const scanResult = payload?.scan_result && typeof payload.scan_result === 'object'
        ? payload.scan_result
        : (schedulerResult.payload && typeof schedulerResult.payload === 'object' ? schedulerResult.payload : {});
      if (schedulerResult.skipped) return false;
      const errors = Number(scanResult.errors || 0) || 0;
      return !errors;
    }

    function confirmManualDailyScan() {
      return new Promise((resolve) => {
        const existing = document.getElementById('manualDailyScanConfirmOverlay');
        if (existing) {
          resolve(false);
          return;
        }

        const overlay = document.createElement('div');
        overlay.id = 'manualDailyScanConfirmOverlay';
        overlay.className = 'manual-daily-scan-confirm-overlay';
        overlay.setAttribute('role', 'presentation');
        overlay.innerHTML = `
          <div class="manual-daily-scan-confirm" role="dialog" aria-modal="true" aria-labelledby="manualDailyScanConfirmTitle" aria-describedby="manualDailyScanConfirmCopy">
            <div class="manual-daily-scan-confirm-kicker">Manual Daily Scan</div>
            <div id="manualDailyScanConfirmTitle" class="manual-daily-scan-confirm-title">确认补跑今日日筛</div>
            <div id="manualDailyScanConfirmCopy" class="manual-daily-scan-confirm-copy">
              <div>这会重新计算今日 <code>candidate / active</code>。</div>
              <div>手动加入的标的会保留。</div>
              <div>不会直接下单，也不会触发信号确认。</div>
            </div>
            <div class="manual-daily-scan-confirm-actions">
              <button type="button" class="mini-btn" data-confirm-action="cancel">取消</button>
              <button type="button" class="mini-btn scan-rerun-btn" data-confirm-action="confirm">确认补跑</button>
            </div>
          </div>
        `;

        const cleanup = (value) => {
          document.removeEventListener('keydown', handleKeydown);
          overlay.remove();
          resolve(value);
        };
        const handleKeydown = (event) => {
          if (event.key === 'Escape') cleanup(false);
        };

        overlay.addEventListener('click', (event) => {
          if (event.target === overlay) cleanup(false);
        });
        overlay.querySelector('[data-confirm-action="cancel"]')?.addEventListener('click', () => cleanup(false));
        overlay.querySelector('[data-confirm-action="confirm"]')?.addEventListener('click', () => cleanup(true));
        document.addEventListener('keydown', handleKeydown);
        document.body.appendChild(overlay);
        window.requestAnimationFrame(() => {
          overlay.classList.add('show');
          overlay.querySelector('[data-confirm-action="confirm"]')?.focus();
        });
      });
    }

    window.rerunTodayDailyScan = async function() {
      if (manualDailyScanState.running) return;
      if (!isSelectedDateToday()) {
        manualDailyScanState.status = 'error';
        manualDailyScanState.message = `补跑失败: 只支持当前美东日期 ${getUsDate()}`;
        showToast(`只支持补跑当前美东日期 ${getUsDate()}`);
        syncManualDailyScanButton();
        return;
      }
      const confirmed = await confirmManualDailyScan();
      if (!confirmed) return;

      manualDailyScanState.running = true;
      manualDailyScanState.status = 'running';
      manualDailyScanState.message = '正在补跑并刷新目标池...';
      syncManualDailyScanButton();
      try {
        showLoading('正在补跑今日日筛...');
        const payload = await requestJson('/api/custom/system/scheduler/jobs/run', {
          method: 'POST',
          body: {
            environment: currentEnvironment,
            job_id: 'ibkr_scan_runtime',
            trigger_source: 'console_manual_daily_scan'
          }
        });
        manualDailyScanState.lastResult = payload;
        const resultMessage = summarizeManualDailyScanResult(payload);
        const succeeded = didManualDailyScanSucceed(payload);
        const resultDetail = resultMessage.replace(/^补跑(?:完成|跳过): /, '');
        manualDailyScanState.status = succeeded ? 'success' : 'error';
        manualDailyScanState.message = succeeded
          ? `补跑成功: ${resultDetail}`
          : `补跑未成功: ${resultDetail}`;
        showToast(manualDailyScanState.message);
        currentTargetState.page = 1;
        screenerLoadKey = '';
        windowProgressState.loadedKey = '';
        await loadScreener(false, { force: true });
        await loadDailyTargets(false);
      } catch (error) {
        const errorMessage = `补跑失败: ${error.message || error}`;
        manualDailyScanState.status = 'error';
        manualDailyScanState.message = errorMessage;
        showToast(errorMessage);
      } finally {
        manualDailyScanState.running = false;
        syncManualDailyScanButton();
        hideLoading();
      }
    };

    async function loadScreener(showToastOnSuccess = false, { loadCurrentTargetsAfter = true, force = false } = {}) {
      if (!initAuth()) return;
      const marketDate = document.getElementById('marketDate').value || '';
      const loadKey = getScreenerLoadKey(marketDate);
      const nextParams = { market_date: marketDate };
      if (activeTab === 'screener') {
        nextParams.view = activeScreenerView;
      } else if (activeTab === 'watchlist') {
        nextParams.tab = 'watchlist';
      } else if (activeTab === 'monitor') {
        nextParams.tab = 'monitor';
      } else if (activeTab === 'targets') {
        const targetDate = getDailyTargetDate() || marketDate;
        nextParams.tab = 'targets';
        nextParams.date = targetDate;
        nextParams.market_date = targetDate;
      }
      const nextUrl = buildPageUrl('/ibkr_screener.html', nextParams, { environment: currentEnvironment });
      if (`${location.pathname}${location.search}` !== nextUrl) {
        window.history.replaceState({}, '', nextUrl);
      }

      if (!force && screenerLoadKey === loadKey) {
        populateSelect('exchangeFilter', screenerPayload.filters && screenerPayload.filters.exchanges);
        populateSelect('industryFilter', screenerPayload.filters && screenerPayload.filters.industries);
        populateSelect('targetStatusFilter', screenerPayload.filters && screenerPayload.filters.target_statuses);
        populateSelect('directionFilter', screenerPayload.filters && screenerPayload.filters.direction_biases);
        applyFilters();
        renderRulesBoard();
        if (loadCurrentTargetsAfter) {
          await loadTodayTargets(false);
        }
        setPageRefreshTime();
        if (activeTab === 'screener') updateHero();
        if (showToastOnSuccess) showToast('筛选器已刷新');
        return screenerPayload;
      }

      try {
        showLoading('正在聚合筛选器数据...');
        const rulesPromise = loadRulesSummary();
        const payload = await requestJson(`/api/custom/ibkr/screener${buildQuery({
          environment: currentEnvironment,
          market_date: marketDate
        })}`);
        const items = Array.isArray(payload?.items) ? payload.items : [];
        screenerPayload = {
          ...(payload || { items: [], summary: {}, filters: {} }),
          items: items.map((row) => mergeScreenerRowWithRealtimeQuote(row)),
        };
        screenerLoadKey = loadKey;
        populateSelect('exchangeFilter', payload.filters && payload.filters.exchanges);
        populateSelect('industryFilter', payload.filters && payload.filters.industries);
        populateSelect('targetStatusFilter', payload.filters && payload.filters.target_statuses);
        populateSelect('directionFilter', payload.filters && payload.filters.direction_biases);
        applyFilters();
        await rulesPromise;
        if (loadCurrentTargetsAfter) {
          await loadTodayTargets(false);
        }
        renderRulesBoard();
        setPageRefreshTime();
        if (activeTab === 'screener') updateHero();
        void refreshScreenerQuotes(items, loadKey);
        if (showToastOnSuccess) showToast('筛选器已刷新');
        return screenerPayload;
      } catch (error) {
        console.error('loadScreener failed:', error);
        screenerLoadKey = '';
        document.getElementById('refreshInfo').textContent = '加载失败';
        document.getElementById('screenerTable').innerHTML = `<tr><td colspan="9" class="empty">${escapeHtml(error.message || error)}</td></tr>`;
        renderMobileCardState('screenerCards', error.message || error);
        showToast(`加载失败: ${error.message || error}`);
      } finally {
        hideLoading();
      }
    }

    function bindFilterEvents() {
      const ids = [
        'symbolSearch',
        'exchangeFilter',
        'industryFilter',
        'targetStatusFilter',
        'directionFilter',
        'priceMin',
        'priceMax',
        'dayChangeMin',
        'dayChangeMax',
        'atrPctMin',
        'avgVolumeMin',
        'premarketVolumeMin',
        'targetScoreMin',
        'freshnessMax',
        'sortBy',
        'screenerPageSize',
        'operableOnly'
      ];
      ids.forEach((id) => {
        const element = document.getElementById(id);
        if (!element) return;
        const eventName = element.tagName === 'INPUT' && element.type === 'text' ? 'input' : 'change';
        element.addEventListener(eventName, applyFilters);
      });
      document.getElementById('marketDate').addEventListener('change', async () => {
        const nextDate = document.getElementById('marketDate').value || getUsDate();
        currentTargetState.page = 1;
        dailyTargetsState.selectedDate = nextDate;
        screenerLoadKey = '';
        windowProgressState.loadedKey = '';
        if (document.getElementById('dailyTargetDate')) {
          document.getElementById('dailyTargetDate').value = nextDate;
        }
        syncManualDailyScanButton();
        syncUrl();
        if (activeTab === 'screener') {
          await ensureActiveScreenerDataLoaded({ force: true });
          return;
        }
        if (activeTab === 'targets') {
          await loadDailyTargets(false);
          return;
        }
        updateHero();
      });
    }

    function bindCurrentTargetFilterEvents() {
      const immediateIds = [
        'currentTechnicalStateFilter',
        'currentSignalStateFilter',
        'currentTargetStatusFilter',
        'currentDirectionBiasFilter',
        'currentTargetSortBy',
        'currentTargetPageSize',
        'currentReadyOnly',
        'currentSignaledOnly',
      ];
      immediateIds.forEach((id) => {
        const element = document.getElementById(id);
        if (!element) return;
        element.addEventListener('change', () => applyCurrentTargetFilters({ resetPage: true }));
      });
      const searchInput = document.getElementById('currentTargetSearch');
      if (searchInput) {
        searchInput.addEventListener('input', scheduleCurrentTargetReload);
        searchInput.addEventListener('keydown', (event) => {
          if (event.key !== 'Enter') return;
          window.clearTimeout(currentTargetState.searchDebounceId);
          applyCurrentTargetFilters({ resetPage: true });
        });
      }
      document.getElementById('openUniverseViewBtn')?.addEventListener('click', async () => {
        await activateScreenerView('universe');
      });
      document.getElementById('refreshWindowProgressBtn')?.addEventListener('click', () => {
        loadWindowProgress(true, { force: true });
      });
      document.getElementById('toggleCurrentAdvancedFiltersBtn')?.addEventListener('click', () => {
        currentFiltersExpanded = !currentFiltersExpanded;
        syncCurrentAdvancedFilters();
      });
      syncCurrentAdvancedFilters();
      syncManualDailyScanButton();
    }

    function parseSymbolList(rawValue) {
      const seen = new Set();
      return String(rawValue || '')
        .split(/[\s,;，；]+/)
        .map((value) => String(value || '').trim().toUpperCase())
        .filter((value) => {
          if (!value || seen.has(value)) return false;
          seen.add(value);
          return true;
        });
    }

    function findExistingBySymbol(symbol) {
      const normalized = String(symbol || '').trim().toUpperCase();
      return watchlistState.items.filter((item) => String(item.symbol || '').trim().toUpperCase() === normalized);
    }

    function formatExistingWatchlistScopes(items) {
      const seen = new Set();
      return (Array.isArray(items) ? items : [])
        .map((item) => `${formatRecordEnvironment(item.environment)} ${formatWatchlistRole(item.symbol_role)} ${formatWatchlistMember(item)}`)
        .filter((label) => {
          if (!label || seen.has(label)) return false;
          seen.add(label);
          return true;
        })
        .join(' / ');
    }

    function pickBestCandidate(items, symbol) {
      const normalized = String(symbol || '').trim().toUpperCase();
      const list = Array.isArray(items) ? items.slice() : [];
      const exchangeRank = (exchange) => {
        const normalizedExchange = String(exchange || '').trim().toUpperCase();
        if (normalizedExchange === 'NASDAQ' || normalizedExchange === 'NYSE') return 2;
        if (['AMEX', 'ARCA', 'BATS', 'SMART'].includes(normalizedExchange)) return 1;
        return 0;
      };
      list.sort((left, right) => {
        const leftExact = String(left.symbol || '').trim().toUpperCase() === normalized ? 1 : 0;
        const rightExact = String(right.symbol || '').trim().toUpperCase() === normalized ? 1 : 0;
        if (rightExact !== leftExact) return rightExact - leftExact;
        const leftUs = left.is_us ? 1 : 0;
        const rightUs = right.is_us ? 1 : 0;
        if (rightUs !== leftUs) return rightUs - leftUs;
        const exchangeDiff = exchangeRank(right.exchange) - exchangeRank(left.exchange);
        if (exchangeDiff !== 0) return exchangeDiff;
        return Number(right.score || 0) - Number(left.score || 0);
      });
      return list[0] || null;
    }

    async function searchBestContract(symbol) {
      const payload = await requestJson(`/api/custom/ibkr/contracts/search${buildQuery({
        environment: currentEnvironment,
        q: symbol,
        limit: 6
      })}`);
      return pickBestCandidate(payload.items, symbol);
    }

    function buildWatchlistBody(item, scope, noteOverride) {
      const secTypes = Array.isArray(item.sec_types) ? item.sec_types : [];
      return {
        environment: currentEnvironment,
        source: 'manual_page_add',
        scope,
        manual_member: true,
        symbol_role: getWatchlistRoleForTab(),
        symbol: item.symbol,
        exchange: item.exchange || '',
        industry: item.industry || item.asset_class || (secTypes.length ? secTypes.join('/') : ''),
        asset_class: item.asset_class || '',
        sec_types: secTypes,
        description: item.description || '',
        note: noteOverride != null ? String(noteOverride || '').trim() : String(item.note || '').trim()
      };
    }

    function findExistingDailyTarget(symbol) {
      const normalized = String(symbol || '').trim().toUpperCase();
      return dailyTargetsState.items.find((item) => String(item.symbol || '').trim().toUpperCase() === normalized) || null;
    }

    function buildDailyTargetBody(item, draft) {
      return {
        environment: currentEnvironment,
        source: 'manual_page_add',
        symbol: item.symbol,
        exchange: item.exchange || '',
        date: draft.date,
        direction_bias: draft.direction_bias,
        score: draft.score,
        scan_reason: draft.scan_reason,
        status: draft.status,
        extra: {
          source: 'screener_targets_tab',
          conid: item.conid || 0,
          company_name: item.company_name || '',
          description: item.description || '',
          asset_class: item.asset_class || '',
          sec_types: Array.isArray(item.sec_types) ? item.sec_types : [],
          is_us: Boolean(item.is_us)
        }
      };
    }

    function getDailyTargetDraft() {
      const scoreValue = Number(document.getElementById('dailyTargetScoreInput').value || 0);
      return {
        date: getDailyTargetDate(),
        status: document.getElementById('dailyTargetStatusInput').value || 'candidate',
        direction_bias: document.getElementById('dailyTargetDirectionInput').value || 'neutral',
        score: Number.isFinite(scoreValue) ? scoreValue : 0,
        scan_reason: String(document.getElementById('dailyTargetReasonInput').value || '').trim()
      };
    }

    function renderDailyTargetSearchResults() {
      const mount = document.getElementById('dailyTargetSearchResults');
      const draft = getDailyTargetDraft();
      const manualDateAllowed = isManualTargetDateAllowed(draft.date);

      if (!dailyTargetsState.searchResults.length) {
        mount.innerHTML = '<div class="empty-state">暂无搜索结果。</div>';
        return;
      }

      mount.innerHTML = dailyTargetsState.searchResults.map((item, index) => {
        const secTypes = Array.isArray(item.sec_types) ? item.sec_types : [];
        const description = item.description || item.company_name || '无描述';
        const existing = findExistingDailyTarget(item.symbol);
        return `
          <article class="result-card">
            <div class="card-top">
              <span class="symbol-chip">${escapeHtml(item.symbol || '--')}</span>
              <span class="pool-pill">${escapeHtml(item.exchange || 'SMART')}</span>
              <span class="status-chip ${escapeHtml(draft.status)}">${escapeHtml(draft.status)}</span>
              ${existing ? `<span class="pool-pill exists">已存在 ${escapeHtml(existing.status || 'candidate')}</span>` : ''}
            </div>
            <div>
              <div class="card-title">${escapeHtml(item.company_name || item.symbol || '--')}</div>
              <div class="card-copy">${escapeHtml(description)}</div>
            </div>
            <div class="pill-row">
              <span class="status-chip">date ${escapeHtml(draft.date)}</span>
              <span class="status-chip ${escapeHtml(draft.direction_bias)}">${escapeHtml(draft.direction_bias)}</span>
              <span class="status-chip">score ${escapeHtml(String(draft.score))}</span>
              ${(secTypes.length ? secTypes : [item.asset_class || 'UNKNOWN']).map((type) => `<span class="pool-pill">${escapeHtml(type)}</span>`).join('')}
            </div>
            <div class="card-bottom">
              <button class="btn primary" type="button" onclick="addDailyTargetCandidate(${index})" ${manualDateAllowed ? '' : 'disabled'}>${manualDateAllowed ? '加入目标池' : '仅支持当前交易日'}</button>
              <a class="mini-link" href="${buildPageUrl('/ibkr_chart.html', { symbol: item.symbol || '', interval: '5m' }, { environment: currentEnvironment })}">查看图表</a>
            </div>
          </article>
        `;
      }).join('');
    }

    function renderDailyTargetRows() {
      const items = getFilteredDailyTargetItems();
      const table = document.getElementById('dailyTargetsTable');
      if (!items.length) {
        table.innerHTML = '<tr><td colspan="9" class="empty-state">当前日期没有目标池记录。</td></tr>';
        renderDailyTargetCards([]);
        if (activeTab === 'targets') updateHero();
        return;
      }

      table.innerHTML = items.map((item) => `
        <tr>
          <td>
            <div class="meta-stack">
              <div class="table-symbol">${escapeHtml(item.symbol || '--')}</div>
              <small>${escapeHtml(item.id || '')}</small>
            </div>
          </td>
          <td>${escapeHtml(item.exchange || '--')}</td>
          <td>${escapeHtml(item.date || '--')}</td>
          <td><span class="status-chip ${escapeHtml(item.direction_bias || 'neutral')}">${escapeHtml(item.direction_bias || 'neutral')}</span></td>
          <td>${escapeHtml(Number(item.score || 0).toFixed(1))}</td>
          <td><span class="status-chip ${escapeHtml(item.status || 'candidate')}">${escapeHtml(item.status || 'candidate')}</span></td>
          <td>${escapeHtml(item.scan_reason || '--')}</td>
          <td>
            <div class="meta-stack">
              <span>${escapeHtml(item.us_time || '--')}</span>
              <small>${item.updated ? escapeHtml(formatBeijingTime(item.updated, 'short')) : '--'}</small>
            </div>
          </td>
          <td>
            <div class="row-actions">
              <a class="mini-link" href="${buildPageUrl('/ibkr_chart.html', { symbol: item.symbol || '', interval: '5m' }, { environment: currentEnvironment })}">Chart</a>
              <button class="mini-btn" type="button" onclick="editDailyTargetItem('${escapeHtml(item.id || '')}')">编辑</button>
              <button class="mini-btn danger" type="button" onclick="removeDailyTargetItem('${escapeHtml(item.id || '')}', '${escapeHtml(item.symbol || '')}')">删除</button>
            </div>
          </td>
        </tr>
      `).join('');
      renderDailyTargetCards(items);

      if (activeTab === 'targets') updateHero();
    }

    async function searchDailyTargetContracts() {
      const keyword = String(document.getElementById('dailyTargetSearchInput').value || '').trim();
      if (!keyword) {
        showToast('请输入 ticker 或公司名');
        return;
      }

      document.getElementById('dailyTargetSearchMeta').textContent = `正在查询 ${keyword} ...`;
      document.getElementById('dailyTargetSearchResults').innerHTML = '<div class="empty-state">搜索中...</div>';
      try {
        const payload = await requestJson(`/api/custom/ibkr/contracts/search${buildQuery({
          environment: currentEnvironment,
          q: keyword,
          limit: 12
        })}`);
        dailyTargetsState.searchResults = Array.isArray(payload.items) ? payload.items : [];
        document.getElementById('dailyTargetSearchMeta').textContent = `IBKR 返回 ${dailyTargetsState.searchResults.length} 个候选`;
        renderDailyTargetSearchResults();
      } catch (error) {
        dailyTargetsState.searchResults = [];
        document.getElementById('dailyTargetSearchMeta').textContent = `搜索失败: ${error.message || error}`;
        document.getElementById('dailyTargetSearchResults').innerHTML = `<div class="empty-state">${escapeHtml(error.message || error)}</div>`;
      }
    }

    async function loadDailyTargets(showToastOnSuccess = false, { refreshCurrentTargets = false } = {}) {
      dailyTargetsState.selectedDate = getDailyTargetDate();
      document.getElementById('dailyTargetDate').value = dailyTargetsState.selectedDate;

      try {
        const response = await apiFetch('ibkr_targets', {
          filter: `environment = "${escapeFilterValue(currentEnvironment)}" && date = "${escapeFilterValue(dailyTargetsState.selectedDate)}"`,
          sort: '-updated',
          perPage: 200
        });
        dailyTargetsState.items = Array.isArray(response.items) ? response.items : [];
        dailyTargetsState.loaded = true;
        dailyTargetsState.loadedDate = dailyTargetsState.selectedDate;
        dailyTargetsState.lastRefresh = 'ibkr_targets 已加载';
        setPageRefreshTime();
        document.getElementById('dailyTargetListMeta').textContent = `${getEnvironmentLabel(currentEnvironment)} / ${dailyTargetsState.selectedDate} / ${dailyTargetsState.items.length} 条`;
        renderDailyTargetRows();
        if (refreshCurrentTargets) {
          await loadTodayTargets(false);
        }
        syncUrl();
        if (showToastOnSuccess) showToast('ibkr_targets 已刷新');
      } catch (error) {
        dailyTargetsState.items = [];
        dailyTargetsState.loaded = false;
        dailyTargetsState.loadedDate = '';
        dailyTargetsState.lastRefresh = 'ibkr_targets 加载失败';
        document.getElementById('dailyTargetsTable').innerHTML = `<tr><td colspan="9" class="empty-state">${escapeHtml(error.message || error)}</td></tr>`;
        renderMobileCardState('dailyTargetsCards', error.message || error);
        document.getElementById('dailyTargetListMeta').textContent = `加载失败: ${error.message || error}`;
        if (activeTab === 'targets') updateHero();
      }
    }

    async function addDailyTargetCandidate(index) {
      const item = dailyTargetsState.searchResults[index];
      if (!item) return;
      if (!ensureManualTargetDateAllowed()) return;

      const draft = getDailyTargetDraft();
      try {
        const payload = await requestJson('/api/custom/ibkr/targets/upsert', {
          method: 'POST',
          body: buildDailyTargetBody(item, draft)
        });
        showToast(`${item.symbol} 已加入 ${draft.date} 目标池`);
        const warning = getRuntimeWarning(payload) || getWatchlistSyncWarning(payload);
        if (warning) showToast(`预热提示: ${warning}`);
        await loadDailyTargets(false);
      } catch (error) {
        showToast(`加入失败: ${error.message || error}`);
      }
    }

    async function editDailyTargetItem(recordId) {
      const item = dailyTargetsState.items.find((row) => String(row.id || '') === String(recordId || ''));
      if (!item) {
        showToast('记录不存在');
        return;
      }

      const nextStatus = window.prompt('修改 status(candidate/active/removed)', item.status || 'candidate');
      if (nextStatus === null) return;
      if (!['candidate', 'active', 'removed'].includes(String(nextStatus || '').trim())) {
        showToast('status 无效');
        return;
      }
      const nextDirection = window.prompt('修改 direction_bias(long/short/neutral)', item.direction_bias || 'neutral');
      if (nextDirection === null) return;
      if (!['long', 'short', 'neutral'].includes(String(nextDirection || '').trim())) {
        showToast('direction_bias 无效');
        return;
      }
      const nextScoreText = window.prompt('修改 score', String(item.score ?? 0));
      if (nextScoreText === null) return;
      const nextScore = Number(nextScoreText);
      if (!Number.isFinite(nextScore)) {
        showToast('score 无效');
        return;
      }
      const nextReason = window.prompt('修改 scan_reason', item.scan_reason || '');
      if (nextReason === null) return;

      try {
        const payload = await requestJson('/api/custom/ibkr/targets/upsert', {
          method: 'POST',
          body: {
            environment: currentEnvironment,
            source: 'manual_page_edit',
            symbol: item.symbol,
            exchange: item.exchange || '',
            date: item.date || dailyTargetsState.selectedDate,
            direction_bias: String(nextDirection || '').trim(),
            score: nextScore,
            scan_reason: String(nextReason || '').trim(),
            status: String(nextStatus || '').trim(),
            extra: item.extra && typeof item.extra === 'object' ? item.extra : {}
          }
        });
        showToast(`${item.symbol} 已更新`);
        const warning = getRuntimeWarning(payload) || getWatchlistSyncWarning(payload);
        if (warning) showToast(`联动提示: ${warning}`);
        await loadDailyTargets(false);
      } catch (error) {
        showToast(`更新失败: ${error.message || error}`);
      }
    }

    async function removeDailyTargetItem(recordId, symbol) {
      if (!recordId) return;
      if (!window.confirm(`确认删除 ${symbol} 的目标池记录？`)) return;
      try {
        const payload = await requestJson('/api/custom/ibkr/targets/remove', {
          method: 'POST',
          body: {
            environment: currentEnvironment,
            source: 'manual_page_remove',
            record_id: recordId,
            symbol
          }
        });
        showToast(`${symbol} 已删除`);
        const warning = getRuntimeWarning(payload) || getWatchlistSyncWarning(payload);
        if (warning) showToast(`清理提示: ${warning}`);
        await loadDailyTargets(false);
        await loadWatchlist(false);
      } catch (error) {
        showToast(`删除失败: ${error.message || error}`);
      }
    }

    async function batchAddDailyTargetSymbols() {
      const symbols = parseSymbolList(document.getElementById('dailyTargetBatchSymbolsInput').value);
      const draft = getDailyTargetDraft();
      if (!ensureManualTargetDateAllowed()) return;
      if (!symbols.length) {
        showToast('先输入要批量加入的 symbols');
        return;
      }

      let success = 0;
      let failed = 0;
      for (let index = 0; index < symbols.length; index += 1) {
        const symbol = symbols[index];
        document.getElementById('dailyTargetBatchMeta').textContent = `处理中 ${index + 1}/${symbols.length}: ${symbol}`;
        try {
          const candidate = await searchBestContract(symbol);
          if (!candidate) throw new Error('IBKR 未返回候选');
          await requestJson('/api/custom/ibkr/targets/upsert', {
            method: 'POST',
            body: buildDailyTargetBody(candidate, draft)
          });
          success += 1;
        } catch (_) {
          failed += 1;
        }
      }

      document.getElementById('dailyTargetBatchMeta').textContent = `批量导入完成: 成功 ${success} / 失败 ${failed}`;
      showToast(`批量导入完成: ${success}/${symbols.length}`);
      await loadDailyTargets(false);
    }

    async function batchDeleteDailyTargetSymbols() {
      const symbols = parseSymbolList(document.getElementById('dailyTargetBatchDeleteInput').value);
      if (!symbols.length) {
        showToast('先输入要删除的 symbols');
        return;
      }

      const targets = dailyTargetsState.items.filter((item) => symbols.includes(String(item.symbol || '').trim().toUpperCase()));
      if (!targets.length) {
        showToast('当前日期下没有匹配记录');
        return;
      }
      if (!window.confirm(`确认删除 ${targets.length} 条目标池记录？`)) return;

      let deleted = 0;
      for (const item of targets) {
        try {
          await requestJson('/api/custom/ibkr/targets/remove', {
            method: 'POST',
            body: {
              environment: currentEnvironment,
              source: 'manual_page_remove',
              record_id: item.id,
              symbol: item.symbol || ''
            }
          });
          deleted += 1;
        } catch (_) {
          // ignore batch delete failures and continue
        }
      }

      document.getElementById('dailyTargetDeleteMeta').textContent = `批量删除完成: 删除 ${deleted} / 匹配 ${targets.length}`;
      showToast(`批量删除完成: ${deleted}/${targets.length}`);
      await loadDailyTargets(false);
      await loadWatchlist(false);
    }

    function attachDailyTargetEvents() {
      document.getElementById('dailyTargetSearchBtn').addEventListener('click', searchDailyTargetContracts);
      document.getElementById('dailyTargetSearchInput').addEventListener('keydown', (event) => {
        if (event.key === 'Enter') searchDailyTargetContracts();
      });
      document.getElementById('dailyTargetBatchAddBtn').addEventListener('click', batchAddDailyTargetSymbols);
      document.getElementById('dailyTargetBatchClearBtn').addEventListener('click', () => {
        document.getElementById('dailyTargetBatchSymbolsInput').value = '';
        document.getElementById('dailyTargetBatchMeta').textContent = '批量导入只支持当前交易日的应急手动加入，会逐个调用 IBKR 搜索并触发运行时预热。';
      });
      document.getElementById('dailyTargetBatchDeleteBtn').addEventListener('click', batchDeleteDailyTargetSymbols);
      document.getElementById('dailyTargetRefreshBtn').addEventListener('click', () => loadDailyTargets(true));
      document.getElementById('dailyTargetTableSearchInput').addEventListener('input', renderDailyTargetRows);
      document.getElementById('dailyTargetDate').addEventListener('change', () => {
        const nextDate = document.getElementById('dailyTargetDate').value || getUsDate();
        dailyTargetsState.selectedDate = nextDate;
        document.getElementById('marketDate').value = nextDate;
        renderDailyTargetSearchResults();
        loadDailyTargets(false);
      });
      ['dailyTargetStatusInput', 'dailyTargetDirectionInput', 'dailyTargetScoreInput', 'dailyTargetReasonInput'].forEach((id) => {
        document.getElementById(id).addEventListener('input', renderDailyTargetSearchResults);
        document.getElementById(id).addEventListener('change', renderDailyTargetSearchResults);
      });
    }

    function buildWatchlistFilter() {
      const scope = document.getElementById('scopeFilter').value || 'all';
      const current = escapeFilterValue(currentEnvironment);
      const activeRole = getWatchlistRoleForTab();
      const roleFilter = activeRole === 'trade'
        ? '(symbol_role = "trade" || symbol_role = "")'
        : 'symbol_role = "market_monitor"';
      if (scope === 'global') return `(environment = "global" && ${roleFilter})`;
      if (scope === 'current') {
        return currentEnvironment === 'live'
          ? `((environment = "live" || environment = "") && ${roleFilter})`
          : `(environment = "${current}" && ${roleFilter})`;
      }
      return currentEnvironment === 'live'
        ? `((environment = "global" || environment = "live" || environment = "") && ${roleFilter})`
        : `((environment = "global" || environment = "${current}") && ${roleFilter})`;
    }

    function syncWatchlistScopeOptions() {
      const scopeSelect = document.getElementById('targetScope');
      scopeSelect.innerHTML = `
        <option value="${escapeHtml(currentEnvironment)}">${escapeHtml(getEnvironmentLabel(currentEnvironment))}</option>
        <option value="global">GLOBAL</option>
      `;
    }

    function renderSearchResults() {
      const mount = document.getElementById('searchResults');
      const scope = document.getElementById('targetScope').value || currentEnvironment;
      const scopeLabel = scope === 'global' ? 'GLOBAL' : getEnvironmentLabel(scope);
      const roleLabel = getWatchlistRoleLabel();

      if (!watchlistState.searchResults.length) {
        mount.innerHTML = '<div class="empty-state">暂无搜索结果。</div>';
        return;
      }

      mount.innerHTML = watchlistState.searchResults.map((item, index) => {
        const secTypes = Array.isArray(item.sec_types) ? item.sec_types : [];
        const description = item.description || item.company_name || '无描述';
        const existing = findExistingBySymbol(item.symbol);
        const existingLabel = formatExistingWatchlistScopes(existing);
        return `
          <article class="result-card">
            <div class="card-top">
              <span class="symbol-chip">${escapeHtml(item.symbol || '--')}</span>
              <span class="pool-pill">${escapeHtml(item.exchange || 'SMART')}</span>
              <span class="pool-pill ${item.is_us ? '' : 'neutral'}">${item.is_us ? 'US' : 'NON-US'}</span>
              ${existing.length ? `<span class="pool-pill exists">已存在 ${escapeHtml(existingLabel)}</span>` : ''}
            </div>
            <div>
              <div class="card-title">${escapeHtml(item.company_name || item.symbol || '--')}</div>
              <div class="card-copy">${escapeHtml(description)}</div>
            </div>
            <div class="pill-row">
              <span class="status-chip">conid ${escapeHtml(item.conid || '--')}</span>
              <span class="status-chip">score ${escapeHtml(item.score || 0)}</span>
              ${(secTypes.length ? secTypes : [item.asset_class || 'UNKNOWN']).map((type) => `<span class="pool-pill">${escapeHtml(type)}</span>`).join('')}
            </div>
            <div class="card-bottom">
              <button class="btn primary" type="button" onclick="addCandidate(${index})">加入 ${escapeHtml(scopeLabel)} ${escapeHtml(roleLabel)}</button>
              <a class="mini-link" href="${buildPageUrl('/ibkr_chart.html', { symbol: item.symbol || '', interval: '5m' }, { environment: currentEnvironment })}">查看图表</a>
            </div>
          </article>
        `;
      }).join('');
    }

    function getWatchlistPageSize() {
      const rawValue = Number(document.getElementById('watchlistPageSize')?.value || watchlistPaginationState.perPage || 10);
      if (!Number.isFinite(rawValue) || rawValue <= 0) return 10;
      return Math.max(1, Math.min(100, Math.trunc(rawValue)));
    }

    function getWatchlistPageButtons(page, totalPages) {
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

    function getWatchlistPageItems(items) {
      const rows = Array.isArray(items) ? items : [];
      watchlistPaginationState.perPage = getWatchlistPageSize();
      const totalPages = Math.max(1, Math.ceil(rows.length / watchlistPaginationState.perPage));
      watchlistPaginationState.page = Math.max(1, Math.min(totalPages, Number(watchlistPaginationState.page) || 1));
      const startIndex = (watchlistPaginationState.page - 1) * watchlistPaginationState.perPage;
      return rows.slice(startIndex, startIndex + watchlistPaginationState.perPage);
    }

    function renderWatchlistPagination(items, pageItems) {
      const rows = Array.isArray(items) ? items : [];
      watchlistPaginationState.perPage = getWatchlistPageSize();
      const totalRows = rows.length;
      const totalPages = Math.max(1, Math.ceil(totalRows / watchlistPaginationState.perPage));
      const page = Math.max(1, Math.min(totalPages, Number(watchlistPaginationState.page) || 1));
      watchlistPaginationState.page = page;
      const returnedCount = Array.isArray(pageItems) ? pageItems.length : 0;
      const shouldShowPagination = totalRows > watchlistPaginationState.perPage;
      const controls = [];

      controls.push(`<button class="mini-btn pagination-btn" type="button" onclick="setWatchlistPage(${page - 1})" ${page <= 1 ? 'disabled' : ''}>上一页</button>`);
      let lastPage = 0;
      getWatchlistPageButtons(page, totalPages).forEach((value) => {
        if (lastPage && value - lastPage > 1) {
          controls.push('<span class="pagination-ellipsis">...</span>');
        }
        controls.push(`<button class="mini-btn pagination-btn ${value === page ? 'active' : ''}" type="button" onclick="setWatchlistPage(${value})">${value}</button>`);
        lastPage = value;
      });
      controls.push(`<button class="mini-btn pagination-btn" type="button" onclick="setWatchlistPage(${page + 1})" ${page >= totalPages ? 'disabled' : ''}>下一页</button>`);

      ['watchlistPaginationTop', 'watchlistPaginationBottom'].forEach((id) => {
        const mount = document.getElementById(id);
        if (!mount) return;
        const bar = mount.closest('.pagination-bar');
        if (bar) bar.hidden = !shouldShowPagination;
        mount.innerHTML = shouldShowPagination ? controls.join('') : '';
      });

      const statusText = totalRows
        ? `第 ${page} / ${totalPages} 页 · 本页 ${returnedCount} 条 · 可见 ${totalRows} 条`
        : '第 1 / 1 页 · 当前没有结果';
      ['watchlistPaginationStatusTop', 'watchlistPaginationStatusBottom'].forEach((id) => {
        const mount = document.getElementById(id);
        if (mount) mount.textContent = shouldShowPagination ? statusText : '';
      });
    }

    window.setWatchlistPage = function setWatchlistPage(page) {
      const items = getFilteredWatchlistItems();
      const totalPages = Math.max(1, Math.ceil(items.length / getWatchlistPageSize()));
      const nextPage = Math.max(1, Math.min(totalPages, Number(page) || 1));
      if (nextPage === watchlistPaginationState.page) return;
      watchlistPaginationState.page = nextPage;
      renderWatchlistRows();
    };

    function renderWatchlistRows() {
      const items = getFilteredWatchlistItems();
      const table = document.getElementById('watchlistTable');
      if (!items.length) {
        table.innerHTML = `<tr><td colspan="9" class="empty-state">暂无 ${escapeHtml(getWatchlistRoleLabel())} 记录。</td></tr>`;
        renderWatchlistCards([]);
        renderWatchlistPagination(items, []);
      } else {
        const pageItems = getWatchlistPageItems(items);
        table.innerHTML = pageItems.map((item) => {
          const configItem = isConfigMonitorItem(item);
          return `
            <tr>
              <td>
                <div class="meta-stack">
                  <div class="table-symbol">${escapeHtml(item.symbol || '--')}</div>
                  <small>${escapeHtml(configItem ? 'ibkr_market_ws_symbols' : (item.id || ''))}</small>
                </div>
              </td>
              <td>${escapeHtml(item.exchange || '--')}</td>
              <td>${escapeHtml(item.industry || '--')}</td>
              <td><span class="env-badge ${resolveRecordEnvClass(item.environment)}">${escapeHtml(formatRecordEnvironment(item.environment))}</span></td>
              <td>${configItem ? statusChip('CONFIG', 'config') : escapeHtml(formatWatchlistRole(item.symbol_role || 'trade'))}</td>
              <td>${escapeHtml(formatWatchlistMember(item))}</td>
              <td>${escapeHtml(item.note || '--')}</td>
              <td>
                <div class="meta-stack">
                  <span>${escapeHtml(item.updated_us || item.us_time || '--')}</span>
                  <small>${item.updated ? escapeHtml(formatBeijingTime(item.updated, 'short')) : '--'}</small>
                </div>
              </td>
              <td>
                <div class="row-actions">
                  <a class="mini-link" href="${buildPageUrl('/ibkr_chart.html', { symbol: item.symbol || '', interval: '5m' }, { environment: currentEnvironment })}">Chart</a>
                  ${configItem
                    ? `<a class="mini-link" href="${getConfigPageUrl()}">改配置</a>`
                    : `<button class="mini-btn" type="button" onclick="editItem('${escapeHtml(item.id || '')}')">编辑</button>
                       <button class="mini-btn danger" type="button" onclick="removeItem('${escapeHtml(item.id || '')}', '${escapeHtml(item.symbol || '')}', '${escapeHtml(formatRecordEnvironment(item.environment))}')">删除</button>`
                  }
                </div>
              </td>
            </tr>
          `;
        }).join('');
        renderWatchlistCards(pageItems);
        renderWatchlistPagination(items, pageItems);
      }

      const configSuffix = watchlistState.configLoadError ? ` · 配置读取失败: ${watchlistState.configLoadError}` : '';
      document.getElementById('listMeta').textContent = `载入 ${watchlistState.items.length} · 可见 ${items.length} · 每页 ${getWatchlistPageSize()}${configSuffix}`;
      if (isWatchlistRoleTab()) {
        renderSearchResults();
        updateHero();
      }
    }

    async function searchContracts() {
      const keyword = String(document.getElementById('searchInput').value || '').trim();
      if (!keyword) {
        showToast('请输入 ticker 或公司名');
        return;
      }

      document.getElementById('searchMeta').textContent = `正在查询 ${keyword} ...`;
      document.getElementById('searchResults').innerHTML = '<div class="empty-state">搜索中...</div>';
      try {
        const payload = await requestJson(`/api/custom/ibkr/contracts/search${buildQuery({
          environment: currentEnvironment,
          q: keyword,
          limit: 12
        })}`);
        watchlistState.searchResults = Array.isArray(payload.items) ? payload.items : [];
        document.getElementById('searchMeta').textContent = `IBKR 返回 ${watchlistState.searchResults.length} 个候选`;
        renderSearchResults();
      } catch (error) {
        watchlistState.searchResults = [];
        document.getElementById('searchMeta').textContent = `搜索失败: ${error.message || error}`;
        document.getElementById('searchResults').innerHTML = `<div class="empty-state">${escapeHtml(error.message || error)}</div>`;
      }
    }

    async function loadWatchlist(showToastOnSuccess = false) {
      try {
        const response = await apiFetch('watchlist', {
          filter: buildWatchlistFilter(),
          sort: '-updated',
          perPage: 200
        });
        const collectionItems = Array.isArray(response.items) ? response.items : [];
        const configItems = getWatchlistRoleForTab() === 'market_monitor'
          ? await loadConfiguredMarketMonitorItems(collectionItems)
          : [];
        watchlistState.items = [...collectionItems, ...configItems];
        watchlistState.loaded = true;
        watchlistState.loadedRole = getWatchlistRoleForTab();
        watchlistState.lastRefresh = `${getWatchlistRoleLabel()} 已加载`;
        setPageRefreshTime();
        renderWatchlistRows();
        if (showToastOnSuccess) showToast(`${getWatchlistRoleLabel()} 已刷新`);
      } catch (error) {
        watchlistState.items = [];
        watchlistState.loaded = true;
        watchlistState.loadedRole = getWatchlistRoleForTab();
        watchlistState.configLoadError = '';
        watchlistState.lastRefresh = 'watchlist 加载失败';
        document.getElementById('watchlistTable').innerHTML = `<tr><td colspan="9" class="empty-state">${escapeHtml(error.message || error)}</td></tr>`;
        renderMobileCardState('watchlistCards', error.message || error);
        document.getElementById('listMeta').textContent = `加载失败: ${error.message || error}`;
        if (isWatchlistRoleTab()) {
          renderSearchResults();
          updateHero();
        }
      }
    }

    async function addCandidate(index) {
      const item = watchlistState.searchResults[index];
      if (!item) return;
      const scope = document.getElementById('targetScope').value || currentEnvironment;
      const note = String(document.getElementById('noteInput').value || '').trim();
      try {
        const payload = await requestJson('/api/custom/ibkr/watchlist/upsert', {
          method: 'POST',
          body: buildWatchlistBody(item, scope, note)
        });
        showToast(`${item.symbol} 已写入 ${scope === 'global' ? 'GLOBAL' : getEnvironmentLabel(scope)} ${getWatchlistRoleLabel()}`);
        const warning = getRuntimeWarning(payload);
        if (warning) showToast(`预热提示: ${warning}`);
        await loadWatchlist(false);
        if (payload.action === 'created' || payload.action === 'updated') {
          document.getElementById('noteInput').value = '';
        }
      } catch (error) {
        showToast(`加入失败: ${error.message || error}`);
      }
    }

    async function editItem(recordId) {
      const item = watchlistState.items.find((row) => String(row.id || '') === String(recordId || ''));
      if (!item) {
        showToast('记录不存在');
        return;
      }
      if (isConfigMonitorItem(item)) {
        showToast('配置来源的市场监控标的请到配置页修改 ibkr_market_ws_symbols');
        return;
      }

      const rawScope = String(item.environment || '').trim().toLowerCase();
      const originalScope = rawScope || currentEnvironment;
      const nextScopeInput = window.prompt(`修改 scope，仅支持 ${currentEnvironment} / global`, originalScope);
      if (nextScopeInput === null) return;
      const nextScope = String(nextScopeInput || '').trim().toLowerCase() || originalScope;
      if (![currentEnvironment, 'global'].includes(nextScope)) {
        showToast('scope 仅支持当前环境或 global');
        return;
      }

      const nextExchange = window.prompt('修改 exchange', item.exchange || '');
      if (nextExchange === null) return;
      const nextIndustry = window.prompt('修改 industry', item.industry || '');
      if (nextIndustry === null) return;
      const nextNote = window.prompt('修改 note', item.note || '');
      if (nextNote === null) return;
      const nextRole = window.prompt('修改 symbol_role(trade/market_monitor)', normalizeWatchlistRole(item.symbol_role || 'trade'));
      if (nextRole === null) return;
      if (!['trade', 'market_monitor'].includes(String(nextRole || '').trim())) {
        showToast('symbol_role 仅支持 trade 或 market_monitor');
        return;
      }

      try {
        const payload = await requestJson('/api/custom/ibkr/watchlist/upsert', {
          method: 'POST',
          body: {
            environment: currentEnvironment,
            source: 'manual_page_edit',
            scope: nextScope,
            manual_member: true,
            symbol_role: String(nextRole || '').trim(),
            symbol: item.symbol,
            exchange: String(nextExchange || '').trim().toUpperCase(),
            industry: String(nextIndustry || '').trim(),
            note: String(nextNote || '').trim()
          }
        });
        if ((nextScope !== originalScope || !rawScope) && recordId) {
          await requestJson('/api/custom/ibkr/watchlist/remove', {
            method: 'POST',
            body: {
              environment: currentEnvironment,
              source: 'manual_page_scope_move',
              record_id: recordId,
              symbol: item.symbol,
              remove_current_day_targets: false
            }
          });
        }
        showToast(`${item.symbol} 已更新`);
        const warning = getRuntimeWarning(payload);
        if (warning) showToast(`预热提示: ${warning}`);
        await loadWatchlist(false);
      } catch (error) {
        showToast(`更新失败: ${error.message || error}`);
      }
    }

    async function removeItem(recordId, symbol, scopeLabel) {
      if (!recordId) return;
      const item = watchlistState.items.find((row) => String(row.id || '') === String(recordId || ''));
      if (isConfigMonitorItem(item)) {
        showToast('配置来源的市场监控标的请到配置页修改 ibkr_market_ws_symbols');
        return;
      }
      if (!window.confirm(`确认删除 ${symbol} (${scopeLabel}) ?`)) return;
      try {
        const payload = await requestJson('/api/custom/ibkr/watchlist/remove', {
          method: 'POST',
          body: {
            environment: currentEnvironment,
            source: 'manual_page_remove',
            record_id: recordId,
            symbol
          }
        });
        showToast(`${symbol} 已删除`);
        const warning = getRuntimeWarning(payload);
        if (warning) showToast(`清理提示: ${warning}`);
        await loadWatchlist(false);
        await loadDailyTargets(false);
      } catch (error) {
        showToast(`删除失败: ${error.message || error}`);
      }
    }

    async function batchAddSymbols() {
      const symbols = parseSymbolList(document.getElementById('batchSymbolsInput').value);
      const scope = document.getElementById('targetScope').value || currentEnvironment;
      const note = String(document.getElementById('noteInput').value || '').trim();
      if (!symbols.length) {
        showToast('先输入要批量加入的 symbols');
        return;
      }

      let success = 0;
      let failed = 0;
      for (let index = 0; index < symbols.length; index += 1) {
        const symbol = symbols[index];
        document.getElementById('batchMeta').textContent = `处理中 ${index + 1}/${symbols.length}: ${symbol}`;
        try {
          const candidate = await searchBestContract(symbol);
          if (!candidate) throw new Error('IBKR 未返回候选');
          await requestJson('/api/custom/ibkr/watchlist/upsert', {
            method: 'POST',
            body: buildWatchlistBody(candidate, scope, note)
          });
          success += 1;
        } catch (_) {
          failed += 1;
        }
      }

      document.getElementById('batchMeta').textContent = `批量导入完成: 成功 ${success} / 失败 ${failed}`;
      showToast(`批量导入完成: ${success}/${symbols.length}`);
      await loadWatchlist(false);
    }

    async function batchDeleteSymbols() {
      const symbols = parseSymbolList(document.getElementById('batchDeleteInput').value);
      if (!symbols.length) {
        showToast('先输入要删除的 symbols');
        return;
      }

      const matched = watchlistState.items.filter((item) => symbols.includes(String(item.symbol || '').trim().toUpperCase()));
      const targets = matched.filter((item) => !isConfigMonitorItem(item));
      if (!targets.length) {
        showToast(matched.length ? '匹配项来自配置，请到配置页修改 ibkr_market_ws_symbols' : '当前加载范围内没有匹配记录');
        return;
      }
      if (!window.confirm(`确认删除 ${targets.length} 条记录？`)) return;

      let deleted = 0;
      for (const item of targets) {
        try {
          await requestJson('/api/custom/ibkr/watchlist/remove', {
            method: 'POST',
            body: {
              environment: currentEnvironment,
              source: 'manual_page_remove',
              record_id: item.id,
              symbol: item.symbol || ''
            }
          });
          deleted += 1;
        } catch (_) {
          // ignore batch delete failures and continue
        }
      }

      document.getElementById('deleteMeta').textContent = `批量删除完成: 删除 ${deleted} / 匹配 ${targets.length}`;
      showToast(`批量删除完成: ${deleted}/${targets.length}`);
      await loadWatchlist(false);
      await loadDailyTargets(false);
    }

    function attachWatchlistEvents() {
      document.getElementById('searchBtn').addEventListener('click', searchContracts);
      document.getElementById('searchInput').addEventListener('keydown', (event) => {
        if (event.key === 'Enter') searchContracts();
      });
      document.getElementById('batchAddBtn').addEventListener('click', batchAddSymbols);
      document.getElementById('clearBatchBtn').addEventListener('click', () => {
        document.getElementById('batchSymbolsInput').value = '';
        document.getElementById('batchMeta').textContent = getWatchlistRoleForTab() === 'market_monitor'
          ? '批量搜索并写入 market_monitor。'
          : '批量搜索并写入当前 scope。';
      });
      document.getElementById('batchDeleteBtn').addEventListener('click', batchDeleteSymbols);
      document.getElementById('refreshListBtn').addEventListener('click', () => loadWatchlist(true));
      document.getElementById('scopeFilter').addEventListener('change', () => {
        watchlistPaginationState.page = 1;
        loadWatchlist(false);
      });
      document.getElementById('listSearchInput').addEventListener('input', () => {
        watchlistPaginationState.page = 1;
        renderWatchlistRows();
      });
      document.getElementById('watchlistPageSize').addEventListener('change', () => {
        watchlistPaginationState.page = 1;
        renderWatchlistRows();
      });
      document.getElementById('targetScope').addEventListener('change', renderSearchResults);
    }


        function renderRunDetail() {
            if (!selectedRun) {
                document.getElementById('selectedRunPill').innerHTML = '';
                document.getElementById('runDetail').innerHTML = '<div class="empty-state">选择 run 查看详情。</div>';
                return;
            }
            document.getElementById('selectedRunPill').innerHTML = detailStatusTag(selectedRun.status);
            const metrics = selectedRun.metrics || {};
            const extra = selectedRun.extra || {};
            const portfolioRisk = metrics.portfolio_risk || extra.portfolio_risk || {};
            const executionCost = metrics.execution_cost_summary || extra.execution_cost_summary || {};
            const monthlyReturns = Array.isArray(metrics.monthly_returns) ? metrics.monthly_returns : [];
            const dailyFunnel = Array.isArray(metrics.daily_funnel) ? metrics.daily_funnel : [];
            const dailyFunnelPage = getBacktestClientPagination('dailyFunnel', dailyFunnel);
            const qualityRows = Array.isArray(metrics.data_quality) ? metrics.data_quality : [];
            const filteredQualityRows = filterBacktestDataQualityRows(qualityRows);
            const qualityPreview = getBacktestTablePreview('dataQuality', filteredQualityRows);
            const qualitySummary = getBacktestDataQualitySummary(qualityRows);
            const filteredQualitySummary = getBacktestDataQualitySummary(filteredQualityRows);
            const qualityDateOptions = getBacktestDataQualityDateOptions(qualityRows);
            const targetLookup = buildBacktestTargetLookup(selectedTargets);
            const skipped = Array.isArray(metrics.skipped_symbols) ? metrics.skipped_symbols : [];
            const symbolCount = countRunSymbols(selectedRun);
            const symbolSummary = summarizeRunSymbols(selectedRun);
            const phaseRuntime = getBacktestPhaseRuntimeModel(selectedRun);
            const detailHtml = `
                <div class="detail-card">
                    <div class="subhead">Meta</div>
                    <div class="detail-list compact-detail-list">
                        <div class="detail-item"><div class="detail-item-label">Name</div><div class="detail-item-value">${escapeHtml(selectedRun.name || '--')}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Run ID</div><div class="detail-item-value mono">${escapeHtml(selectedRun.id || '--')}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Date Range</div><div class="detail-item-value">${escapeHtml(formatRunDate(selectedRun))}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Symbol Source</div><div class="detail-item-value">${escapeHtml(selectedRun.symbol_source || '--')}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Source Environment</div><div class="detail-item-value">${escapeHtml((selectedRun.source_environment || '--').toUpperCase())}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Symbols</div><div class="detail-item-value">${escapeHtml(symbolSummary || '--')}${symbolCount ? ` <span class="mini-chip">${escapeHtml(String(symbolCount))} total</span>` : ''}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Capital</div><div class="detail-item-value">${escapeHtml(formatMoney(selectedRun.initial_capital || 0))}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Total Costs</div><div class="detail-item-value">${escapeHtml(formatMoney((executionCost.total_commission || 0) + (executionCost.estimated_slippage_cost || 0)))}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Runtime</div><div class="detail-item-value">${escapeHtml(phaseRuntime.totalLabel)}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Selection Replay</div><div class="detail-item-value">${escapeHtml(phaseRuntime.selectionLabel)} <span class="mini-chip">${escapeHtml(phaseRuntime.selectionSummary)}</span></div></div>
                        <div class="detail-item"><div class="detail-item-label">Execution Stream</div><div class="detail-item-value">${escapeHtml(phaseRuntime.executionLabel)} <span class="mini-chip">${escapeHtml(phaseRuntime.selectedSummary)}</span></div></div>
                        <div class="detail-item"><div class="detail-item-label">Cache Hit / Miss</div><div class="detail-item-value">${escapeHtml(phaseRuntime.cacheValue)} <span class="mini-chip">${escapeHtml(phaseRuntime.cacheSummary)}</span></div></div>
                    </div>
                    <details class="compact-details run-meta-more">
                        <summary class="compact-summary">执行 / 风控参数</summary>
                        <div class="detail-list">
                            <div class="detail-item"><div class="detail-item-label">Session</div><div class="detail-item-value">${escapeHtml(selectedRun.session_mode || '--')}</div></div>
                            <div class="detail-item"><div class="detail-item-label">Warmup</div><div class="detail-item-value">${escapeHtml(String(extra.warmup_bars || metrics.warmup_bars || '--'))}</div></div>
                            <div class="detail-item"><div class="detail-item-label">Scan Cutoff</div><div class="detail-item-value">${escapeHtml(extra.premarket_cutoff_time || '--')}</div></div>
                            <div class="detail-item"><div class="detail-item-label">Execution</div><div class="detail-item-value">${escapeHtml(extra.execution_model || metrics.execution_model || '--')}</div></div>
                            <div class="detail-item"><div class="detail-item-label">Account Model</div><div class="detail-item-value">${escapeHtml(extra.account_model_mode || portfolioRisk.account_model_mode || '--')}</div></div>
                            <div class="detail-item"><div class="detail-item-label">Fee / Slippage</div><div class="detail-item-value">${escapeHtml(extra.fee_model || executionCost.fee_model || '--')} / ${escapeHtml(extra.slippage_model || executionCost.slippage_model || '--')}</div></div>
                            <div class="detail-item"><div class="detail-item-label">Borrow Mode</div><div class="detail-item-value">${escapeHtml(portfolioRisk.borrow_limit_mode || extra.borrow_limit_mode || '--')}</div></div>
                            <div class="detail-item"><div class="detail-item-label">Borrow Limit</div><div class="detail-item-value">${escapeHtml(formatMoney(portfolioRisk.max_borrow_amount || extra.max_borrow_amount || 0))}</div></div>
                            <div class="detail-item"><div class="detail-item-label">Buying Power</div><div class="detail-item-value">${escapeHtml(formatMoney(portfolioRisk.total_exposure_limit || 0))}</div></div>
                            <div class="detail-item"><div class="detail-item-label">Signal Validity</div><div class="detail-item-value">${escapeHtml(String(portfolioRisk.signal_validity_minutes || extra.signal_validity_minutes || '--'))}m</div></div>
                            <div class="detail-item"><div class="detail-item-label">Order Cut</div><div class="detail-item-value">${escapeHtml(portfolioRisk.order_window_end_time || extra.order_window_end_time || '--')}</div></div>
                            <div class="detail-item"><div class="detail-item-label">Started</div><div class="detail-item-value mono">${escapeHtml(selectedRun.started_at || '--')}</div></div>
                            <div class="detail-item"><div class="detail-item-label">Finished</div><div class="detail-item-value mono">${escapeHtml(selectedRun.finished_at || '--')}</div></div>
                        </div>
                    </details>
                </div>
                <div class="detail-card">
                    <div class="subhead">Strategy Params</div>
                    ${renderBacktestTextPreviewBox('strategyParams', getStrategyParamsText(selectedRun), 'runDetail')}
                </div>
                <div class="detail-card">
                    <div class="subhead">Data Quality</div>
                    ${qualityRows.length ? `
                        <div class="foot-note">${escapeHtml(qualitySummary.label)} · ok ${escapeHtml(formatNumber(qualitySummary.okCount, 0))} · gaps ${escapeHtml(formatNumber(qualitySummary.gapCount, 0))}</div>
                        <div class="quality-filter-bar">
                            <div class="quality-filter-group">
                                <button class="filter-chip ${dataQualityFilters.date === 'all' ? 'active' : ''}" type="button" onclick="setDataQualityDateFilter('all')">All <span>${escapeHtml(formatNumber(qualityRows.length, 0))}</span></button>
                                ${qualityDateOptions.map((item) => `
                                    <button class="filter-chip ${dataQualityFilters.date === item.date ? 'active' : ''} ${item.issueCount ? 'has-issue' : ''}" type="button" onclick="setDataQualityDateFilter('${escapeHtml(item.date)}')">
                                        ${escapeHtml(item.date)} <span>${escapeHtml(formatNumber(item.count, 0))}</span>${item.issueCount ? `<span class="issue-dot">${escapeHtml(formatNumber(item.issueCount, 0))}</span>` : ''}
                                    </button>
                                `).join('')}
                            </div>
                            <div class="quality-filter-group compact">
                                ${[
                                    ['all', 'All'],
                                    ['issues', 'Issues'],
                                    ['gaps', 'Gaps'],
                                    ['ok', 'OK'],
                                ].map(([key, label]) => `
                                    <button class="filter-chip ${dataQualityFilters.status === key ? 'active' : ''}" type="button" onclick="setDataQualityStatusFilter('${escapeHtml(key)}')">${escapeHtml(label)}</button>
                                `).join('')}
                            </div>
                        </div>
                        <div class="foot-note">当前筛选：${escapeHtml(filteredQualitySummary.label)} · ok ${escapeHtml(formatNumber(filteredQualitySummary.okCount, 0))} · gaps ${escapeHtml(formatNumber(filteredQualitySummary.gapCount, 0))}</div>
                        ${renderBacktestTablePreviewBar('dataQuality', qualityPreview, '条 symbol-day 记录')}
                        ${filteredQualityRows.length ? `<div class="quality-grid">
                            ${qualityPreview.rows.map((item) => {
                                const gapCount = Number(item.gap_count || 0);
                                const tone = item.status === 'ok' && gapCount === 0 ? 'good' : (item.status === 'insufficient_data' ? 'bad' : 'warn');
                                const targetRows = getBacktestTargetRowsForQualityRow(item, targetLookup);
                                const reasonModel = getBacktestTargetReasonModel(targetRows);
                                const reasonChips = reasonModel.chips || [];
                                return `
                                    <div class="quality-card ${tone}">
                                        <div class="quality-title">${escapeHtml(item.symbol || '--')} ${item.date ? `<span class="mini-chip">${escapeHtml(item.date)}</span>` : ''}</div>
                                        <div class="quality-copy">status=${escapeHtml(item.status || '--')} · bars=${escapeHtml(String(item.bar_count || 0))} · gaps=${escapeHtml(String(gapCount))}</div>
                                        <div class="quality-copy">${escapeHtml(item.first_bar_us || '--')} → ${escapeHtml(item.last_bar_us || '--')}</div>
                                        ${reasonModel.primaryText ? `<div class="quality-active-summary">${escapeHtml(reasonModel.primaryText)}</div>` : ''}
                                        <div class="quality-reason-row">
                                            ${reasonChips.length
                                                ? reasonChips.map((chip) => `<span class="mini-chip">${escapeHtml(chip)}</span>`).join('')
                                                : `<span class="mini-chip muted">${selectedTargetsLoading ? 'target loading' : 'target reason --'}</span>`}
                                        </div>
                                    </div>
                                `;
                            }).join('')}
                        </div>` : '<div class="empty-state">当前筛选下没有 Data Quality 记录。</div>'}
                    ` : '<div class="empty-state">暂无数据质量汇总。</div>'}
                    ${skipped.length ? `<div class="foot-note">Skipped: ${escapeHtml(skipped.join(', '))}</div>` : ''}
                </div>
                <div class="detail-card">
                    <div class="subhead">Daily Funnel</div>
                    ${dailyFunnel.length ? `
                        ${renderBacktestClientPaginationBar('dailyFunnel', dailyFunnel)}
                        <div class="table-wrap">
                            <table class="data-table" style="min-width: 760px;">
                                <thead>
                                    <tr>
                                        <th>Date</th>
                                        <th>Targets</th>
                                        <th>Signals</th>
                                        <th>Executed</th>
                                        <th>Trades</th>
                                        <th>Target → Entry</th>
                                        <th>Signal → Entry</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    ${dailyFunnelPage.pageRows.map((item) => `
                                        <tr>
                                            <td class="mono">${escapeHtml(item.date || '--')}</td>
                                            <td>${escapeHtml(String(item.target_count || 0))}</td>
                                            <td>${escapeHtml(String(item.signal_count || 0))}</td>
                                            <td>${escapeHtml(String(item.executed_signal_count || 0))}</td>
                                            <td>${escapeHtml(String(item.open_count || item.trade_count || 0))}</td>
                                            <td>${escapeHtml(formatPct(item.target_to_entry_rate || 0))}</td>
                                            <td>${escapeHtml(formatPct(item.signal_to_entry_rate || 0))}</td>
                                        </tr>
                                    `).join('')}
                                </tbody>
                            </table>
                        </div>
                    ` : '<div class="empty-state">暂无每日漏斗统计；旧 run 需要重新回测后生成。</div>'}
                </div>
                <div class="detail-card">
                    <div class="subhead">Monthly Returns</div>
                    ${monthlyReturns.length ? `
                        <div class="table-wrap">
                            <table class="data-table" style="min-width: 420px;">
                                <thead><tr><th>Month</th><th>Return</th></tr></thead>
                                <tbody>
                                    ${monthlyReturns.map((item) => `
                                        <tr>
                                            <td class="mono">${escapeHtml(item.month || '--')}</td>
                                            <td class="${classForValue(item.return_pct)}">${escapeHtml(formatPct(item.return_pct || 0))}</td>
                                        </tr>
                                    `).join('')}
                                </tbody>
                            </table>
                        </div>
                    ` : '<div class="empty-state">暂无月度权益。</div>'}
                </div>
                <div class="detail-card">
                    <div class="subhead">Error / Notes</div>
                    <div class="note-box">${escapeHtml(selectedRun.error || 'No error. This run completed without runtime exceptions.')}</div>
                </div>
                ${buildRunAnalysisCard(selectedRun)}
                ${buildBacktestAuditCard(selectedRun)}
                ${buildBacktestIndicatorCaptureCard(selectedRun)}
                ${buildBacktestTargetReplayCard(selectedRun)}
                ${buildBacktestReverseCaptureCard(selectedRun)}
                ${buildTvParityCard(selectedRun)}
                <div class="detail-card">
                    <div class="subhead">Runtime Extra</div>
                    ${renderBacktestTextPreviewBox('runtimeExtra', JSON.stringify({
                        strategy_tag: extra.strategy_tag || '',
                        resolved_symbols: extra.resolved_symbols || [],
                        force_flat_eod: extra.force_flat_eod,
                        backtest_indicator_capture: extra.backtest_indicator_capture || {},
                        backtest_signal_capture: extra.backtest_signal_capture || {},
                        backtest_target_capture: extra.backtest_target_capture || {},
                        backtest_reverse_capture: extra.backtest_reverse_capture || {},
                        historical_targeting: extra.historical_targeting || {},
                        daily_selection_cache: metrics.daily_selection_cache || extra.daily_selection_cache || extra.historical_targeting?.daily_selection_cache || {},
                        daily_selected_profile: metrics.daily_selected_profile || metrics.portfolio_profile || {},
                        daily_scan_match_diagnostics: metrics.daily_scan_match_diagnostics || extra.daily_scan_match_diagnostics || {},
                        portfolio_risk: metrics.portfolio_risk || extra.portfolio_risk || {},
                        execution_cost_summary: metrics.execution_cost_summary || extra.execution_cost_summary || {},
                        portfolio_rejection_counts: metrics.portfolio_rejection_counts || extra.portfolio_rejection_counts || {},
                        portfolio_candidate_samples: metrics.portfolio_candidate_samples || [],
                        backtest_audit_summary: extra.backtest_audit_summary || {
                            focus_date: metrics.backtest_audit?.focus_date || '',
                            focus_symbols: metrics.backtest_audit?.focus_symbols || [],
                            event_count: metrics.backtest_audit?.event_count || 0,
                            event_type_counts: metrics.backtest_audit?.event_type_counts || {},
                            timeline_truncated: metrics.backtest_audit?.timeline_truncated || false,
                        },
                        analysis_report: extra.analysis_report || {},
                    }, null, 2), 'runDetail')}
                </div>
            `;
            document.getElementById('runDetail').innerHTML = detailHtml;
        }

        function buildReplaySymbolOptions() {
            const select = document.getElementById('replaySymbol');
            const symbols = new Set();
            if (selectedRun?.symbols) {
                selectedRun.symbols.split(',').map((item) => item.trim()).filter(Boolean).forEach((symbol) => symbols.add(symbol));
            }
            selectedTrades.forEach((trade) => {
                if (trade.symbol) symbols.add(String(trade.symbol).trim().toUpperCase());
            });
            selectedTargets.forEach((item) => {
                if (item.symbol) symbols.add(String(item.symbol).trim().toUpperCase());
            });
            selectedSignals.forEach((item) => {
                if (item.symbol) symbols.add(String(item.symbol).trim().toUpperCase());
            });
            selectedReverseSignals.forEach((item) => {
                if (item.symbol) symbols.add(String(item.symbol).trim().toUpperCase());
            });
            const items = Array.from(symbols).sort();
            if (!items.length) {
                select.innerHTML = '<option value="">No symbol</option>';
                return;
            }
            const current = select.value && items.includes(select.value) ? select.value : items[0];
            select.innerHTML = items.map((symbol) => `<option value="${escapeHtml(symbol)}" ${symbol === current ? 'selected' : ''}>${escapeHtml(symbol)}</option>`).join('');
        }

        function renderTrades() {
            document.getElementById('tradeCountLabel').textContent = `${selectedTrades.length} trades`;
            buildReplaySymbolOptions();
            if (!selectedRun) {
                document.getElementById('tradesPanel').innerHTML = '<div class="empty-state">先选一个 run，才会加载对应交易明细。</div>';
                return;
            }
            if (!selectedTrades.length) {
                document.getElementById('tradesPanel').innerHTML = `
                    ${renderBacktestRowPager('trades')}
                    <div class="empty-state">暂无成交记录。</div>
                `;
                return;
            }
            const tradePreview = getBacktestTablePreview('trades', selectedTrades);
            document.getElementById('tradesPanel').innerHTML = `
                ${renderBacktestRowPager('trades')}
                ${renderBacktestTablePreviewBar('trades', tradePreview, '笔交易')}
                <div class="table-wrap">
                    <table class="data-table">
                        <thead>
                            <tr>
                                <th>#</th>
                                <th>Symbol</th>
                                <th>Direction</th>
                                <th>Entry</th>
	                                <th>Exit</th>
	                                <th>Costs</th>
	                                <th>PnL</th>
                                <th>Bars</th>
                                <th>Reason</th>
                                <th>Replay</th>
                            </tr>
                        </thead>
                        <tbody>
                            ${tradePreview.rows.map((trade) => `
                                <tr>
                                    <td class="mono">${trade.trade_index}</td>
                                    <td>${escapeHtml(trade.symbol || '--')}</td>
                                    <td><span class="tag ${trade.direction === 'short' ? 'short' : 'long'}">${escapeHtml((trade.direction || '--').toUpperCase())}</span></td>
	                                    <td class="mono">${escapeHtml((trade.entry_us_time || '--').slice(0, 16))}<br>${escapeHtml(formatMoney(trade.entry_price))}</td>
	                                    <td class="mono">${escapeHtml((trade.exit_us_time || '--').slice(0, 16))}<br>${escapeHtml(formatMoney(trade.exit_price))}</td>
	                                    <td class="mono">${escapeHtml(formatMoney(((trade.extra || {}).total_commission || 0) + ((trade.extra || {}).estimated_slippage_cost || 0)))}</td>
	                                    <td class="${classForValue(trade.pnl)}">${escapeHtml(formatMoney(trade.pnl))}<br>${escapeHtml(formatPct(trade.pnl_pct))}</td>
                                    <td>${escapeHtml(String(trade.bars_held || 0))}</td>
                                    <td>${escapeHtml(trade.exit_reason || '--')}</td>
                                    <td>
                                        <button class="btn ghost" type="button" onclick="replayTrade('${escapeHtml(trade.symbol || '')}', ${Number(trade.entry_bar_ms || 0)})">回放</button>
                                        <button class="btn ghost" type="button" onclick="openTradeChart('${escapeHtml(trade.symbol || '')}', ${Number(trade.entry_bar_ms || 0)}, ${Number(trade.exit_bar_ms || 0)})">主图</button>
                                    </td>
                                </tr>
                            `).join('')}
                        </tbody>
                    </table>
                </div>
            `;
        }

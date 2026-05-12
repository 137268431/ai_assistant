        function getBacktestServiceState() {
            const serviceName = String(activeStatus?.service || 'ibkr-backtest').trim() || 'ibkr-backtest';
            const serviceStatus = String(
                activeStatus?.service_status
                || activeStatus?.service_state?.status
                || (activeStatus?.ok === false ? 'degraded' : (activeStatus?.service ? 'running' : 'unknown'))
            ).trim().toLowerCase() || 'unknown';
            const workerStatus = activeStatus?.running
                ? String(activeStatus?.stage || activeStatus?.status || 'running').trim().toLowerCase()
                : 'idle';
            const clientId = Number(activeStatus?.ib_gateway_client_id || activeStatus?.broker_client_id || 0) || 0;
            return { serviceName, serviceStatus, workerStatus, clientId };
        }

        function buildHeroNotes() {
            const running = Boolean(activeStatus?.running);
            const serviceState = getBacktestServiceState();
            const activeRun = runList.find((item) => item.id === activeStatus?.run_id) || selectedRun;
            const activeBatch = batchList.find((item) => item.id === activeStatus?.batch_id) || selectedBatch;
            const notes = [
                {
                    label: 'Source Env',
                    value: `${getEnvironmentLabel(currentEnvironment)} · bars / targets / watchlist 均取自当前来源环境`,
                },
                {
                    label: 'Isolation',
                    value: '只写 backtest collections；运行时重新计算指标与信号，不回写实盘链路。',
                },
                {
                    label: 'Backtest Service',
                    value: `${serviceState.serviceName} ${serviceState.serviceStatus.toUpperCase()} · worker ${serviceState.workerStatus.toUpperCase()}${serviceState.clientId ? ` · IB client ${serviceState.clientId}` : ''}`,
                },
                {
                    label: 'Worker Job',
                    value: running
                        ? `${activeStatus.stage || 'running'} · ${activeStatus.message || 'backtest in progress'}`
                        : '当前没有运行中的 backtest job',
                },
                {
                    label: 'Active Batch',
                    value: activeBatch
                        ? `${activeBatch.name || activeBatch.id} · ${activeBatch.completed_count}/${activeBatch.variant_count} variants`
                        : '未选中 experiment 时，这里会展示参数扫描批次进度与最佳变体',
                },
                {
                    label: 'Selected Run',
                    value: activeRun
                        ? `${activeRun.name || activeRun.id} · ${formatRunDate(activeRun)}`
                        : '选中一个 run 后会显示收益曲线、成交明细和 replay timeline',
                },
            ];
            document.getElementById('heroNotes').innerHTML = notes.map((note) => `
                <div class="hero-note">
                    <div class="hero-note-label">${escapeHtml(note.label)}</div>
                    <div class="hero-note-value">${escapeHtml(note.value)}</div>
                </div>
            `).join('');
        }

        function renderStatusPanel() {
            const running = Boolean(activeStatus?.running);
            const serviceState = getBacktestServiceState();
            const lastStatus = activeStatus?.status || 'idle';
            const status = running ? lastStatus : 'idle';
            const progress = running ? Number(activeStatus?.progress || 0) : 0;
            document.getElementById('statusPill').innerHTML = detailStatusTag(status);
            document.getElementById('statusProgressBar').style.width = `${Math.max(0, Math.min(100, progress))}%`;
            document.getElementById('statusHeadline').textContent = running
                ? (activeStatus?.message || 'backtest running')
                : '当前没有运行中的 backtest job';
            document.getElementById('statusMeta').innerHTML = `
                <div>Service: <span class="mono">${escapeHtml(`${serviceState.serviceName} ${serviceState.serviceStatus}`)}</span></div>
                <div>IB Client ID: <span class="mono">${escapeHtml(serviceState.clientId || '--')}</span></div>
                <div>Stage: <span class="mono">${escapeHtml(running ? (activeStatus?.stage || 'running') : 'idle')}</span></div>
                <div>Progress: <span class="mono">${progress}%</span></div>
                <div>Run ID: <span class="mono">${escapeHtml(running ? (activeStatus?.run_id || '--') : '--')}</span></div>
                <div>Batch ID: <span class="mono">${escapeHtml(running ? (activeStatus?.batch_id || '--') : '--')}</span></div>
                <div>Updated: <span class="mono">${activeStatus?.updated_at_ms ? formatBarTimeMsToET(activeStatus.updated_at_ms) : '--'}</span></div>
            `;
            if (running) {
                const run = runList.find((item) => item.id === activeStatus.run_id);
                const batch = batchList.find((item) => item.id === activeStatus.batch_id);
                document.getElementById('statusNote').textContent = run
                    ? `正在运行：${run.name || run.id}\n日期：${formatRunDate(run)}\nSymbols：${run.symbols || '--'}${batch ? `\nExperiment：${batch.name || batch.id}` : ''}`
                    : `Worker 正在执行中，当前 stage=${activeStatus.stage || 'running'}。`;
            } else {
                const lastStateHint = ['completed', 'failed', 'cancelled'].includes(lastStatus)
                    ? `最近一次执行结果：${lastStatus} · ${activeStatus?.message || 'backtest finished'}`
                    : '暂无运行中的回测。你可以直接启动新的 run，或者点击左侧历史记录查看既有结果。';
                document.getElementById('statusNote').textContent = lastStateHint;
            }
        }

        function renderBatches() {
            document.getElementById('batchCountLabel').textContent = `${batchList.length} experiments`;
            if (!batchList.length) {
                document.getElementById('batchList').innerHTML = '<div class="empty-state">暂无参数扫描。<br>填入 Variants 后启动。</div>';
                return;
            }
            document.getElementById('batchList').innerHTML = batchList.map((batch) => `
                <div class="run-item ${batch.id === selectedBatchId ? 'active' : ''}" onclick="selectBatch('${escapeHtml(batch.id)}')">
                    <div class="run-item-top">
                        <div>
                            <div class="run-item-title">${escapeHtml(batch.name || batch.id)}</div>
                            <div class="run-item-copy mono">${escapeHtml(formatRunDate(batch))}</div>
                        </div>
                        ${detailStatusTag(batch.status)}
                    </div>
                    <div class="run-stats">
                        <span class="mini-chip">VAR ${batch.variant_count}</span>
                        <span class="mini-chip">DONE ${batch.completed_count}</span>
                        <span class="mini-chip">BEST ${formatPct(batch.best_total_return_pct)}</span>
                        <span class="mini-chip">SH ${formatNumber(batch.best_sharpe, 2)}</span>
                    </div>
                    <div class="run-item-bottom" style="margin-top: 10px;">
                        <div class="run-item-copy">${escapeHtml(summarizeRunSymbols(batch))}</div>
                        <div class="run-item-copy mono">${escapeHtml((batch.created || '--').slice(0, 19))}</div>
                    </div>
                </div>
            `).join('');
        }

        function renderBatchDetail() {
            if (!selectedBatch) {
                document.getElementById('batchDetail').innerHTML = '<div class="empty-state">选择 experiment 查看参数与排行。</div>';
                return;
            }
            const leaderboard = Array.isArray(selectedBatch.leaderboard) ? selectedBatch.leaderboard : [];
            const leaderboardPreview = getBacktestTablePreview('leaderboard', leaderboard);
            document.getElementById('batchDetail').innerHTML = `
                <div class="detail-card">
                    <div class="subhead">Experiment Meta</div>
                    <div class="detail-list">
                        <div class="detail-item"><div class="detail-item-label">Name</div><div class="detail-item-value">${escapeHtml(selectedBatch.name || '--')}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Batch ID</div><div class="detail-item-value mono">${escapeHtml(selectedBatch.id || '--')}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Progress</div><div class="detail-item-value">${escapeHtml(`${selectedBatch.completed_count}/${selectedBatch.variant_count}`)}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Best Variant</div><div class="detail-item-value">${escapeHtml(selectedBatch.best_variant_label || '--')}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Best Return</div><div class="detail-item-value ${classForValue(selectedBatch.best_total_return_pct)}">${escapeHtml(formatPct(selectedBatch.best_total_return_pct || 0))}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Best Sharpe</div><div class="detail-item-value ${classForValue(selectedBatch.best_sharpe)}">${escapeHtml(formatNumber(selectedBatch.best_sharpe, 2))}</div></div>
                    </div>
                </div>
                <div class="detail-card">
                    <div class="subhead">Variants JSON</div>
                    ${renderBacktestTextPreviewBox('batchVariants', getBatchVariantsText(selectedBatch), 'batchDetail')}
                </div>
                ${buildExperimentAnalysisCard(selectedBatch)}
                <div class="detail-card">
                    <div class="subhead">Leaderboard</div>
                    ${leaderboard.length ? `
                        ${renderBacktestTablePreviewBar('leaderboard', leaderboardPreview, '个变体')}
                        <div class="table-wrap">
                            <table class="data-table">
                                <thead>
                                    <tr>
                                        <th>#</th>
                                        <th>Variant</th>
                                        <th>Status</th>
                                        <th>Return</th>
                                        <th>Sharpe</th>
                                        <th>Trades</th>
                                        <th>Drawdown</th>
                                        <th>Run</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    ${leaderboardPreview.rows.map((item, index) => `
                                        <tr>
                                            <td class="mono">${index + 1}</td>
                                            <td>${escapeHtml(item.variant_label || item.name || '--')}</td>
                                            <td>${detailStatusTag(item.status || '--')}</td>
                                            <td class="${classForValue(item.total_return_pct)}">${escapeHtml(formatPct(item.total_return_pct || 0))}</td>
                                            <td class="${classForValue(item.sharpe)}">${escapeHtml(formatNumber(item.sharpe, 2))}</td>
                                            <td>${escapeHtml(String(item.trade_count || 0))}</td>
                                            <td class="negative">${escapeHtml(formatPct(-Math.abs(item.max_drawdown_pct || 0)))}</td>
                                            <td><button class="btn ghost" type="button" onclick="openRunFromBatch('${escapeHtml(item.run_id || '')}')">打开 Run</button></td>
                                        </tr>
                                    `).join('')}
                                </tbody>
                            </table>
                        </div>
                    ` : '<div class="empty-state">暂无 leaderboard。</div>'}
                </div>
            `;
        }

        function renderRuns() {
            document.getElementById('runCountLabel').textContent = `${runList.length} runs`;
            if (!runList.length) {
                document.getElementById('runList').innerHTML = '<div class="empty-state">暂无回测记录。<br>先跑一轮。</div>';
                return;
            }
            const runPreview = getBacktestTablePreview('runs', runList);
            document.getElementById('runList').innerHTML = `
                ${renderBacktestTablePreviewBar('runs', runPreview, '个 run')}
                ${runPreview.rows.map((run) => `
                <div class="run-item ${run.id === selectedRunId ? 'active' : ''}" onclick="selectRun('${escapeHtml(run.id)}')">
                    <div class="run-item-top">
                        <div>
                            <div class="run-item-title">${escapeHtml(run.name || run.id)}</div>
                            <div class="run-item-copy mono">${escapeHtml(formatRunDate(run))}</div>
                        </div>
                        ${detailStatusTag(run.status)}
                    </div>
                    <div class="run-stats">
                        <span class="mini-chip">${escapeHtml(run.source_environment.toUpperCase())}</span>
                        <span class="mini-chip">TRADES ${run.trade_count}</span>
                        <span class="mini-chip">RET ${formatPct(run.total_return_pct)}</span>
                        <span class="mini-chip">SHARPE ${formatNumber(run.sharpe, 2)}</span>
                    </div>
                    <div class="run-item-bottom" style="margin-top: 10px;">
                        <div class="run-item-copy">${escapeHtml(summarizeRunSymbols(run))}</div>
                        <div class="run-item-copy mono">${escapeHtml((run.created || '--').slice(0, 19))}</div>
                    </div>
                </div>
                `).join('')}
            `;
        }

        function renderMetrics() {
            if (!selectedRun) {
                document.getElementById('metricsGrid').innerHTML = '<div class="empty-state">选择 run 查看收益与风险。</div>';
                document.getElementById('metricsMoreGrid').innerHTML = '';
                destroyEquityChart();
                return;
            }
            const metrics = selectedRun.metrics || {};
            const executionCost = metrics.execution_cost_summary || selectedRun.extra?.execution_cost_summary || {};
            const scanDiagnostics = metrics.daily_scan_match_diagnostics || selectedRun.extra?.daily_scan_match_diagnostics || {};
            const dailySelectedProfile = metrics.daily_selected_profile || metrics.portfolio_profile || selectedRun.extra?.daily_selected_profile || {};
            const dailySelectionCache = metrics.daily_selection_cache || selectedRun.extra?.daily_selection_cache || selectedRun.extra?.historical_targeting?.daily_selection_cache || {};
            const primaryCards = [
                ['Net PnL', formatMoney(selectedRun.net_pnl), classForValue(selectedRun.net_pnl), `${selectedRun.trade_count} trades`],
                ['Total Return', formatPct(selectedRun.total_return_pct), classForValue(selectedRun.total_return_pct), `ending ${formatMoney(metrics.ending_equity || 0)}`],
                ['Sharpe', formatNumber(selectedRun.sharpe, 2), classForValue(selectedRun.sharpe), `sortino ${formatNumber(metrics.sortino, 2)}`],
                ['Max Drawdown', formatPct(-Math.abs(selectedRun.max_drawdown_pct || 0)), 'negative', `profit factor ${formatNumber(metrics.profit_factor, 2)}`],
            ];
            const secondaryCards = [
                ['Win Rate', formatPct(selectedRun.win_rate), classForValue(selectedRun.win_rate - 50), `expectancy ${formatMoney(metrics.expectancy || 0)}`],
                ['Avg Win / Loss', `${formatMoney(metrics.avg_win || 0)} / ${formatMoney(metrics.avg_loss || 0)}`, '', `盈亏比 ${formatNumber(metrics.win_loss_ratio || 0, 2)}`],
                ['Signal Fill', formatPct(metrics.signal_fill_rate || 0), classForValue((metrics.signal_fill_rate || 0) - 50), `${metrics.executed_signal_count || 0}/${metrics.signal_count || 0} executed`],
                ['Portfolio Exposure', formatMoney(metrics.portfolio_max_gross_exposure || 0), '', `borrow max ${formatMoney(metrics.portfolio_max_borrowed_amount || 0)}`],
                ['Execution Costs', formatMoney((executionCost.total_commission || 0) + (executionCost.estimated_slippage_cost || 0)), '', `${executionCost.fee_model || '--'} / ${executionCost.slippage_model || '--'}`],
                ['Signal Rejects', String(Object.values(metrics.portfolio_rejection_counts || {}).reduce((sum, value) => sum + Number(value || 0), 0)), '', formatBreakdown(metrics.portfolio_rejection_counts || {})],
                ['Replay Targets', String(metrics.backtest_target_count || 0), '', `${metrics.historical_targeting?.target_date_count || 0} trade dates`],
                ['Daily Opens', String(sumDailyOpenCounts(metrics.daily_open_counts || [])), '', formatDailyCounts(metrics.daily_open_counts || [])],
                ['Target → Entry', formatPct(metrics.target_to_entry_rate || 0), classForValue((metrics.target_to_entry_rate || 0) - 25), metrics.target_funnel_enabled ? 'historical target funnel' : 'target funnel disabled'],
                ['Signal → Entry', formatPct(metrics.signal_to_entry_rate || 0), classForValue((metrics.signal_to_entry_rate || 0) - 50), `${metrics.funnel_executed_signal_count ?? metrics.executed_signal_count ?? 0}/${metrics.funnel_signal_count ?? metrics.signal_count ?? 0} executed`],
                ['Reverse Actions', String(metrics.backtest_reverse_signal_count || 0), '', formatBreakdown(metrics.backtest_reverse_action_breakdown || {})],
            ];
            if (dailySelectedProfile.enabled || dailySelectedProfile.mode === 'daily_selected_live_sd') {
                secondaryCards.splice(6, 0, [
                    'Daily Selected',
                    String(dailySelectedProfile.selected_symbol_days || 0),
                    '',
                    `${dailySelectedProfile.trade_dates || 0} days · bars ${dailySelectedProfile.bars_loaded || 0}`,
                ]);
            }
            if (dailySelectionCache.enabled) {
                secondaryCards.splice(6, 0, [
                    'Selection Cache',
                    `${dailySelectionCache.hit_days || 0}/${dailySelectionCache.total_days || 0}`,
                    '',
                    `${formatPct(dailySelectionCache.hit_rate || 0)} hit · rebuilt ${dailySelectionCache.rebuilt_days || 0}`,
                ]);
            }
            if (scanDiagnostics.enabled) {
                secondaryCards.splice(6, 0, [
                    'Scan Match',
                    formatPct(scanDiagnostics.selected_day_signal_rate_pct || 0),
                    classForValue((scanDiagnostics.selected_day_signal_rate_pct || 0) - 35),
                    `${scanDiagnostics.selected_day_signal_count || 0}/${scanDiagnostics.generated_signal_count || 0} signals`,
                ]);
            }
            const renderMetricCards = (cards) => cards.map(([label, value, tone, subtext]) => `
                <div class="metric-card">
                    <div class="metric-label">${escapeHtml(label)}</div>
                    <div class="metric-value ${tone}">${escapeHtml(value)}</div>
                    <div class="metric-subtext">${escapeHtml(subtext)}</div>
                </div>
            `).join('');
            document.getElementById('metricsGrid').innerHTML = renderMetricCards(primaryCards);
            document.getElementById('metricsMoreGrid').innerHTML = renderMetricCards(secondaryCards);
            renderEquityChart(selectedRun);
        }

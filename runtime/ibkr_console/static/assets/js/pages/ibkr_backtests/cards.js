        function groupTargetsByDate(items, run) {
            const dailyRows = Array.isArray(run?.extra?.historical_targeting?.daily) ? run.extra.historical_targeting.daily : [];
            const dailyMap = new Map(
                dailyRows.map((item) => [String(item?.date || ''), item || {}]),
            );
            const groups = new Map();
            items.forEach((item) => {
                const date = String(item?.date || '--');
                if (!groups.has(date)) {
                    groups.set(date, {
                        date,
                        summary: dailyMap.get(date) || null,
                        items: [],
                    });
                }
                groups.get(date).items.push(item);
            });
            return Array.from(groups.values())
                .map((group) => ({
                    ...group,
                    items: group.items.sort((a, b) => {
                        const rankDelta = Number(a.rank || 9999) - Number(b.rank || 9999);
                        if (rankDelta !== 0) return rankDelta;
                        const scoreDelta = Number(b.score || 0) - Number(a.score || 0);
                        if (scoreDelta !== 0) return scoreDelta;
                        return String(a.symbol || '').localeCompare(String(b.symbol || ''));
                    }),
                }))
                .sort((a, b) => String(b.date || '').localeCompare(String(a.date || '')));
        }

        function buildBacktestTargetReplayCard(run) {
            const capture = run?.extra?.backtest_target_capture || run?.metrics?.backtest_target_capture || {};
            const historicalTargeting = run?.extra?.historical_targeting || run?.metrics?.historical_targeting || {};
            const scanDiagnostics = run?.metrics?.daily_scan_match_diagnostics || run?.extra?.daily_scan_match_diagnostics || {};
            const groups = groupTargetsByDate(selectedTargets, run);
            const targetPage = getBacktestClientPagination('historicalTargets', selectedTargets);
            const visibleTargetIds = new Set(targetPage.pageRows.map((item) => item.id || `${item.date}:${item.symbol}:${item.rank}`));
            const visibleGroups = groups
                .map((group) => ({
                    ...group,
                    items: group.items.filter((item) => visibleTargetIds.has(item.id || `${item.date}:${item.symbol}:${item.rank}`)),
                }))
                .filter((group) => group.items.length);
            const selectedSymbolCount = Number(
                historicalTargeting.selected_symbol_count
                || new Set(selectedTargets.map((item) => String(item?.symbol || '').trim()).filter(Boolean)).size
                || 0,
            );
            const targetCount = Number(
                run?.metrics?.backtest_target_count
                || capture.saved_count
                || historicalTargeting.target_row_count
                || selectedTargets.length
                || 0,
            );
            const status = selectedTargetsLoading
                ? 'running'
                : String(capture.status || (targetCount > 0 ? 'ok' : 'empty')).toLowerCase();
            const enabled = String(run?.symbol_source || '').toLowerCase() === 'daily_scan_replay' || targetCount > 0;

            if (!enabled && !selectedTargets.length) {
                return `
                    <div class="detail-card">
                        <div class="subhead">Historical Targets</div>
                        <div class="empty-state">未启用历史选股回放。</div>
                    </div>
                `;
            }

            return `
                <div class="detail-card">
                    <div class="subhead">Historical Targets</div>
                    <div class="foot-note">展示历史选股回放结果；开仓以 selection plan 为准。</div>
                    <div style="margin: 10px 0 14px;">${detailStatusTag(status)}</div>
                    <div class="detail-list">
                        <div class="detail-item"><div class="detail-item-label">Collection</div><div class="detail-item-value mono">${escapeHtml(capture.collection || 'ibkr_backtest_targets')}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Rows</div><div class="detail-item-value">${escapeHtml(String(targetCount))}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Trade Dates</div><div class="detail-item-value">${escapeHtml(String(historicalTargeting.target_date_count || groups.length || 0))}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Selected Symbols</div><div class="detail-item-value">${escapeHtml(String(selectedSymbolCount))}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Universe Mode</div><div class="detail-item-value">${escapeHtml(historicalTargeting.universe_mode || '--')}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Cutoff</div><div class="detail-item-value">${escapeHtml(historicalTargeting.premarket_cutoff_time || run?.extra?.premarket_cutoff_time || '--')}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Scan Session</div><div class="detail-item-value">${escapeHtml(historicalTargeting.scan_session_mode || '--')}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Scan Warmup</div><div class="detail-item-value">${escapeHtml(String(historicalTargeting.scan_warmup_bars || run?.extra?.scan_warmup_bars || '--'))}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Saved / Errors</div><div class="detail-item-value">${escapeHtml(String(capture.saved_count || 0))} / ${escapeHtml(String(capture.error_count || 0))}</div></div>
                        ${scanDiagnostics.enabled ? `
                            <div class="detail-item"><div class="detail-item-label">Signal Match</div><div class="detail-item-value">${escapeHtml(formatPct(scanDiagnostics.selected_day_signal_rate_pct || 0))}</div></div>
                            <div class="detail-item"><div class="detail-item-label">Matched Signals</div><div class="detail-item-value">${escapeHtml(String(scanDiagnostics.selected_day_signal_count || 0))} / ${escapeHtml(String(scanDiagnostics.generated_signal_count || 0))}</div></div>
                            <div class="detail-item"><div class="detail-item-label">Not Selected</div><div class="detail-item-value">${escapeHtml(String(scanDiagnostics.not_selected_signal_count || 0))}</div></div>
                        ` : ''}
                    </div>
                    <div style="margin-top: 12px;">${renderBacktestRowPager('targets')}</div>
                    ${renderBacktestClientPaginationBar('historicalTargets', selectedTargets)}
                    ${selectedTargetsLoading ? '<div class="empty-state" style="margin-top: 14px;">读取历史 targets ...</div>' : ''}
                    ${!selectedTargetsLoading && visibleGroups.length ? visibleGroups.map((group) => {
                        const summary = group.summary || {};
                        const first = group.items[0] || {};
                        const universeSize = Number(summary.universe_size || first?.extra?.universe_size || 0);
                        const selectedCount = Number(summary.selected_count || group.items.length || 0);
                        const readyCount = Number(summary.ready_symbol_count || 0);
                        const candidateCount = Number(summary.candidate_count || 0);
                        return `
                            <div class="subhead" style="margin-top: 18px;">${escapeHtml(group.date)}</div>
                            <div class="foot-note">
                                selected ${escapeHtml(String(selectedCount))}
                                ${candidateCount ? ` / candidates ${escapeHtml(String(candidateCount))}` : ''}
                                ${readyCount ? ` · ready ${escapeHtml(String(readyCount))}` : ''}
                                ${universeSize ? ` · universe ${escapeHtml(String(universeSize))}` : ''}
                            </div>
                            <div class="table-wrap" style="margin-top: 10px;">
                                <table class="data-table" style="min-width: 900px;">
                                    <thead>
                                        <tr>
                                            <th>Rank</th>
                                            <th>Symbol</th>
                                            <th>Score</th>
                                            <th>Bias</th>
                                            <th>Reason</th>
                                            <th>Cutoff</th>
                                            <th>Universe</th>
                                            <th>Replay</th>
                                        </tr>
                                    </thead>
                                    <tbody>
                                        ${group.items.map((item) => `
                                            <tr>
                                                <td class="mono">${escapeHtml(String(item.rank || '--'))}</td>
                                                <td class="mono">${escapeHtml(item.symbol || '--')}${item.exchange ? `<br><span style="color: var(--muted);">${escapeHtml(item.exchange)}</span>` : ''}</td>
                                                <td>${escapeHtml(formatNumber(item.score || 0, 4))}</td>
                                                <td>${item.direction_bias === 'short'
                                                    ? '<span class="tag short">SHORT</span>'
                                                    : item.direction_bias === 'long'
                                                        ? '<span class="tag long">LONG</span>'
                                                        : `<span class="tag">${escapeHtml((item.direction_bias || 'neutral').toUpperCase())}</span>`}</td>
                                                <td>${escapeHtml(item.scan_reason || '--')}</td>
                                                <td class="mono">${escapeHtml(item.us_time || '--')}</td>
                                                <td>${escapeHtml(String(item?.extra?.universe_size || universeSize || '--'))}</td>
                                                <td><button class="btn ghost" type="button" onclick="replayTarget('${escapeHtml(item.symbol || '')}', ${Number(item.bar_time_ms || 0)})">回放</button></td>
                                            </tr>
                                        `).join('')}
                                    </tbody>
                                </table>
                            </div>
                        `;
                    }).join('') : ''}
                    ${!selectedTargetsLoading && !visibleGroups.length ? '<div class="empty-state" style="margin-top: 14px;">暂无历史 target 明细。</div>' : ''}
                </div>
            `;
        }

        function buildBacktestSetupBreakdownCard(run) {
            const metrics = run?.metrics || {};
            const setupRows = Array.isArray(metrics.setup_stats) ? metrics.setup_stats : [];
            const summary = metrics.setup_summary && typeof metrics.setup_summary === 'object' ? metrics.setup_summary : {};
            if (!setupRows.length && !Object.keys(summary).length) {
                return `
                    <div class="detail-card">
                        <div class="subhead">Setup Breakdown</div>
                        <div class="empty-state">暂无 setup 维度统计；旧 run 或未启用 setup-flattening 的 run 会显示为空。</div>
                    </div>
                `;
            }
            const sortedRows = setupRows.slice().sort((a, b) => Number(b?.net_pnl || 0) - Number(a?.net_pnl || 0));
            const setupPage = getBacktestClientPagination('setupBreakdown', sortedRows);
            const insufficient = Array.isArray(summary.insufficient_sample_setups)
                ? summary.insufficient_sample_setups.map((item) => String(item || '').trim()).filter(Boolean)
                : [];
            return `
                <div class="detail-card">
                    <div class="subhead">Setup Breakdown</div>
                    <div class="foot-note">按后端提供的 setup_stats 展示；字段缺失时以 -- 或 0 兜底。</div>
                    <div class="detail-list" style="margin-top: 12px;">
                        <div class="detail-item"><div class="detail-item-label">Setup Count</div><div class="detail-item-value">${escapeHtml(String(summary.setup_count ?? setupRows.length ?? 0))}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Top Contributor</div><div class="detail-item-value">${escapeHtml(summary.top_contributor || '--')}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Top Drag</div><div class="detail-item-value">${escapeHtml(summary.top_drag || '--')}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Insufficient Samples</div><div class="detail-item-value">${escapeHtml(insufficient.join(', ') || '--')}</div></div>
                    </div>
                    ${sortedRows.length ? `
                        ${renderBacktestClientPaginationBar('setupBreakdown', sortedRows)}
                        <div class="table-wrap" style="margin-top: 10px;">
                            <table class="data-table" style="min-width: 980px;">
                                <thead>
                                    <tr>
                                        <th>Setup</th>
                                        <th>Family</th>
                                        <th>Dir / Mode</th>
                                        <th>Signals</th>
                                        <th>Fill Rate</th>
                                        <th>Trades</th>
                                        <th>Net PnL</th>
                                        <th>Win / PF</th>
                                        <th>Expectancy</th>
                                        <th>Reverse</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    ${setupPage.pageRows.map((item) => {
                                        const setupName = item.setup_label || item.setup || '--';
                                        const direction = String(item.direction || '').trim().toLowerCase();
                                        return `
                                            <tr>
                                                <td>${escapeHtml(setupName)}<br><span class="mono" style="color: var(--muted);">${escapeHtml(item.setup || '--')}</span></td>
                                                <td>${escapeHtml(item.setup_family || '--')}</td>
                                                <td>${direction ? `<span class="tag ${direction === 'short' ? 'short' : 'long'}">${escapeHtml(direction.toUpperCase())}</span>` : '--'}<br><span class="mono" style="color: var(--muted);">${escapeHtml(item.signal_mode || '--')}</span></td>
                                                <td>${escapeHtml(String(item.signal_count || 0))}<br><span style="color: var(--muted);">exec ${escapeHtml(String(item.executed_signal_count || 0))}</span></td>
                                                <td>${escapeHtml(formatPct(item.signal_fill_rate || 0))}</td>
                                                <td>${escapeHtml(String(item.trade_count || 0))}</td>
                                                <td class="${classForValue(item.net_pnl)}">${escapeHtml(formatMoney(item.net_pnl || 0))}</td>
                                                <td>${escapeHtml(formatPct(item.win_rate || 0))}<br><span style="color: var(--muted);">PF ${escapeHtml(formatNumber(item.profit_factor || 0, 2))}</span></td>
                                                <td class="${classForValue(item.expectancy)}">${escapeHtml(formatMoney(item.expectancy || 0))}</td>
                                                <td>${escapeHtml(formatBreakdown(item.reverse_action_breakdown || {}))}</td>
                                            </tr>
                                        `;
                                    }).join('')}
                                </tbody>
                            </table>
                        </div>
                    ` : '<div class="empty-state" style="margin-top: 14px;">setup_summary 存在，但暂无 setup_stats 明细。</div>'}
                </div>
            `;
        }


        function backtestAuditEventLabel(event) {
            const type = String(event?.event_type || '');
            const labels = {
                target_selected: '入选标的',
                signal_generated: '信号产生',
                signal_pending: '等待成交',
                entry_filled: '成交成功',
                trade_opened: '开仓记录',
                trade_closed: '平仓/止盈止损',
                atr_stop_adjust: 'ATR 止损调整',
                exit_policy_stop_adjust: '策略止损调整',
                target_policy_stop_adjust: '目标策略止损调整',
                reverse_adjust_sl: '反转调止损',
                reverse_adjust_tp: '反转调止盈',
                reverse_action: '特殊/反转事件',
                signal_skipped: '信号跳过',
                signal_dropped: '信号丢弃',
            };
            return labels[type] || type || '--';
        }

        function formatAuditPriceChange(event) {
            const parts = [];
            if (Number(event?.entry_price || 0)) parts.push(`entry ${formatMoney(event.entry_price)}`);
            if (Number(event?.exit_price || 0)) parts.push(`exit ${formatMoney(event.exit_price)}`);
            if (Number(event?.old_sl || 0) || Number(event?.new_sl || 0)) {
                parts.push(`SL ${formatMoney(event.old_sl || 0)} → ${formatMoney(event.new_sl || 0)}`);
            } else if (Number(event?.stop_loss || 0)) {
                parts.push(`SL ${formatMoney(event.stop_loss)}`);
            }
            if (Number(event?.old_tp || 0) || Number(event?.new_tp || 0)) {
                parts.push(`TP ${formatMoney(event.old_tp || 0)} → ${formatMoney(event.new_tp || 0)}`);
            } else if (Number(event?.take_profit || 0)) {
                parts.push(`TP ${formatMoney(event.take_profit)}`);
            }
            if (Number(event?.pnl || 0)) parts.push(`PnL ${formatMoney(event.pnl)}`);
            return parts.join('<br>') || '--';
        }

        function renderAuditTimelineRows(events) {
            return events.map((event) => `
                <tr>
                    <td class="mono">${escapeHtml((event.us_time || '--').slice(0, 16))}</td>
                    <td class="mono">${escapeHtml(event.symbol || '--')}</td>
                    <td>${escapeHtml(backtestAuditEventLabel(event))}<br><span class="mono" style="color: var(--muted);">${escapeHtml(event.stage || '--')}</span></td>
                    <td>${event.direction ? `<span class="tag ${event.direction === 'short' ? 'short' : 'long'}">${escapeHtml(String(event.direction).toUpperCase())}</span>` : '--'}</td>
                    <td class="mono">${escapeHtml(event.signal_id || event.signal || '--')}</td>
                    <td>${escapeHtml(event.status || '--')}<br><span style="color: var(--muted);">${escapeHtml(event.reason || '')}</span></td>
                    <td class="mono">${formatAuditPriceChange(event)}</td>
                </tr>
            `).join('');
        }

        function buildBacktestAuditCard(run) {
            const audit = run?.metrics?.backtest_audit || run?.extra?.backtest_audit_summary || {};
            const focusDay = audit.focus_day || {};
            const focusTimeline = Array.isArray(focusDay.timeline) ? focusDay.timeline : [];
            const dailySummary = Array.isArray(audit.daily_summary) ? audit.daily_summary : [];
            const eventTypeCounts = audit.event_type_counts || {};
            const focusSymbols = Array.isArray(audit.focus_symbols) ? audit.focus_symbols : [];
            const timeline = focusTimeline.length
                ? focusTimeline
                : (Array.isArray(audit.timeline) ? audit.timeline.filter((item) => String(item?.date || '') === String(audit.focus_date || '')) : []);
            const dailySummaryPage = getBacktestClientPagination('auditDailySummary', dailySummary);
            const timelinePage = getBacktestClientPagination('auditFocusTimeline', timeline);

            if (!audit.enabled && !dailySummary.length && !timeline.length) {
                return `
                    <div class="detail-card">
                        <div class="subhead">回测审计链路</div>
                        <div class="empty-state">旧 run 没有审计链路；重新回测后会展示标的、信号、成交、止盈止损和特殊事件时间线。</div>
                    </div>
                `;
            }

            return `
                <div class="detail-card">
                    <div class="subhead">回测审计链路</div>
                    <div class="foot-note">按 run 内实际行串起：今日/焦点日标的 → 信号 → 成交 → 止盈止损 → SL/TP 调整 → 反转等特殊事件。</div>
                    <div class="detail-list" style="margin-top: 12px;">
                        <div class="detail-item"><div class="detail-item-label">Focus Date</div><div class="detail-item-value mono">${escapeHtml(audit.focus_date || focusDay.date || '--')}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Focus Symbols</div><div class="detail-item-value">${escapeHtml(focusSymbols.join(', ') || '--')}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Events</div><div class="detail-item-value">${escapeHtml(String(audit.event_count || timeline.length || 0))}${audit.timeline_truncated ? ' truncated' : ''}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Event Types</div><div class="detail-item-value">${escapeHtml(formatBreakdown(eventTypeCounts))}</div></div>
                    </div>
                    ${dailySummary.length ? `
                        <div class="subhead" style="margin-top: 18px;">Daily Summary</div>
                        ${renderBacktestClientPaginationBar('auditDailySummary', dailySummary)}
                        <div class="table-wrap" style="margin-top: 10px;">
                            <table class="data-table" style="min-width: 880px;">
                                <thead>
                                    <tr>
                                        <th>Date</th>
                                        <th>Targets</th>
                                        <th>Signals</th>
                                        <th>Executed</th>
                                        <th>Trades</th>
                                        <th>TP / SL</th>
                                        <th>Risk / Special</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    ${dailySummaryPage.pageRows.map((item) => `
                                        <tr>
                                            <td class="mono">${escapeHtml(item.date || '--')}</td>
                                            <td>${escapeHtml(String(item.target_count || 0))}<br><span class="mono" style="color: var(--muted);">${escapeHtml((item.target_symbols || []).join(', '))}</span></td>
                                            <td>${escapeHtml(String(item.signal_count || 0))}</td>
                                            <td>${escapeHtml(String(item.executed_signal_count || 0))}</td>
                                            <td>${escapeHtml(String(item.trade_count || 0))}</td>
                                            <td>${escapeHtml(String(item.take_profit_count || 0))} / ${escapeHtml(String(item.stop_loss_count || 0))}</td>
                                            <td>${escapeHtml(String(item.risk_adjustment_count || 0))} / ${escapeHtml(String(item.special_event_count || 0))}</td>
                                        </tr>
                                    `).join('')}
                                </tbody>
                            </table>
                        </div>
                    ` : ''}
                    <div class="subhead" style="margin-top: 18px;">Focus Timeline</div>
                    ${timeline.length ? `
                        ${focusDay.timeline_truncated ? '<div class="foot-note">焦点日事件较多，完整数据在 Runtime Extra / metrics.backtest_audit 中；这里按页查看。</div>' : ''}
                        ${renderBacktestClientPaginationBar('auditFocusTimeline', timeline)}
                        <div class="table-wrap" style="margin-top: 10px;">
                            <table class="data-table" style="min-width: 1080px;">
                                <thead>
                                    <tr>
                                        <th>Time</th>
                                        <th>Symbol</th>
                                        <th>Event</th>
                                        <th>Side</th>
                                        <th>Signal</th>
                                        <th>Status / Reason</th>
                                        <th>Prices</th>
                                    </tr>
                                </thead>
                                <tbody>${renderAuditTimelineRows(timelinePage.pageRows)}</tbody>
                            </table>
                        </div>
                    ` : '<div class="empty-state" style="margin-top: 14px;">焦点日暂无审计事件。</div>'}
                </div>
            `;
        }

        function buildBacktestIndicatorCaptureCard(run) {
            const capture = run?.extra?.backtest_indicator_capture || run?.metrics?.backtest_indicator_capture || {};
            const status = String(capture.status || 'empty').toLowerCase();
            const errors = Array.isArray(capture.errors) ? capture.errors : [];
            return `
                <div class="detail-card">
                    <div class="subhead">回测指标留痕</div>
                    <div class="foot-note">默认不落库：bars 可复用，指标只在内存中演算；仅勾选“保存指标明细”时写入该临时表用于排查。</div>
                    <div style="margin: 10px 0 14px;">${detailStatusTag(status)}</div>
                    <div class="detail-list">
                        <div class="detail-item"><div class="detail-item-label">Collection</div><div class="detail-item-value mono">${escapeHtml(capture.collection || 'ibkr_backtest_indicators')}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Attempted</div><div class="detail-item-value">${escapeHtml(String(capture.attempted_count || run?.metrics?.backtest_indicator_count || 0))}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Saved</div><div class="detail-item-value">${escapeHtml(String(capture.saved_count || 0))}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Errors</div><div class="detail-item-value">${escapeHtml(String(capture.error_count || 0))}</div></div>
                    </div>
                    ${errors.length ? `<div class="note-box mono" style="margin-top: 14px;">${escapeHtml(JSON.stringify(errors, null, 2))}</div>` : '<div class="empty-state" style="margin-top: 14px;">暂无落库错误。</div>'}
                </div>
            `;
        }

        function buildBacktestReverseCaptureCard(run) {
            const capture = run?.extra?.backtest_reverse_capture || run?.metrics?.backtest_reverse_capture || {};
            const status = String(capture.status || 'empty').toLowerCase();
            const errors = Array.isArray(capture.errors) ? capture.errors : [];
            const metrics = run?.metrics || {};
            const samples = Array.isArray(metrics.backtest_reverse_samples) ? metrics.backtest_reverse_samples : [];
            return `
                <div class="detail-card">
                    <div class="subhead">反转验证</div>
                    <div class="foot-note">回测反转留痕，不回写 live。</div>
                    <div style="margin: 10px 0 14px;">${detailStatusTag(status)}</div>
                    <div class="detail-list">
                        <div class="detail-item"><div class="detail-item-label">Collection</div><div class="detail-item-value mono">${escapeHtml(capture.collection || 'ibkr_backtest_reverse_signals')}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Attempted</div><div class="detail-item-value">${escapeHtml(String(capture.attempted_count || metrics.backtest_reverse_signal_count || 0))}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Saved</div><div class="detail-item-value">${escapeHtml(String(capture.saved_count || 0))}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Errors</div><div class="detail-item-value">${escapeHtml(String(capture.error_count || 0))}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Action Breakdown</div><div class="detail-item-value">${escapeHtml(formatBreakdown(metrics.backtest_reverse_action_breakdown || {}))}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Strength Breakdown</div><div class="detail-item-value">${escapeHtml(formatBreakdown(metrics.backtest_reverse_strength_breakdown || {}))}</div></div>
                    </div>
                    ${samples.length ? `<div class="note-box mono" style="margin-top: 14px;">${escapeHtml(JSON.stringify(samples, null, 2))}</div>` : '<div class="empty-state" style="margin-top: 14px;">暂无反转动作。</div>'}
                    ${errors.length ? `<div class="note-box mono" style="margin-top: 14px;">${escapeHtml(JSON.stringify(errors, null, 2))}</div>` : ''}
                </div>
            `;
        }

        function buildRunAnalysisCard(run) {
            const report = run?.extra?.analysis_report || {};
            const summary = report.summary || run?.metrics?.analysis_summary || {};
            const recommendation = report.recommendation || {};
            const comparison = report.baseline_comparison || {};
            const strengths = Array.isArray(recommendation.strengths) ? recommendation.strengths : [];
            const risks = Array.isArray(recommendation.risks) ? recommendation.risks : [];
            const suggestions = Array.isArray(recommendation.suggestions) ? recommendation.suggestions : [];
            const verdict = String(summary.verdict || recommendation.verdict || 'mixed').toLowerCase();
            const baselineExists = Boolean(comparison.baseline_exists);
            const improved = Boolean(summary.improved_vs_baseline || comparison.improved_vs_baseline);
            const strengthsHtml = strengths.map((item) => escapeHtml(item)).join('<br>');
            const risksHtml = risks.map((item) => escapeHtml(item)).join('<br>');
            const suggestionsHtml = suggestions.map((item) => escapeHtml(item)).join('<br>');

            return `
                <div class="detail-card">
                    <div class="subhead">回测分析</div>
                    <div class="foot-note">系统会结合收益、回撤、成交率、反转动作和 TV 对齐结果，自动产出这期回测的判断与优化方向。</div>
                    <div style="margin: 10px 0 14px;">${detailStatusTag(verdict)}</div>
                    <div class="note-box">${escapeHtml(summary.headline || recommendation.headline || '当前 run 尚未产出分析摘要。')}</div>
                    <div class="detail-list" style="margin-top: 14px;">
                        <div class="detail-item"><div class="detail-item-label">Improved vs Baseline</div><div class="detail-item-value ${improved ? 'positive' : ''}">${escapeHtml(improved ? 'YES' : 'NO')}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Baseline Run</div><div class="detail-item-value mono">${escapeHtml(comparison.baseline_run_id || '--')}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Delta Return</div><div class="detail-item-value ${classForValue(comparison.delta_return_pct || 0)}">${baselineExists ? escapeHtml(formatPct(comparison.delta_return_pct || 0)) : '--'}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Delta Sharpe</div><div class="detail-item-value ${classForValue(comparison.delta_sharpe || 0)}">${baselineExists ? escapeHtml(formatNumber(comparison.delta_sharpe || 0, 2)) : '--'}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Scope Similarity</div><div class="detail-item-value">${baselineExists ? escapeHtml(`${formatNumber((comparison.scope_similarity?.overlap_ratio || 0) * 100, 0)}% overlap`) : '--'}</div></div>
                    </div>
                    ${strengths.length ? `<div class="note-box" style="margin-top: 14px;"><strong>优势</strong><br>${strengthsHtml}</div>` : ''}
                    ${risks.length ? `<div class="note-box" style="margin-top: 14px;"><strong>风险</strong><br>${risksHtml}</div>` : ''}
                    ${suggestions.length ? `<div class="note-box" style="margin-top: 14px;"><strong>优化建议</strong><br>${suggestionsHtml}</div>` : '<div class="empty-state" style="margin-top: 14px;">暂无优化建议。</div>'}
                </div>
            `;
        }

        function buildExperimentAnalysisCard(batch) {
            const analysis = batch?.extra?.experiment_analysis || {};
            const summary = analysis.summary || {};
            const comparison = analysis.comparison || {};
            const suggestions = Array.isArray(analysis.suggestions) ? analysis.suggestions : [];
            const improved = Boolean(summary.improved_vs_historical || comparison.improved_vs_historical);
            const suggestionsHtml = suggestions.map((item) => escapeHtml(item)).join('<br>');

            if (!batch) {
                return '';
            }

            return `
                <div class="detail-card">
                    <div class="subhead">Experiment Analysis</div>
                    <div class="foot-note">系统会在参数扫描完成后，对最佳变体、次优变体和同周期历史基准做对比，帮助判断是否值得替换当前候选方案。</div>
                    <div class="note-box" style="margin-top: 12px;">${escapeHtml(summary.headline || '当前 experiment 尚未产出分析结论。')}</div>
                    <div class="detail-list" style="margin-top: 14px;">
                        <div class="detail-item"><div class="detail-item-label">Best Variant</div><div class="detail-item-value">${escapeHtml(summary.best_variant_label || comparison.best_variant_label || '--')}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Improved vs Historical</div><div class="detail-item-value ${improved ? 'positive' : ''}">${escapeHtml(improved ? 'YES' : 'NO')}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Delta vs Historical</div><div class="detail-item-value ${classForValue(comparison.delta_vs_historical_return_pct || 0)}">${comparison.historical_baseline_run_id ? escapeHtml(formatPct(comparison.delta_vs_historical_return_pct || 0)) : '--'}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Delta vs 2nd Best</div><div class="detail-item-value ${classForValue(comparison.delta_vs_second_best_return_pct || 0)}">${comparison.delta_vs_second_best_return_pct !== undefined ? escapeHtml(formatPct(comparison.delta_vs_second_best_return_pct || 0)) : '--'}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Historical Baseline</div><div class="detail-item-value mono">${escapeHtml(comparison.historical_baseline_run_id || '--')}</div></div>
                    </div>
                    ${suggestions.length ? `<div class="note-box" style="margin-top: 14px;"><strong>优化方向</strong><br>${suggestionsHtml}</div>` : '<div class="empty-state" style="margin-top: 14px;">暂无优化方向。</div>'}
                </div>
            `;
        }

        function buildTvParityCard(run) {
            const tvParity = run?.extra?.tv_parity || {};
            const summary = tvParity.summary || run?.metrics?.tv_parity || {};
            const status = String(tvParity.status || summary.status || 'disabled').toLowerCase();
            const symbolRows = Array.isArray(tvParity.symbols) ? tvParity.symbols : [];
            const signalCompareEnabled = Boolean(
                summary.signal_compare_enabled !== undefined
                    ? summary.signal_compare_enabled
                    : tvParity.signal_compare_enabled
            );

            if (!summary.enabled && !tvParity.enabled) {
                return `
                    <div class="detail-card">
                        <div class="subhead">TV 对齐校验</div>
                        <div class="empty-state">未启用 TV 对齐校验。</div>
                    </div>
                `;
            }

            const sampleItems = [];
            symbolRows.forEach((item) => {
                [
                    ['indicators', 'indicator'],
                    ['signals', 'signal'],
                ].forEach(([key, label]) => {
                    const section = item?.[key] || {};
                    if (Array.isArray(section.mismatch_samples) && section.mismatch_samples.length) {
                        sampleItems.push({ symbol: item.symbol, type: `${label}_mismatch`, sample: section.mismatch_samples[0] });
                    }
                    if (Array.isArray(section.missing_in_tv_samples) && section.missing_in_tv_samples.length) {
                        sampleItems.push({ symbol: item.symbol, type: `${label}_missing_in_tv`, sample: section.missing_in_tv_samples[0] });
                    }
                    if (Array.isArray(section.missing_in_backtest_samples) && section.missing_in_backtest_samples.length) {
                        sampleItems.push({ symbol: item.symbol, type: `${label}_missing_in_backtest`, sample: section.missing_in_backtest_samples[0] });
                    }
                });
            });

            return `
                <div class="detail-card">
                    <div class="subhead">TV 对齐校验</div>
                    <div class="foot-note">${signalCompareEnabled
                        ? '这是回测阶段的临时 accuracy audit，只用于当前对齐验证，不会作为以后生产链路的硬阻断条件。'
                        : '当前只校验技术指标与 TV reference 的对齐，TV 信号不再作为误差来源。'}</div>
                    <div style="margin: 10px 0 14px;">${detailStatusTag(status)}</div>
                    <div class="detail-list">
                        <div class="detail-item"><div class="detail-item-label">Reference</div><div class="detail-item-value">${escapeHtml((tvParity.source_environment || run?.source_environment || '--').toUpperCase())} · ${escapeHtml(tvParity.interval || '5')}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Compared Symbols</div><div class="detail-item-value">${escapeHtml(String(summary.symbol_count || 0))}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Symbols With Ref</div><div class="detail-item-value">${escapeHtml(String(summary.symbols_with_reference || 0))}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Indicator Match</div><div class="detail-item-value">${escapeHtml(formatPct(summary.indicator_match_rate || 0))}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Signal Match</div><div class="detail-item-value">${signalCompareEnabled ? escapeHtml(formatPct(summary.signal_match_rate || 0)) : 'OFF'}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Indicator Rows</div><div class="detail-item-value">${escapeHtml(String(summary.indicator_matched_count || 0))} / ${escapeHtml(String(summary.indicator_generated_count || 0))} generated · TV ${escapeHtml(String(summary.indicator_tv_count || 0))}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Indicator Drift</div><div class="detail-item-value">${escapeHtml(String(summary.indicator_mismatch_count || 0))} mismatch · ${escapeHtml(String(summary.indicator_missing_in_tv_count || 0))} missing in TV · ${escapeHtml(String(summary.indicator_missing_in_backtest_count || 0))} missing in replay</div></div>
                        <div class="detail-item"><div class="detail-item-label">Signal Rows</div><div class="detail-item-value">${signalCompareEnabled ? `${escapeHtml(String(summary.signal_matched_count || 0))} / ${escapeHtml(String(summary.signal_generated_count || 0))} generated · TV ${escapeHtml(String(summary.signal_tv_count || 0))}` : 'disabled'}</div></div>
                        <div class="detail-item"><div class="detail-item-label">Signal Drift</div><div class="detail-item-value">${signalCompareEnabled ? `${escapeHtml(String(summary.signal_mismatch_count || 0))} mismatch · ${escapeHtml(String(summary.signal_missing_in_tv_count || 0))} missing in TV · ${escapeHtml(String(summary.signal_missing_in_backtest_count || 0))} missing in replay` : 'disabled'}</div></div>
                    </div>
                    ${symbolRows.length ? `
                        <div class="table-wrap" style="margin-top: 14px;">
                            <table class="data-table" style="min-width: 760px;">
                                <thead>
                                    <tr>
                                        <th>Symbol</th>
                                        <th>Status</th>
                                        <th>Indicators</th>
                                        <th>Signals</th>
                                        <th>Ref Error</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    ${symbolRows.map((item) => `
                                        <tr>
                                            <td class="mono">${escapeHtml(item.symbol || '--')}</td>
                                            <td>${detailStatusTag(item.status || '--')}</td>
                                            <td>
                                                gen ${escapeHtml(String(item?.indicators?.generated_count || 0))} / tv ${escapeHtml(String(item?.indicators?.tv_count || 0))}
                                                <br>match ${escapeHtml(String(item?.indicators?.matched_count || 0))} · drift ${escapeHtml(String((item?.indicators?.mismatch_count || 0) + (item?.indicators?.missing_in_tv_count || 0) + (item?.indicators?.missing_in_backtest_count || 0)))}
                                            </td>
                                            <td>
                                                ${signalCompareEnabled
                                                    ? `gen ${escapeHtml(String(item?.signals?.generated_count || 0))} / tv ${escapeHtml(String(item?.signals?.tv_count || 0))}
                                                <br>match ${escapeHtml(String(item?.signals?.matched_count || 0))} · drift ${escapeHtml(String((item?.signals?.mismatch_count || 0) + (item?.signals?.missing_in_tv_count || 0) + (item?.signals?.missing_in_backtest_count || 0)))}`
                                                    : 'disabled'}
                                            </td>
                                            <td class="mono">${escapeHtml(item.reference_error || '--')}</td>
                                        </tr>
                                    `).join('')}
                                </tbody>
                            </table>
                        </div>
                    ` : '<div class="empty-state" style="margin-top: 14px;">暂无可对齐 TV rows。</div>'}
                    <div class="subhead" style="margin-top: 16px;">Sample Drift</div>
                    ${sampleItems.length ? `<div class="note-box mono">${escapeHtml(JSON.stringify(sampleItems.slice(0, 8), null, 2))}</div>` : '<div class="empty-state">暂无 sample drift。</div>'}
                </div>
            `;
        }

        function getBatchVariantsText(batch) {
            const variants = batch?.params?.variants || [];
            return Array.isArray(variants) && variants.length ? JSON.stringify(variants, null, 2) : '[]';
        }

        function setTrackingHtml(elementId, html) {
            const element = document.getElementById(elementId);
            if (!element) return;
            if (element.innerHTML !== html) element.innerHTML = html;
        }

        function setTrackingText(elementId, text) {
            const element = document.getElementById(elementId);
            if (!element) return;
            const next = String(text ?? '');
            if (element.textContent !== next) element.textContent = next;
        }

        function syncTrackingDefaultFilters(model) {
            if (!model) return;
            if (trackingFilterRunId !== selectedRunId) {
                trackingFilterRunId = selectedRunId;
                trackingFilters.date = model.focus_date || model.dates?.[model.dates.length - 1] || '';
                trackingFilters.symbol = '';
                trackingFilters.eventType = '';
                trackingFilters.setup = '';
                return;
            }
            if (trackingFilters.date && !(model.dates || []).includes(trackingFilters.date)) {
                trackingFilters.date = model.focus_date || model.dates?.[model.dates.length - 1] || '';
            }
            if (trackingFilters.setup && !(model.setupLabels || []).includes(trackingFilters.setup)) {
                trackingFilters.setup = '';
            }
        }

        function renderTrackingOptions(items, selectedValue, allLabel) {
            const values = Array.isArray(items) ? items : [];
            return [
                `<option value="">${escapeHtml(allLabel)}</option>`,
                ...values.map((value) => {
                    const text = String(value || '');
                    return `<option value="${escapeHtml(text)}" ${text === selectedValue ? 'selected' : ''}>${escapeHtml(text)}</option>`;
                }),
            ].join('');
        }

        function renderTrackingControls(model) {
            const dateSelect = document.getElementById('trackingDateFilter');
            const symbolSelect = document.getElementById('trackingSymbolFilter');
            const eventTypeSelect = document.getElementById('trackingEventTypeFilter');
            const setupSelect = document.getElementById('trackingSetupFilter');
            if (!dateSelect || !symbolSelect || !eventTypeSelect) return;
            const dateHtml = renderTrackingOptions(model?.dates || [], trackingFilters.date, '全部日期');
            const symbolHtml = renderTrackingOptions(model?.symbols || [], trackingFilters.symbol, '全部标的');
            const eventHtml = renderTrackingOptions(model?.eventTypes || [], trackingFilters.eventType, '全部事件');
            const setupHtml = renderTrackingOptions(model?.setupLabels || [], trackingFilters.setup, '全部 Setup');
            if (dateSelect.innerHTML !== dateHtml) dateSelect.innerHTML = dateHtml;
            if (symbolSelect.innerHTML !== symbolHtml) symbolSelect.innerHTML = symbolHtml;
            if (eventTypeSelect.innerHTML !== eventHtml) eventTypeSelect.innerHTML = eventHtml;
            if (setupSelect && setupSelect.innerHTML !== setupHtml) setupSelect.innerHTML = setupHtml;
            if (dateSelect.value !== trackingFilters.date) dateSelect.value = trackingFilters.date;
            if (symbolSelect.value !== trackingFilters.symbol) symbolSelect.value = trackingFilters.symbol;
            if (eventTypeSelect.value !== trackingFilters.eventType) eventTypeSelect.value = trackingFilters.eventType;
            if (setupSelect && setupSelect.value !== trackingFilters.setup) setupSelect.value = trackingFilters.setup;
        }

        function getFilteredTrackingEvents(model) {
            const timeline = Array.isArray(model?.timeline) ? model.timeline : [];
            return timeline.filter((event) => {
                if (trackingFilters.date && getTrackingEventDate(event) !== trackingFilters.date) return false;
                if (trackingFilters.symbol && String(event.symbol || '') !== trackingFilters.symbol) return false;
                if (trackingFilters.eventType && String(event.event_type || '') !== trackingFilters.eventType) return false;
                if (trackingFilters.setup && getBacktestSetupFilterValue(event) !== trackingFilters.setup) return false;
                return true;
            });
        }

        function getFilteredTrackingFlows(model) {
            const flows = Array.isArray(model?.symbol_day_flows) ? model.symbol_day_flows : [];
            return flows.filter((flow) => {
                if (trackingFilters.date && String(flow.date || '') !== trackingFilters.date) return false;
                if (trackingFilters.symbol && String(flow.symbol || '') !== trackingFilters.symbol) return false;
                if (trackingFilters.eventType && !(flow.events || []).some((event) => String(event.event_type || '') === trackingFilters.eventType)) return false;
                if (trackingFilters.setup) {
                    return (flow.setup_labels || []).includes(trackingFilters.setup)
                        || (flow.events || []).some((event) => getBacktestSetupFilterValue(event) === trackingFilters.setup);
                }
                return true;
            });
        }

        function summarizeFilteredTrackingEvents(events) {
            const summary = {
                targets: 0,
                signals: 0,
                filled: 0,
                trades: 0,
                exits: 0,
                risk: 0,
                special: 0,
                takeProfit: 0,
                stopLoss: 0,
            };
            (events || []).forEach((event) => {
                const eventType = String(event.event_type || '');
                const stage = String(event.stage || '');
                const status = String(event.status || '');
                if (eventType === 'target_selected') summary.targets += 1;
                if (eventType === 'signal_generated') summary.signals += 1;
                if (eventType === 'entry_filled') summary.filled += 1;
                if (eventType === 'trade_opened') summary.trades += 1;
                if (eventType === 'trade_closed') {
                    summary.exits += 1;
                    if (status === 'take_profit') summary.takeProfit += 1;
                    if (status === 'stop_loss') summary.stopLoss += 1;
                }
                if (stage === 'risk') summary.risk += 1;
                if (stage === 'special') summary.special += 1;
            });
            return summary;
        }

        function renderTrackingSummary(model, filteredEvents) {
            const summary = summarizeFilteredTrackingEvents(filteredEvents);
            const modeLabel = model.audit_mode === 'native' ? 'Native audit' : 'Derived rows';
            const focusLabel = trackingFilters.date || model.focus_date || '--';
            const cards = [
                ['Mode', modeLabel, model.timeline_truncated ? 'timeline truncated' : `${formatNumber(model.event_count || filteredEvents.length || 0, 0)} events`],
                ['Focus Date', focusLabel, `${formatNumber((model.dates || []).length, 0)} trade dates`],
                ['Targets', String(summary.targets), `${formatNumber((model.focus_symbols || []).length, 0)} focus symbols`],
                ['Signal -> Fill', `${summary.signals} -> ${summary.filled}`, `${formatPct(summary.signals ? (summary.filled / summary.signals) * 100 : 0)} filled`],
                ['Trades / Exits', `${summary.trades} / ${summary.exits}`, `TP ${summary.takeProfit} · SL ${summary.stopLoss}`],
                ['Risk / Special', `${summary.risk} / ${summary.special}`, formatBreakdown(model.stage_counts || {})],
            ];
            setTrackingHtml('trackingSummaryGrid', cards.map(([label, value, subtext]) => `
                <div class="tracking-summary-card">
                    <div class="metric-label">${escapeHtml(label)}</div>
                    <div class="metric-value">${escapeHtml(value)}</div>
                    <div class="metric-subtext">${escapeHtml(subtext)}</div>
                </div>
            `).join(''));
        }

        function trackingStageClass(stage) {
            const value = String(stage || '').trim().toLowerCase();
            if (value === 'target') return 'target';
            if (value === 'signal') return 'signal';
            if (value === 'execution') return 'execution';
            if (value === 'risk') return 'risk';
            if (value === 'special') return 'special';
            if (value === 'exit') return 'exit';
            return 'muted';
        }

        function renderTrackingEventChips(events) {
            const items = (events || []).slice(0, 8);
            if (!items.length) return '<span class="tracking-chip muted">no events</span>';
            const chips = items.map((event) => `
                <span class="tracking-chip ${trackingStageClass(event.stage)}" title="${escapeHtml(event.reason || '')}">
                    ${escapeHtml(backtestAuditEventLabel(event))}
                </span>
            `).join('');
            const more = (events || []).length > items.length ? `<span class="tracking-chip muted">+${(events || []).length - items.length}</span>` : '';
            return `${chips}${more}`;
        }

        function getTrackingReplayBarMs(event) {
            return Number(
                event?.bar_time_ms
                || event?.entry_bar_ms
                || event?.exit_bar_ms
                || event?.details?.signal_bar_ms
                || event?.details?.confirm_ready_bar_ms
                || 0
            );
        }

        function buildTrackingLifecycleFlowUrl(flow, replayMs = 0) {
            const symbol = String(flow?.symbol || '').trim().toUpperCase();
            const runId = String(selectedRunId || selectedRun?.id || selectedRun?.run_id || '').trim();
            if (!symbol || !runId) return '';
            const barTimeMs = Number(replayMs || 0);
            const backtestDate = String(flow?.date || trackingFilters.date || selectedTrackingModel?.focus_date || selectedRun?.date_to || '').trim();
            const params = {
                mode: 'backtest',
                run_id: runId,
                backtest_run_id: runId,
                symbol,
                interval: '5m',
                backtest_date: backtestDate,
                date: backtestDate,
                trace: 1,
            };
            if (barTimeMs > 0) {
                const padMs = 90 * 60 * 1000;
                params.range = 'custom';
                params.bar_time_ms = Math.round(barTimeMs);
                params.start_ms = Math.max(0, Math.round(barTimeMs - padMs));
                params.end_ms = Math.round(barTimeMs + padMs);
            }
            return buildPageUrl('/ibkr_lifecycle_flow.html', params, { environment: 'backtest' });
        }

        function renderTrackingFlows(model) {
            const flows = getFilteredTrackingFlows(model);
            setTrackingText('trackingFlowCountLabel', `${flows.length} flows`);
            if (!selectedRun) {
                setTrackingHtml('trackingFlowsPanel', '<div class="empty-state">先选择一个 run。</div>');
                return;
            }
            if (!flows.length) {
                setTrackingHtml('trackingFlowsPanel', '<div class="empty-state">当前过滤条件下没有标的链路。</div>');
                return;
            }
            const pageModel = getBacktestClientPagination('trackingFlows', flows);
            const html = `
                ${renderBacktestClientPaginationBar('trackingFlows', flows)}
                <div class="tracking-flow-grid">
                    ${pageModel.pageRows.map((flow) => {
                        const firstEvent = (flow.events || [])[0] || {};
                        const replayMs = getTrackingReplayBarMs(firstEvent);
                        const lifecycleFlowUrl = buildTrackingLifecycleFlowUrl(flow, replayMs);
                        return `
                            <div class="tracking-flow-card ${flow.targeted ? 'targeted' : ''}">
                                <div class="tracking-flow-top">
                                    <div>
                                        <div class="tracking-flow-title mono">${escapeHtml(flow.symbol || '--')}</div>
                                        <div class="tracking-flow-copy mono">${escapeHtml(flow.date || '--')} · ${escapeHtml(String(flow.event_count || 0))} events</div>
                                    </div>
                                    ${flow.targeted ? '<span class="tag signal">TARGET</span>' : '<span class="tag">OFF PLAN</span>'}
                                </div>
                                <div class="tracking-flow-stats">
                                    <span>signals ${escapeHtml(String(flow.signal_count || 0))}</span>
                                    <span>fills ${escapeHtml(String(flow.executed_signal_count || 0))}</span>
                                    <span>trades ${escapeHtml(String(flow.trade_count || 0))}</span>
                                    <span>risk ${escapeHtml(String(flow.risk_adjustment_count || 0))}</span>
                                    <span>special ${escapeHtml(String(flow.special_event_count || 0))}</span>
                                    ${(flow.setup_labels || []).slice(0, 2).map((label) => `<span>setup ${escapeHtml(label)}</span>`).join('')}
                                </div>
                                <div class="tracking-flow-chain">${renderTrackingEventChips(flow.events || [])}</div>
                                <div class="tracking-flow-actions">
                                    <button class="btn ghost" type="button" onclick="replayTrackingEvent('${escapeHtml(flow.symbol || '')}', ${Number(replayMs || 0)})">Replay</button>
                                    <button class="btn ghost" type="button" onclick="openTradeChart('${escapeHtml(flow.symbol || '')}', ${Number(replayMs || 0)}, ${Number(replayMs || 0)})">主图</button>
                                    ${lifecycleFlowUrl ? `<a class="btn ghost" href="${lifecycleFlowUrl}">流程图</a>` : ''}
                                </div>
                            </div>
                        `;
                    }).join('')}
                </div>
            `;
            setTrackingHtml('trackingFlowsPanel', html);
        }

        function formatTrackingEventTime(event) {
            const usTime = String(event?.us_time || '').trim();
            if (usTime) return usTime.slice(0, 19);
            const barMs = Number(event?.bar_time_ms || 0);
            return barMs ? formatBarTimeMsToET(barMs) : '--';
        }

        function formatTrackingDetails(event) {
            const details = event?.details || {};
            const entries = Object.entries(details)
                .filter(([, value]) => value != null && value !== '' && !(Array.isArray(value) && !value.length))
                .slice(0, 4);
            if (!entries.length) return '';
            return entries.map(([key, value]) => `${key}=${Array.isArray(value) ? value.length : value}`).join(' · ');
        }

        function renderTrackingTimeline(model, filteredEvents) {
            setTrackingText('trackingEventCountLabel', `${filteredEvents.length} events`);
            if (!selectedRun) {
                setTrackingHtml('trackingTimelinePanel', '<div class="empty-state">先选择一个 run。</div>');
                return;
            }
            if (!filteredEvents.length) {
                const copy = trackingLoading ? '正在加载追踪数据 ...' : '当前过滤条件下没有事件。';
                setTrackingHtml('trackingTimelinePanel', `<div class="empty-state">${escapeHtml(copy)}</div>`);
                return;
            }
            const pageModel = getBacktestClientPagination('trackingTimeline', filteredEvents);
            const html = `
                ${renderBacktestClientPaginationBar('trackingTimeline', filteredEvents)}
                <div class="table-wrap tracking-table-wrap">
                    <table class="data-table tracking-table">
                        <thead>
                            <tr>
                                <th>Time</th>
                                <th>Symbol</th>
                                <th>Event</th>
                                <th>Status</th>
                                <th>Prices / SLTP</th>
                                <th>Signal</th>
                                <th>Reason / Details</th>
                                <th>Replay</th>
                            </tr>
                        </thead>
                        <tbody>
                            ${pageModel.pageRows.map((event) => {
                                const replayMs = getTrackingReplayBarMs(event);
                                return `
                                    <tr class="tracking-event-row ${trackingStageClass(event.stage)}">
                                        <td class="mono">${escapeHtml(formatTrackingEventTime(event))}</td>
                                        <td class="mono">${escapeHtml(event.symbol || '--')}${event.direction ? `<br><span class="tag ${event.direction === 'short' ? 'short' : 'long'}">${escapeHtml(event.direction.toUpperCase())}</span>` : ''}</td>
                                        <td>${escapeHtml(backtestAuditEventLabel(event))}<br><span class="tracking-stage mono">${escapeHtml(event.stage || '--')}</span></td>
                                        <td>${escapeHtml(event.status || '--')}</td>
                                        <td class="mono">${formatAuditPriceChange(event)}</td>
                                        <td class="mono">${escapeHtml(event.signal_id || event.signal || '--')}</td>
                                        <td>${escapeHtml(getBacktestSetupLabel(event) || '--')}${event.signal_mode ? `<br><span class="tracking-details mono">${escapeHtml(event.signal_mode)}</span>` : ''}</td>
                                        <td>${escapeHtml(event.reason || '--')}${formatTrackingDetails(event) ? `<br><span class="tracking-details mono">${escapeHtml(formatTrackingDetails(event))}</span>` : ''}</td>
                                        <td><button class="btn ghost" type="button" onclick="replayTrackingEvent('${escapeHtml(event.symbol || '')}', ${Number(replayMs || 0)})">回放</button></td>
                                    </tr>
                                `;
                            }).join('')}
                        </tbody>
                    </table>
                </div>
            `;
            setTrackingHtml('trackingTimelinePanel', html);
        }

        function renderTrackingModeBanner(model) {
            if (!model) return '';
            const modeCopy = model.audit_mode === 'native'
                ? '使用本次 run 写入的 metrics.backtest_audit，包含原生审计链路。'
                : '兼容旧 run：从 targets / signals / trades / reverse rows 推导链路，缺少的成交确认细节会用可用时间兜底。';
            const truncation = model.timeline_truncated || model.symbol_day_flow_truncated
                ? ' 当前 run 的原生审计摘要已截断，必要时按标的进入 Replay / 主图继续核对。'
                : '';
            const pagerHtml = model.audit_mode === 'native'
                ? ''
                : `
                    <div class="tracking-pager-grid">
                        ${renderBacktestRowPager('targets')}
                        ${renderBacktestRowPager('signals')}
                        ${renderBacktestRowPager('reverseSignals')}
                        ${renderBacktestRowPager('trades')}
                    </div>
                `;
            return `
                <div class="tracking-mode-banner ${model.audit_mode === 'native' ? 'native' : 'derived'}">${escapeHtml(modeCopy + truncation)}</div>
                ${pagerHtml}
            `;
        }

        function renderTracking() {
            const model = selectedTrackingModel;
            syncTrackingDefaultFilters(model);
            renderTrackingControls(model);
            if (!selectedRun) {
                setTrackingText('trackingRunLabel', 'No run');
                setTrackingText('trackingFlowCountLabel', '0 flows');
                setTrackingText('trackingEventCountLabel', '0 events');
                setTrackingHtml('trackingSummaryGrid', '<div class="empty-state">选择 run 后会显示每日标的、信号、成交、止盈止损和特殊事件链路。</div>');
                setTrackingHtml('trackingFlowsPanel', '<div class="empty-state">先选择一个 run。</div>');
                setTrackingHtml('trackingTimelinePanel', '<div class="empty-state">先选择一个 run。</div>');
                setTrackingHtml('trackingModeBanner', '');
                return;
            }
            setTrackingText('trackingRunLabel', selectedRun.name || selectedRun.id || 'Selected run');
            if (trackingLoading && !model) {
                setTrackingText('trackingFlowCountLabel', 'loading');
                setTrackingText('trackingEventCountLabel', 'loading');
                setTrackingHtml('trackingSummaryGrid', '<div class="empty-state">正在加载追踪数据 ...</div>');
                setTrackingHtml('trackingFlowsPanel', '<div class="empty-state">正在加载 signals / trades / reverse rows ...</div>');
                setTrackingHtml('trackingTimelinePanel', '<div class="empty-state">正在加载追踪时间线 ...</div>');
                setTrackingHtml('trackingModeBanner', '');
                return;
            }
            if (!model || (!model.enabled && !(model.timeline || []).length)) {
                setTrackingText('trackingFlowCountLabel', '0 flows');
                setTrackingText('trackingEventCountLabel', '0 events');
                setTrackingHtml('trackingSummaryGrid', '<div class="empty-state">这个 run 暂无可追踪数据；新 run 会写入 audit，旧 run 会尽量从关联 rows 推导。</div>');
                setTrackingHtml('trackingFlowsPanel', '<div class="empty-state">暂无标的链路。</div>');
                setTrackingHtml('trackingTimelinePanel', '<div class="empty-state">暂无事件时间线。</div>');
                setTrackingHtml('trackingModeBanner', '');
                return;
            }
            const filteredEvents = getFilteredTrackingEvents(model);
            setTrackingHtml('trackingModeBanner', renderTrackingModeBanner(model));
            renderTrackingSummary(model, filteredEvents);
            renderTrackingFlows(model);
            renderTrackingTimeline(model, filteredEvents);
        }

        function onTrackingFilterChange() {
            trackingFilters.date = String(document.getElementById('trackingDateFilter')?.value || '').trim();
            trackingFilters.symbol = String(document.getElementById('trackingSymbolFilter')?.value || '').trim();
            trackingFilters.eventType = String(document.getElementById('trackingEventTypeFilter')?.value || '').trim();
            trackingFilters.setup = String(document.getElementById('trackingSetupFilter')?.value || '').trim();
            backtestTableExpandedState.trackingTimeline = false;
            backtestTableExpandedState.trackingFlows = false;
            resetBacktestClientPagination('trackingTimeline');
            resetBacktestClientPagination('trackingFlows');
            renderTracking();
        }

        function resetTrackingFilters() {
            const model = selectedTrackingModel;
            trackingFilters.date = model?.focus_date || model?.dates?.[model.dates.length - 1] || '';
            trackingFilters.symbol = '';
            trackingFilters.eventType = '';
            trackingFilters.setup = '';
            backtestTableExpandedState.trackingTimeline = false;
            backtestTableExpandedState.trackingFlows = false;
            resetBacktestClientPagination('trackingTimeline');
            resetBacktestClientPagination('trackingFlows');
            renderTracking();
        }

        async function replayTrackingEvent(symbol, barTimeMs) {
            const safeSymbol = String(symbol || '').trim().toUpperCase();
            if (!safeSymbol) {
                showToast('缺少 symbol，无法回放');
                return;
            }
            setBacktestTab('trades');
            buildReplaySymbolOptions();
            document.getElementById('replaySymbol').value = safeSymbol || document.getElementById('replaySymbol').value;
            document.getElementById('replayCenterBar').value = String(Number(barTimeMs || 0));
            await loadReplayForSelection();
        }

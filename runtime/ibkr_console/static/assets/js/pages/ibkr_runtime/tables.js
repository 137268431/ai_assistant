        function renderEngineTable(status) {
            const engines = getSortedEngineEntries(status?.engines);
            const readySummary = `${status?.ready_engines || 0}/${status?.total_engines || 0} ready`;
            const environmentReadySummary = buildIbkrEngineEnvironmentReadySummary(status?.engines, {
                preferredOrder: [currentDataEnvironment],
            });
            const hasEngineSummaryOnly = Boolean(status?.engines_available) && !Boolean(status?.engines_included);
            document.getElementById('engineHint').textContent = hasEngineSummaryOnly
                ? `${readySummary}${environmentReadySummary ? ` · ${environmentReadySummary}` : ''} · loading detail`
                : `${readySummary}${environmentReadySummary ? ` · ${environmentReadySummary}` : ''}`;
            if (!engines.length) {
                const message = status?.engine_detail_error
                    ? `引擎明细加载失败：${status.engine_detail_error}`
                    : (hasEngineSummaryOnly ? '引擎明细加载中...' : '当前没有预热引擎');
                document.getElementById('engineTable').innerHTML = renderEmpty(message);
                return;
            }
            const rows = engines.slice(0, 20).map(([key, engine]) => {
                const model = getIbkrEngineViewModel(key, engine, currentDataEnvironment);
                return `
                    <tr>
                        <td class="mono">${escapeHtml(model.key)}</td>
                        <td>${model.barCount}</td>
                        <td><span class="pill ${model.ready ? 'pill-ok' : 'pill-pending'}">${escapeHtml(model.readyLabel)}</span></td>
                        <td>${escapeHtml(model.lastCloseLabel)}</td>
                        <td class="mono">${escapeHtml(model.lastBarLabel)}</td>
                    </tr>
                `;
            }).join('');
            document.getElementById('engineTable').innerHTML = `
                <table class="data-table">
                    <thead>
                        <tr><th>ENV / SYMBOL / TF</th><th>BARS</th><th>READY</th><th>LAST CLOSE</th><th>LAST BAR</th></tr>
                    </thead>
                    <tbody>${rows}</tbody>
                </table>
            `;
        }

        async function loadEngineDetail(loadId, fallbackStatus) {
            const baseStatus = fallbackStatus && typeof fallbackStatus === 'object' ? fallbackStatus : {};
            if (baseStatus.engines_included || Number(baseStatus.total_engines || 0) <= 0) {
                renderEngineTable(baseStatus);
                return;
            }

            try {
                const fullStatus = await requestIbkrEnvironmentJson('/api/custom/ibkr/statusz?full=1', currentBrokerMode, { retryAttempts: 3 });
                if (loadId !== latestRuntimeLoadId) return;
                renderEngineTable(fullStatus);
            } catch (error) {
                if (loadId !== latestRuntimeLoadId) return;
                renderEngineTable({
                    ...baseStatus,
                    engine_detail_error: error.message || String(error)
                });
            }
        }

        function renderBarsTable(items, options = {}) {
            if (!items.length) {
                document.getElementById('barsTable').innerHTML = renderEmpty(options.loading ? '正在加载最近 bars...' : '暂无 bars 数据');
                return;
            }
            document.getElementById('barsTable').innerHTML = `
                <table class="data-table">
                    <thead>
                        <tr><th>BAR</th><th>WRITE</th><th>SYMBOL</th><th>TF</th><th>CLOSE</th><th>SRC</th></tr>
                    </thead>
                    <tbody>
                        ${items.map((item) => `
                            <tr>
                                <td class="mono">${item.bar_time_ms ? formatBarTimeMsToET(item.bar_time_ms) : escapeHtml(item.us_time || '--')}</td>
                                <td class="mono">${escapeHtml(String(getComputedTimeLabel(item)).slice(0, 19))}</td>
                                <td>${escapeHtml(item.symbol || '--')}</td>
                                <td>${escapeHtml(formatIbkrIntervalLabel(item.interval))}</td>
                                <td>${formatMoney(item.close)}</td>
                                <td>${escapeHtml(getExtraObject(item).source || '--')}</td>
                            </tr>
                        `).join('')}
                    </tbody>
                </table>
            `;
        }

        function renderIndicatorsTable(items, options = {}) {
            if (!items.length) {
                document.getElementById('indicatorsTable').innerHTML = renderEmpty(options.loading ? '正在加载最近指标...' : '暂无最近指标');
                return;
            }
            document.getElementById('indicatorsTable').innerHTML = `
                <table class="data-table">
                    <thead>
                        <tr><th>CALC</th><th>SYMBOL</th><th>TF</th><th>BAR</th><th>CLOSE</th><th>SCRIPT</th></tr>
                    </thead>
                    <tbody>
                        ${items.map((item) => {
                            const extra = getExtraObject(item);
                            const close = extra.close != null ? formatMoney(extra.close) : '--';
                            return `
                                <tr>
                                    <td class="mono">${escapeHtml(String(getComputedTimeLabel(item)).slice(0, 19))}</td>
                                    <td>${escapeHtml(item.symbol || '--')}</td>
                                    <td>${escapeHtml(formatIbkrIntervalLabel(item.interval || extra.chart_tf))}</td>
                                    <td class="mono">${escapeHtml(getRecordBarLabel(item))}</td>
                                    <td>${close}</td>
                                    <td>${escapeHtml(item.script_tag || extra.script_tag || '--')}</td>
                                </tr>
                            `;
                        }).join('')}
                    </tbody>
                </table>
            `;
        }

        function renderSignalsTable(items) {
            if (!items.length) {
                document.getElementById('signalsTable').innerHTML = renderEmpty('暂无最近信号');
                return;
            }
            document.getElementById('signalsTable').innerHTML = `
                <table class="data-table">
                    <thead>
                        <tr><th>TIME</th><th>SYMBOL</th><th>DIRECTION</th><th>STATUS</th><th>ENTRY</th><th>RR</th></tr>
                    </thead>
                    <tbody>
                        ${items.map((item) => {
                            const direction = String(item.direction || '').toLowerCase();
                            const status = String(item.status || '').toLowerCase();
                            return `
                                <tr>
                                    <td class="mono">${escapeHtml((item.us_time || item.created || '--').slice(0, 19))}</td>
                                    <td>${escapeHtml(item.symbol || '--')}</td>
                                    <td><span class="pill ${direction === 'short' ? 'pill-short' : 'pill-long'}">${escapeHtml((item.direction || '--').toUpperCase())}</span></td>
                                    <td><span class="pill ${statusClass(status)}">${escapeHtml(formatRuntimeSignalStatus(item.status))}</span></td>
                                    <td>${formatMoney(item.entry)}</td>
                                    <td>${escapeHtml(String(item.rr || '--'))}</td>
                                </tr>
                            `;
                        }).join('')}
                    </tbody>
                </table>
            `;
        }

        function renderOrdersTable(items) {
            if (!items.length) {
                document.getElementById('ordersTable').innerHTML = renderEmpty('暂无最近订单');
                return;
            }
            document.getElementById('ordersTable').innerHTML = `
                <table class="data-table">
                    <thead>
                        <tr><th>TIME</th><th>SYMBOL</th><th>ROLE</th><th>STATUS</th><th>QTY</th><th>PRICE</th></tr>
                    </thead>
                    <tbody>
                        ${items.map((item) => {
                            const status = String(item.status || '').toLowerCase();
                            const direction = String(item.direction || '').toLowerCase();
                            return `
                                <tr>
                                    <td class="mono">${escapeHtml((item.us_time || item.created || '--').slice(0, 19))}</td>
                                    <td>${escapeHtml(item.symbol || '--')}</td>
                                    <td><span class="pill ${direction === 'short' ? 'pill-short' : 'pill-long'}">${escapeHtml(item.role || item.order_type || '--')}</span></td>
                                    <td><span class="pill ${statusClass(status)}">${escapeHtml(item.status || '--')}</span></td>
                                    <td>${escapeHtml(String(item.quantity || 0))}</td>
                                    <td>${item.fill_price ? formatMoney(item.fill_price) : formatMoney(item.limit_price)}</td>
                                </tr>
                            `;
                        }).join('')}
                    </tbody>
                </table>
            `;
        }

        function renderEventsTable(items) {
            if (!items.length) {
                document.getElementById('eventsTable').innerHTML = renderEmpty('暂无系统事件');
                return;
            }
            document.getElementById('eventsTable').innerHTML = `
                <table class="data-table">
                    <thead>
                        <tr><th>TIME</th><th>LEVEL</th><th>SOURCE</th><th>TITLE</th></tr>
                    </thead>
                    <tbody>
                        ${items.map((item) => `
                            <tr>
                                <td class="mono">${escapeHtml((item.us_time || item.created || '--').slice(0, 19))}</td>
                                <td><span class="pill ${statusClass(String(item.level || '').toLowerCase())}">${escapeHtml(item.level || '--')}</span></td>
                                <td>${escapeHtml(item.source || '--')}</td>
                                <td style="white-space:normal">${escapeHtml(item.title || '--')}</td>
                            </tr>
                        `).join('')}
                    </tbody>
                </table>
            `;
        }

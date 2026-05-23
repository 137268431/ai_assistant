        function buildTraceToken(text, className = '') {
            return `<span class="trace-token ${escapeHtml(className)}">${escapeHtml(text || '--')}</span>`;
        }
        function getTraceRows(payload) {
            return Array.isArray(payload?.traceTimeline) ? payload.traceTimeline : [];
        }
        function getTracePanelTotalPages(traceRows) {
            const totalRows = Array.isArray(traceRows) ? traceRows.length : 0;
            return Math.max(1, Math.ceil(totalRows / TRACE_PANEL_PAGE_SIZE));
        }
        function clampTracePanelPage(page, totalPages) {
            return Math.min(Math.max(Math.round(Number(page) || 1), 1), Math.max(1, Number(totalPages) || 1));
        }
        function findTracePanelPageForBar(payload, barTimeMs) {
            const traceRows = getTraceRows(payload);
            const target = Number(barTimeMs || 0);
            if (!traceRows.length || !target) return tracePanelPage;
            const rowIndex = traceRows.findIndex((item) => Number(item?.bar_time_ms || 0) === target);
            if (rowIndex < 0) return tracePanelPage;
            return clampTracePanelPage(Math.floor(rowIndex / TRACE_PANEL_PAGE_SIZE) + 1, getTracePanelTotalPages(traceRows));
        }
        function getCommittedTraceFocusContext(payload) {
            const bars = Array.isArray(payload?.bars) ? payload.bars : [];
            if (!bars.length) return null;
            const index = selectedBarIndex >= 0 ? clampIndex(selectedBarIndex, bars.length) : bars.length - 1;
            return buildContext(payload, index, selectedSignalId);
        }
        function clearTraceManualPage() {
            tracePanelManualPage = false;
        }
        function syncTracePanelPageToFocus(payload) {
            if (tracePanelManualPage) return;
            const bars = Array.isArray(payload?.bars) ? payload.bars : [];
            if (!bars.length) {
                tracePanelPage = 1;
                return;
            }
            const focus = getCommittedTraceFocusContext(payload);
            if (!focus?.bar?.bar_time_ms) return;
            tracePanelPage = findTracePanelPageForBar(payload, focus.bar.bar_time_ms);
        }
        function buildTraceTokens(tokens, className = '') {
            return (Array.isArray(tokens) ? tokens : [])
                .filter((item) => item !== null && item !== undefined && String(typeof item === 'object' ? item.text : item).trim())
                .map((item) => {
                    if (item && typeof item === 'object') {
                        return buildTraceToken(item.text, item.className || className);
                    }
                    return buildTraceToken(item, className);
                })
                .join('');
        }
        function buildTraceReviewSection(label, tokenHtml) {
            return `
                <div class="trace-review-section">
                    <div class="trace-review-section-label">${escapeHtml(label)}</div>
                    <div class="trace-token-row">${tokenHtml || buildTraceToken('--')}</div>
                </div>
            `;
        }
        function buildTracePaginationControls({ page, totalPages, totalRows, startIndex, endIndex }) {
            if (!totalRows) return '';
            const pages = [];
            if (totalPages <= 7) {
                for (let i = 1; i <= totalPages; i += 1) pages.push(i);
            } else {
                pages.push(1);
                if (page > 3) pages.push('left');
                for (let i = Math.max(2, page - 1); i <= Math.min(totalPages - 1, page + 1); i += 1) {
                    pages.push(i);
                }
                if (page < totalPages - 2) pages.push('right');
                pages.push(totalPages);
            }
            const pageButtons = pages.map((item) => {
                if (typeof item !== 'number') return '<span class="trace-page-ellipsis">...</span>';
                const active = item === page;
                return `<button class="trace-page-btn ${active ? 'active' : ''}" type="button" onclick="setTracePanelPage(${item}, event)" ${active ? 'aria-current="page"' : ''}>${item}</button>`;
            }).join('');
            return `
                <div class="trace-pagination-bar">
                    <div class="trace-pagination-status">${escapeHtml(`${startIndex + 1}-${endIndex} / ${totalRows}`)}</div>
                    <div class="trace-pagination-pages">
                        <button class="trace-page-btn" type="button" onclick="setTracePanelPage(${page - 1}, event)" ${page <= 1 ? 'disabled' : ''}>Prev</button>
                        ${pageButtons}
                        <button class="trace-page-btn" type="button" onclick="setTracePanelPage(${page + 1}, event)" ${page >= totalPages ? 'disabled' : ''}>Next</button>
                    </div>
                </div>
            `;
        }
        function buildTracePanelBody(payload, { embedded = false } = {}) {
            const traceRows = getTraceRows(payload);
            const bars = Array.isArray(payload?.bars) ? payload.bars : [];
            const focus = bars.length ? getCommittedTraceFocusContext(payload) : null;
            const activeBarMs = Number(focus?.bar?.bar_time_ms || 0) || 0;
            const totalRows = traceRows.length;
            const totalPages = getTracePanelTotalPages(traceRows);
            const page = clampTracePanelPage(tracePanelPage, totalPages);
            tracePanelPage = page;
            const startIndex = totalRows ? (page - 1) * TRACE_PANEL_PAGE_SIZE : 0;
            const endIndex = totalRows ? Math.min(totalRows, startIndex + TRACE_PANEL_PAGE_SIZE) : 0;
            if (!traceRows.length) {
                return {
                    chips: [
                        buildTraceToken(`Trace ${chartTracePanelOpen ? 'On' : 'Off'}`),
                    ],
                    listHtml: '<div class="trace-empty">当前窗口暂无可展示的 trace 记录。</div>',
                    page,
                    totalPages,
                    totalRows,
                    startIndex,
                    endIndex,
                };
            }
            const candidateCount = traceRows.filter((item) => getTraceStage(item) === 'candidate').length;
            const blockedCount = traceRows.filter((item) => getTraceStage(item) === 'blocked').length;
            const confirmedCount = traceRows.filter((item) => getTraceStage(item) === 'confirmed').length;
            const chips = [
                buildTraceToken(`${traceRows.length} bars`),
                buildTraceToken(`${confirmedCount} confirmed`, confirmedCount ? 'positive' : ''),
                buildTraceToken(`${candidateCount} candidate`, candidateCount ? 'warning' : ''),
                buildTraceToken(`${blockedCount} blocked`, blockedCount ? 'negative' : ''),
                focus?.trace?.is_preview || focus?.isPreviewBar ? buildTraceToken('当前焦点含预估 bar', 'preview') : '',
            ].filter(Boolean);
            const pageRows = traceRows.slice(startIndex, endIndex);
            const itemsHtml = pageRows.map((item) => {
                const barTimeMs = Number(item?.bar_time_ms || 0) || 0;
                const stage = getTraceStage(item);
                const active = activeBarMs > 0 && activeBarMs === barTimeMs;
                const isPreview = Boolean(item?.is_preview);
                const eventChain = Array.isArray(item?.event_chain) ? item.event_chain.filter(Boolean) : [];
                const signalState = getTraceSignalState(item);
                const traceSignal = getTraceSignalPayload(item) || {};
                const filters = normalizeCheckList(signalState.filter_checks || traceSignal.filter_checks || item?.filter_checks).concat(
                    Array.isArray(item?.filters) ? item.filters.filter(Boolean) : []
                );
                const triggers = normalizeCheckList(signalState.trigger_checks || traceSignal.trigger_checks || item?.trigger_checks);
                const flowTokens = getTraceFlowTokens(item, 8);
                const hasKeyEvent = Boolean(stage && stage !== 'none') || eventChain.length || filters.length || hasTraceComponentFlags(item?.component_flags);
                const itemClasses = [
                    'trace-review-item',
                    active ? 'active' : '',
                    isPreview ? 'preview' : '',
                    hasKeyEvent ? '' : 'quiet',
                    stage ? `stage-${stage}` : '',
                ].filter(Boolean).join(' ');
                const structure = [
                    item?.structure?.ema_bullish ? 'EMA 多头' : item?.structure?.ema_bearish ? 'EMA 空头' : 'EMA 中性',
                    item?.structure?.dtp_phase ? `DTP ${item.structure.dtp_phase}` : '',
                    ...(Array.isArray(item?.structure?.fractal_tokens) ? item.structure.fractal_tokens : []),
                ].filter(Boolean);
                const touchTokens = Array.isArray(item?.structure?.touch_tokens) ? item.structure.touch_tokens : [];
                const tech = [
                    `VWAP ${formatPercent(item?.position?.vwap_dist)}`,
                    `SD ${getSdRegimeText(item?.position?.sd_regime ?? item?.sd_regime)} · Z ${formatOptionalNumber(item?.position?.sd_close_z ?? item?.sd_close_z)}`,
                    `WRank ${formatOptionalNumber(item?.position?.sd_width_rank ?? item?.sd_width_rank)}`,
                    `ORB ${getOrbBreakoutText({ ...(item || {}), ...(item?.position || {}) })}`,
                    `RVOL20 ${formatOptionalNumber(item?.volume?.rvol_20 ?? item?.rvol_20)}`,
                    `ATR% ${formatPercent(item?.volatility?.atr_pct)}`,
                ].filter(Boolean);
                const signalMeta = [
                    signalState.strategy_profile || traceSignal.strategy_profile ? `Profile ${humanizeToken(signalState.strategy_profile || traceSignal.strategy_profile)}` : '',
                    signalState.setup || traceSignal.setup ? `Setup ${humanizeToken(signalState.setup || traceSignal.setup)}` : '',
                    signalState.entry_order_type || traceSignal.entry_order_type ? `Order ${getOrderTypeText(signalState.entry_order_type || traceSignal.entry_order_type)}` : '',
                    signalState.validity_minutes || traceSignal.validity_minutes ? `Valid ${signalState.validity_minutes || traceSignal.validity_minutes}m` : '',
                ].filter(Boolean);
                const technicalDescription = signalState.technical_description || traceSignal.technical_description || item?.technical_description || '';
                const divergence = Array.isArray(item?.momentum?.divergence_tokens) ? item.momentum.divergence_tokens.slice(0, 4) : [];
                const eventTokens = eventChain.length ? eventChain.slice(0, embedded ? 2 : 4) : ['无新增事件'];
                const filterTokens = filters.length ? filters.slice(0, embedded ? 2 : 3) : ['过滤检查通过/未提供'];
                const triggerTokens = triggers.length ? triggers.slice(0, embedded ? 2 : 3) : ['触发检查未提供'];
                const signalLabel = formatTraceDecisionLabel(item, null);
                return `
                    <button class="${escapeHtml(itemClasses)}" type="button" data-trace-bar-ms="${barTimeMs}" onclick="focusTraceBar('${barTimeMs}')">
                        <div class="trace-review-head">
                            <div>
                                <div class="trace-review-time">${escapeHtml(item?.us_time || '--')}</div>
                                <div class="trace-review-sub">ET${isPreview ? ' · 预估' : ''}</div>
                            </div>
                            <div class="trace-review-price">
                                ${escapeHtml(formatPrice(item?.close))}
                                <span>bar #${escapeHtml(String(item?.bar_index || '--'))}</span>
                            </div>
                        </div>
                        <div class="trace-review-primary">
                            ${buildTraceToken(getTraceStageLabel(stage), getTraceStageBadgeClass(stage))}
                            ${buildTraceToken(signalLabel, getTraceStageBadgeClass(stage))}
                            ${isPreview ? buildTraceToken('预估', 'preview') : ''}
                        </div>
                        <div class="trace-review-grid">
                            ${buildTraceReviewSection('Structure', buildTraceTokens([
                                ...structure,
                                ...touchTokens.map((text) => ({ text, className: 'warning' })),
                            ]))}
                            ${buildTraceReviewSection('Setup', buildTraceTokens(signalMeta.length ? signalMeta : ['旧 trace 未提供 setup']))}
                            ${buildTraceReviewSection('Flow', buildTraceTokens(flowTokens.length ? flowTokens : ['无组件'], flowTokens.length ? 'warning' : ''))}
                            ${buildTraceReviewSection('Triggers', buildTraceTokens(triggerTokens, triggers.length ? 'positive' : ''))}
                            ${buildTraceReviewSection('Filters', buildTraceTokens(filterTokens, filters.length ? 'negative' : ''))}
                            ${buildTraceReviewSection('Tech', buildTraceTokens(tech))}
                            ${buildTraceReviewSection('Divergence', buildTraceTokens(divergence.length ? divergence : ['无背离']))}
                        </div>
                        <div class="trace-review-reason">${escapeHtml(technicalDescription || item?.signal_state?.reason || item?.signal_state?.filter_reason || 'bars 实时推演')}</div>
                    </button>
                `;
            }).join('');
            const paginationHtml = buildTracePaginationControls({ page, totalPages, totalRows, startIndex, endIndex });
            return {
                chips,
                listHtml: `
                    <div class="trace-review-shell">
                        ${paginationHtml}
                        <div class="trace-review-list">${itemsHtml}</div>
                        ${paginationHtml}
                    </div>
                `,
                page,
                totalPages,
                totalRows,
                startIndex,
                endIndex,
            };
        }
        function renderTracePanel(payload = getChartDisplayPayload(), { syncToFocus = true } = {}) {
            const shell = document.getElementById('tracePanelShell');
            if (!shell) return;
            const hidden = !chartTracePanelOpen;
            shell.classList.toggle('hidden', hidden);
            if (hidden) {
                shell.innerHTML = '';
                return;
            }
            if (syncToFocus) {
                syncTracePanelPageToFocus(payload);
            }
            const body = buildTracePanelBody(payload);
            shell.innerHTML = `
                <div class="trace-panel-head">
                    <div>
                        <div class="trace-panel-title">Bar Trace</div>
                        <div class="trace-panel-copy">按 bar 逐条复盘结构、setup、触发检查、过滤检查与技术描述；关键事件优先突出，普通 bar 降低视觉权重。</div>
                    </div>
                    <div class="trace-panel-actions">
                        <button class="tv-tool-btn" type="button" onclick="toggleChartTracePanel()">隐藏 Trace</button>
                        <button class="tv-tool-btn" type="button" onclick="focusLatestChartBar()">回到最新</button>
                    </div>
                </div>
                <div class="trace-panel-summary">${body.chips.join('')}</div>
                ${body.listHtml}
            `;
        }
        window.toggleChartTracePanel = function() {
            chartTracePanelOpen = !chartTracePanelOpen;
            clearTraceManualPage();
            saveChartUiPrefs();
            renderTracePanel(getChartDisplayPayload());
            renderChartToolbar(getChartDisplayPayload());
            renderMobileDock(getChartDisplayPayload());
            updateQueryState();
        };
        window.setTracePanelPage = function(page, event = null) {
            if (event?.preventDefault) event.preventDefault();
            if (event?.stopPropagation) event.stopPropagation();
            const payload = getChartDisplayPayload();
            const totalPages = getTracePanelTotalPages(getTraceRows(payload));
            tracePanelPage = clampTracePanelPage(page, totalPages);
            tracePanelManualPage = true;
            renderTracePanel(payload, { syncToFocus: false });
        };
        window.focusTraceBar = function(barTimeMs) {
            const payload = getChartDisplayPayload();
            const bars = Array.isArray(payload?.bars) ? payload.bars : [];
            const targetIndex = findBarIndexByTime(bars, Number(barTimeMs || 0));
            if (targetIndex < 0) return;
            clearTraceManualPage();
            focusBarIndex(targetIndex);
            if (chartTracePanelOpen) {
                tracePanelPage = findTracePanelPageForBar(payload, barTimeMs);
                renderTracePanel(payload, { syncToFocus: false });
            }
        };

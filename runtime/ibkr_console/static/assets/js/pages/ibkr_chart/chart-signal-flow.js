        function buildSignalScatter(signals, bars, priceResolver, color, labelBuilder = null) {
            const indexByMs = new Map(bars.map((bar, index) => [Number(bar.bar_time_ms || 0), index]));
            return signals.map((signal) => {
                const barMs = Number(signal.bar_time_ms || 0);
                const xIndex = indexByMs.get(barMs);
                if (xIndex === undefined) return null;
                const yValue = Number(priceResolver(signal) || 0);
                if (!Number.isFinite(yValue) || yValue <= 0) return null;
                return {
                    value: [xIndex, yValue],
                    itemStyle: { color },
                    name: buildTradeSignalLabel(signal),
                    signal_id: signal.signal_id || '',
                    bar_index: xIndex,
                    bar_time_ms: barMs,
                    labelText: typeof labelBuilder === 'function' ? String(labelBuilder(signal) || '') : '',
                };
            }).filter(Boolean);
        }

        function formatBacktestEventLabel(event) {
            const type = String(event?.event_type || '').trim().toLowerCase();
            const direction = String(event?.direction || '').trim().toLowerCase();
            if (type === 'entry_filled') return direction === 'short' ? 'BT Sell' : 'BT Buy';
            if (type === 'exit_take_profit') return 'BT TP';
            if (type === 'exit_stop_loss') return 'BT SL';
            if (type === 'exit_reverse') return 'BT Rev';
            if (type === 'exit_eod') return 'BT EOD';
            if (type === 'exit_last_bar') return 'BT Exit';
            if (type.startsWith('signal_')) return 'BT Sig';
            return 'BT';
        }

        function formatTvEventLabel(event) {
            const type = String(event?.event_type || '').trim().toLowerCase();
            const direction = String(event?.direction || '').trim().toLowerCase();
            if (type === 'pre_alert') return direction === 'short' ? 'TV pre_alert 空' : 'TV pre_alert 多';
            if (type === 'entry') return direction === 'short' ? 'TV Entry 空' : 'TV Entry 多';
            if (type === 'risk_update') return 'TV Risk';
            if (type === 'exit') return 'TV Exit';
            return 'TV';
        }

        function formatOrderEventLabel(event) {
            const type = String(event?.event_type || '').trim().toLowerCase();
            const direction = String(event?.direction || '').trim().toLowerCase();
            const pnl = Number(event?.pnl);
            const pnlText = Number.isFinite(pnl) ? ` ${pnl > 0 ? '+' : pnl < 0 ? '-' : ''}$${Math.abs(pnl).toFixed(0)}` : '';
            if (type === 'live_entry') return direction === 'short' ? 'Live Sell' : 'Live Buy';
            if (type === 'live_exit_tp') return `Live TP${pnlText}`;
            if (type === 'live_exit_sl') return `Live SL${pnlText}`;
            if (type === 'live_exit_eod') return `EOD Exit${pnlText}`;
            if (type.startsWith('live_exit')) return `Live Exit${pnlText}`;
            return 'Order';
        }

        function buildBacktestEventScatter(events, bars, color, predicate, yResolver, labelBuilder = formatBacktestEventLabel) {
            const indexByMs = new Map(bars.map((bar, index) => [Number(bar.bar_time_ms || 0), index]));
            return (Array.isArray(events) ? events : []).map((event) => {
                if (typeof predicate === 'function' && !predicate(event)) return null;
                const barMs = Number(event?.bar_time_ms || 0);
                const xIndex = indexByMs.get(barMs);
                if (xIndex === undefined) return null;
                const bar = bars[xIndex] || {};
                const yValue = Number((typeof yResolver === 'function' ? yResolver(event, bar) : event?.price) || 0);
                if (!Number.isFinite(yValue) || yValue <= 0) return null;
                return {
                    value: [xIndex, yValue],
                    itemStyle: { color },
                    name: labelBuilder(event),
                    signal_id: event.signal_id || '',
                    bar_index: xIndex,
                    bar_time_ms: barMs,
                    labelText: labelBuilder(event),
                    event_type: event.event_type || '',
                    event_source: event.source || '',
                    backtest_event_type: event.event_type || '',
                    pnl: event.pnl ?? null,
                    pnl_pct: event.pnl_pct ?? null,
                    reason: event.reason || event.exit_reason || '',
                };
            }).filter(Boolean);
        }

        function buildIndicatorMarkerPoints(bars, indicatorMap, predicate, yResolver, labelBuilder) {
            return bars.map((bar, index) => {
                const indicator = indicatorMap.get(Number(bar?.bar_time_ms || 0));
                if (!indicator || !predicate(indicator, bar, index)) return null;
                const yValue = Number(yResolver(indicator, bar, index));
                if (!Number.isFinite(yValue) || yValue <= 0) return null;
                return {
                    value: [index, yValue],
                    bar_index: index,
                    bar_time_ms: Number(bar?.bar_time_ms || 0),
                    labelText: typeof labelBuilder === 'function' ? String(labelBuilder(indicator, bar, index) || '') : '',
                };
            }).filter(Boolean);
        }

        function hasTraceComponentFlags(flags) {
            if (!flags || typeof flags !== 'object') return false;
            return Object.keys(flags).some((key) => Boolean(flags[key]));
        }

        function getTraceComponentTokens(trace) {
            const flags = trace?.component_flags && typeof trace.component_flags === 'object' ? trace.component_flags : {};
            const tokens = [];
            if (flags.sd_upper_bull_touch_seen) tokens.push('上轨E多');
            if (flags.sd_lower_bear_touch_seen) tokens.push('下轨E空');
            if (flags.sd_upper_bull_fractal_seen) tokens.push('上F多');
            if (flags.sd_lower_bull_fractal_seen) tokens.push('下F多');
            if (flags.sd_upper_bear_fractal_seen) tokens.push('上F空');
            if (flags.sd_lower_bear_fractal_seen) tokens.push('下F空');
            if (flags.bull_crsi_div_seen) tokens.push('c多背');
            if (flags.bull_obv_div_seen) tokens.push('o多背');
            if (flags.bear_crsi_div_seen) tokens.push('c空背');
            if (flags.bear_obv_div_seen) tokens.push('o空背');
            if (flags.buy_raw) tokens.push('多候选');
            if (flags.sell_raw) tokens.push('空候选');
            return tokens;
        }

        function getTraceEventTokens(trace) {
            const events = Array.isArray(trace?.event_chain) ? trace.event_chain : [];
            const tokens = [];
            events.forEach((event) => {
                const text = String(event || '');
                if (/SD下轨触发/.test(text)) tokens.push('开下轨');
                if (/SD上轨触发/.test(text)) tokens.push('开上轨');
                if (/EMA 多头触及/.test(text)) tokens.push('E多');
                if (/EMA 空头触及/.test(text)) tokens.push('E空');
                if (/多头分形/.test(text)) tokens.push('F多');
                if (/空头分形/.test(text)) tokens.push('F空');
                if (/cRSI 多头背离/.test(text)) tokens.push('c多背');
                if (/cRSI 空头背离/.test(text)) tokens.push('c空背');
                if (/OBV 多头背离/.test(text)) tokens.push('o多背');
                if (/OBV 空头背离/.test(text)) tokens.push('o空背');
                if (/清空|消费|超过/.test(text)) tokens.push('清空');
            });
            return Array.from(new Set(tokens));
        }

        function getTraceFlowTokens(trace, maxItems = 4) {
            const tokens = Array.from(new Set([
                ...getTraceEventTokens(trace),
                ...getTraceComponentTokens(trace),
            ]));
            if (!tokens.length) return [];
            if (tokens.length <= maxItems) return tokens;
            return [...tokens.slice(0, maxItems), `+${tokens.length - maxItems}`];
        }

        function normalizeDtpDir(value, phaseText = '') {
            const num = Number(value);
            if (num === 1) return 1;
            if (num === -1) return -1;
            const text = String(phaseText || '').trim().toLowerCase();
            if (/蓝|blue|bull|long|up/.test(text)) return 1;
            if (/红|red|bear|short|down/.test(text)) return -1;
            return 0;
        }

        function getTraceDtpState(trace) {
            const structure = trace?.structure && typeof trace.structure === 'object' ? trace.structure : {};
            const phase = String(structure.dtp_phase || '').trim();
            const dir = normalizeDtpDir(structure.dtp_dir, phase);
            const bars = Number(structure.dtp_phase_bars || 0);
            return {
                dir,
                phase,
                bars: Number.isFinite(bars) && bars > 0 ? bars : 0,
            };
        }

        function getDtpPhaseShortText(phase) {
            const text = String(phase || '').trim();
            if (!text) return '';
            if (/红|蓝|中性/.test(text)) return text;
            const lowered = text.toLowerCase();
            if (lowered === 'early') return '初';
            if (lowered === 'confirmed') return '中';
            if (lowered === 'mature') return '熟';
            if (lowered === 'weakening') return '弱';
            if (lowered === 'neutral') return '中性';
            return text;
        }

        function formatDtpStateLabel(state, { compact = false } = {}) {
            const dir = Number(state?.dir || 0);
            const phase = getDtpPhaseShortText(state?.phase || '');
            if (phase && /^(红|蓝|中性)/.test(phase)) return compact ? phase : `DTP${phase}`;
            const dirText = dir === 1 ? '蓝' : dir === -1 ? '红' : '中性';
            const label = dir === 0 ? '中性' : `${dirText}${phase && phase !== '中性' ? phase : ''}`;
            return compact ? label : `DTP${label}`;
        }

        function getDtpStateColor(state) {
            const dir = Number(state?.dir || 0);
            if (dir === 1) return '#38BDF8';
            if (dir === -1) return '#FB7185';
            return '#94A3B8';
        }

        function buildLifecycleLineSeries(name, data, options = {}) {
            const color = options.color || '#7DD3FC';
            return {
                name,
                type: 'line',
                xAxisIndex: 1,
                yAxisIndex: 1,
                data,
                symbol: 'none',
                connectNulls: false,
                smooth: false,
                silent: true,
                z: Number(options.z || 5),
                tooltip: { show: false },
                lineStyle: {
                    width: Number(options.width || 5),
                    color,
                    opacity: Number(options.opacity ?? 0.86),
                    cap: 'round',
                },
                emphasis: { disabled: true },
            };
        }

        function buildLifecycleScatterSeries(name, data, options = {}) {
            const color = options.color || '#7DD3FC';
            const showLabel = Boolean(options.showLabel);
            return {
                name,
                type: 'scatter',
                xAxisIndex: 1,
                yAxisIndex: 1,
                data,
                symbol: options.symbol || 'circle',
                symbolSize: Number(options.symbolSize || 8),
                symbolRotate: Number(options.symbolRotate || 0),
                z: Number(options.z || 10),
                tooltip: { show: false },
                label: {
                    show: showLabel,
                    formatter(params) {
                        return params?.data?.labelText || '';
                    },
                    position: options.labelPosition || 'right',
                    distance: Number(options.labelDistance ?? 5),
                    color: options.labelColor || color,
                    fontSize: Number(options.labelFontSize || 10),
                    fontWeight: 700,
                    fontFamily: 'JetBrains Mono, monospace',
                    backgroundColor: showLabel ? 'rgba(8,12,20,0.88)' : 'transparent',
                    borderColor: color,
                    borderWidth: showLabel ? 1 : 0,
                    borderRadius: 6,
                    padding: showLabel ? [2, 5] : 0,
                },
                labelLayout: {
                    hideOverlap: true,
                    moveOverlap: 'shiftY',
                },
                itemStyle: {
                    color,
                    borderColor: options.borderColor || 'rgba(8,12,20,0.96)',
                    borderWidth: Number(options.borderWidth ?? 1.2),
                    shadowBlur: Number(options.shadowBlur || 0),
                    shadowColor: options.shadowColor || 'transparent',
                },
                emphasis: {
                    scale: 1.08,
                },
            };
        }

        function buildDtpStateSeries(name, data, options = {}) {
            return {
                name,
                type: 'line',
                xAxisIndex: 1,
                yAxisIndex: 1,
                data,
                symbol: 'none',
                connectNulls: false,
                smooth: false,
                silent: true,
                z: Number(options.z || 6),
                tooltip: { show: false },
                lineStyle: {
                    width: Number(options.width || 5),
                    color: options.color || '#94A3B8',
                    opacity: Number(options.opacity ?? 0.82),
                    cap: 'round',
                },
                emphasis: { disabled: true },
            };
        }

        function buildSignalLifecycleSeries(traceTimeline, bars, { showLabels = true } = {}) {
            const indexByMs = new Map(bars.map((bar, index) => [Number(bar?.bar_time_ms || 0), index]));
            const upperWindow = bars.map(() => null);
            const lowerWindow = bars.map(() => null);
            const dtpRed = bars.map(() => null);
            const dtpBlue = bars.map(() => null);
            const dtpNeutral = bars.map(() => null);
            const dtpChanges = [];
            const components = [];
            const candidates = [];
            const blocked = [];
            const confirmed = [];
            const cleared = [];
            let previousDtpKey = '';
            traceTimeline.forEach((trace) => {
                const barMs = Number(trace?.bar_time_ms || 0);
                const index = indexByMs.get(barMs);
                if (!Number.isInteger(index)) return;
                const flags = trace?.window_flags && typeof trace.window_flags === 'object' ? trace.window_flags : {};
                if (flags.sd_upper_valid || flags.sd_upper_active) upperWindow[index] = SIGNAL_LIFECYCLE_Y.upperWindow;
                if (flags.sd_lower_valid || flags.sd_lower_active) lowerWindow[index] = SIGNAL_LIFECYCLE_Y.lowerWindow;

                const eventTokens = getTraceEventTokens(trace);
                const componentTokens = getTraceFlowTokens(trace, 5);
                const dtpState = getTraceDtpState(trace);
                const common = {
                    bar_index: index,
                    bar_time_ms: barMs,
                    us_time: trace?.us_time || '',
                };
                if (dtpState.phase || dtpState.dir) {
                    const dtpPoint = {
                        value: [index, SIGNAL_LIFECYCLE_Y.dtp],
                        labelText: formatDtpStateLabel(dtpState),
                        state: dtpState,
                    };
                    if (dtpState.dir === 1) {
                        dtpBlue[index] = dtpPoint;
                    } else if (dtpState.dir === -1) {
                        dtpRed[index] = dtpPoint;
                    } else {
                        dtpNeutral[index] = dtpPoint;
                    }
                    const dtpKey = `${dtpState.dir}:${dtpState.phase}`;
                    if (dtpKey && dtpKey !== previousDtpKey) {
                        dtpChanges.push({
                            ...common,
                            value: [index, SIGNAL_LIFECYCLE_Y.dtp],
                            labelText: formatDtpStateLabel(dtpState),
                            name: formatDtpStateLabel(dtpState),
                            direction: dtpState.dir === 1 ? 'bullish' : dtpState.dir === -1 ? 'bearish' : 'neutral',
                            dtp_phase: dtpState.phase,
                            dtp_phase_bars: dtpState.bars,
                        });
                    }
                    previousDtpKey = dtpKey;
                }
                if (componentTokens.length && (hasTraceComponentFlags(trace?.component_flags) || eventTokens.length)) {
                    components.push({
                        ...common,
                        value: [index, SIGNAL_LIFECYCLE_Y.component],
                        labelText: componentTokens.join(' '),
                        name: componentTokens.join(' / '),
                        signal_id: `component:${barMs}`,
                        trace_kind: 'component',
                        component_tokens: componentTokens,
                        event_tokens: eventTokens,
                    });
                }

                const stage = getTraceStage(trace);
                const direction = getTraceDirection(trace);
                const reason = getTraceFilterReason(trace);
                const label = formatTraceDecisionLabel(trace, null);
                if (stage === 'blocked') {
                    blocked.push({
                        ...common,
                        value: [index, SIGNAL_LIFECYCLE_Y.decision],
                        labelText: reason || 'blocked',
                        name: label,
                        signal_id: getTraceSignalKey(trace),
                        direction,
                    });
                } else if (stage === 'confirmed') {
                    confirmed.push({
                        ...common,
                        value: [index, SIGNAL_LIFECYCLE_Y.decision],
                        labelText: direction === 'short' ? '确认空' : '确认多',
                        name: label,
                        signal_id: getTraceSignalKey(trace),
                        direction,
                    });
                } else if (stage === 'candidate') {
                    candidates.push({
                        ...common,
                        value: [index, SIGNAL_LIFECYCLE_Y.decision],
                        labelText: direction === 'short' ? '候选空' : '候选多',
                        name: label,
                        signal_id: getTraceSignalKey(trace),
                        direction,
                    });
                }

                if (eventTokens.some((token) => token === '清空')) {
                    cleared.push({
                        ...common,
                        value: [index, SIGNAL_LIFECYCLE_Y.cleared],
                        labelText: '清空',
                        name: getTraceEventTokens(trace).join(' / '),
                    });
                }
            });
            return [
                buildLifecycleLineSeries('Flow 上轨窗口', upperWindow, { color: 'rgba(248,113,113,0.78)', width: 4 }),
                buildLifecycleLineSeries('Flow 下轨窗口', lowerWindow, { color: 'rgba(74,222,128,0.78)', width: 4 }),
                buildDtpStateSeries('Flow DTP红', dtpRed, { color: 'rgba(251,113,133,0.82)', width: 5 }),
                buildDtpStateSeries('Flow DTP蓝', dtpBlue, { color: 'rgba(56,189,248,0.82)', width: 5 }),
                buildDtpStateSeries('Flow DTP中性', dtpNeutral, { color: 'rgba(148,163,184,0.66)', width: 4 }),
                buildLifecycleScatterSeries('Flow DTP转换', dtpChanges, { color: '#FDE68A', symbol: 'diamond', symbolSize: 10, showLabel: showLabels, labelPosition: 'top', labelDistance: 7, labelFontSize: 9, shadowBlur: 8, shadowColor: 'rgba(253,230,138,0.18)', z: 12 }),
                buildLifecycleScatterSeries('Flow 组件收集', components, { color: '#38BDF8', symbol: 'circle', symbolSize: 7, showLabel: false, labelPosition: 'top' }),
                buildLifecycleScatterSeries('Flow 候选', candidates, { color: '#FBBF24', symbol: 'diamond', symbolSize: 10, showLabel: false, labelPosition: 'bottom', shadowBlur: 8, shadowColor: 'rgba(251,191,36,0.22)' }),
                buildLifecycleScatterSeries('Flow 已过滤', blocked, { color: '#F97316', symbol: 'diamond', symbolSize: 12, showLabel: showLabels, labelPosition: 'top', labelDistance: 7, shadowBlur: 10, shadowColor: 'rgba(249,115,22,0.26)' }),
                buildLifecycleScatterSeries('Flow 已确认', confirmed, { color: '#22C55E', symbol: 'circle', symbolSize: 11, showLabel: showLabels, labelPosition: 'top', labelDistance: 7, shadowBlur: 10, shadowColor: 'rgba(34,197,94,0.24)' }),
                buildLifecycleScatterSeries('Flow 清空/过期', cleared, { color: '#94A3B8', symbol: 'pin', symbolRotate: 180, symbolSize: 10, showLabel: false }),
            ];
        }

        function buildBlockedTraceMarkers(traceTimeline, bars, indicatorMap, markerOffset) {
            const indexByMs = new Map(bars.map((bar, index) => [Number(bar?.bar_time_ms || 0), index]));
            return traceTimeline.map((trace) => {
                if (getTraceStage(trace) !== 'blocked') return null;
                const barMs = Number(trace?.bar_time_ms || 0);
                const index = indexByMs.get(barMs);
                if (!Number.isInteger(index)) return null;
                const bar = bars[index] || null;
                if (!bar) return null;
                const indicator = indicatorMap.get(barMs) || {};
                const direction = getTraceDirection(trace);
                const below = direction !== 'short';
                const yValue = below
                    ? Number(bar.low || 0) - markerOffset(indicator, bar, 4.2)
                    : Number(bar.high || 0) + markerOffset(indicator, bar, 4.2);
                if (!Number.isFinite(yValue) || yValue <= 0) return null;
                const reason = getTraceFilterReason(trace);
                return {
                    value: [index, yValue],
                    bar_index: index,
                    bar_time_ms: barMs,
                    labelText: reason || 'blocked',
                    name: formatTraceDecisionLabel(trace, null),
                    signal_id: getTraceSignalKey(trace),
                    direction,
                };
            }).filter(Boolean);
        }

        function buildMarkerScatterSeries(name, data, options = {}) {
            const color = options.color || '#7DD3FC';
            const showLabel = Boolean(options.showLabel);
            const densityTier = chartMarkerDensityTier || '';
            const defaultLabelDistance = densityTier === 'mid' ? 3 : 4;
            const defaultLabelFontSize = densityTier === 'mid' ? 9 : 10;
            const defaultLabelPadding = densityTier === 'mid' ? [1, 3] : [2, 4];
            const labelPosition = options.labelPosition || 'top';
            const labelDistance = Number(options.labelDistance ?? defaultLabelDistance);
            const labelFontSize = Number(options.labelFontSize ?? defaultLabelFontSize);
            const labelPadding = options.labelPadding || defaultLabelPadding;
            const seriesData = options.labelLaneAllocator
                ? options.labelLaneAllocator.apply(data, {
                    showLabel,
                    position: labelPosition,
                    baseDistance: labelDistance,
                    fontSize: labelFontSize,
                    padding: labelPadding,
                })
                : data;
            return {
                name,
                type: 'scatter',
                xAxisIndex: 0,
                yAxisIndex: 0,
                data: seriesData,
                symbol: options.symbol || 'triangle',
                symbolRotate: Number(options.symbolRotate || 0),
                symbolSize: Number(options.symbolSize || 12),
                z: Number(options.z || 8),
                animation: false,
                tooltip: { show: false },
                label: {
                    show: showLabel,
                    formatter(params) {
                        return params?.data?.labelText || '';
                    },
                    position: labelPosition,
                    distance: labelDistance,
                    color: options.labelColor || color,
                    fontSize: labelFontSize,
                    fontWeight: 700,
                    fontFamily: 'JetBrains Mono, monospace',
                    backgroundColor: showLabel ? (densityTier === 'mid' ? 'rgba(8,12,20,0.82)' : 'rgba(8,12,20,0.92)') : 'transparent',
                    borderColor: color,
                    borderWidth: showLabel ? 1 : 0,
                    borderRadius: 6,
                    padding: showLabel ? labelPadding : 0,
                },
                labelLayout: {
                    hideOverlap: true,
                    moveOverlap: 'shiftY',
                },
                itemStyle: {
                    color,
                    borderColor: 'rgba(8,12,20,0.96)',
                    borderWidth: 1.4,
                    shadowBlur: Number(options.shadowBlur || 0),
                    shadowColor: options.shadowColor || 'transparent',
                },
                emphasis: {
                    disabled: true,
                },
            };
        }

        function renderChart(payload) {
            const displayPayload = buildChartDisplayPayload(payload);
            chartDisplayPayload = displayPayload || payload || null;
            const chartSeriesPayload = displayPayload || payload || {};
            const bars = Array.isArray(displayPayload?.bars) ? displayPayload.bars.slice() : [];
            const formalBars = Array.isArray(payload?.bars) ? payload.bars.slice() : [];
            const indicators = Array.isArray(chartSeriesPayload?.indicators) ? chartSeriesPayload.indicators.slice() : [];
            const signals = Array.isArray(chartSeriesPayload?.signals) ? chartSeriesPayload.signals.slice() : [];
            const latest = chartSeriesPayload?.latestIndicator || null;
            const canvas = document.getElementById('chartCanvas');
            const note = document.getElementById('chartNote');

            if (!bars.length) {
                if (chartInstance) {
                    chartInstance.dispose();
                    chartInstance = null;
                }
                chartMarkerDensityTier = '';
                resetChartZoomState();
                canvas.innerHTML = '<div class="chart-empty">暂无可绘制 bars。</div>';
                note.textContent = '图表基于 ibkr_bars 重算。';
                renderChartWorkspaceChrome(displayPayload);
                return;
            }

            const sortedBars = bars.slice().sort((a, b) => Number(a.bar_time_ms || 0) - Number(b.bar_time_ms || 0));
            const sortedFormalBars = formalBars.slice().sort((a, b) => Number(a.bar_time_ms || 0) - Number(b.bar_time_ms || 0));
            const indicatorMap = new Map(indicators.map((item) => [Number(item.bar_time_ms || 0), item]));
            const barMap = new Map(sortedFormalBars.map((item) => [Number(item.bar_time_ms || 0), item]));
            const previewBar = sortedBars.find((item) => item?.preview || item?.is_preview) || null;
            const categories = sortedBars.map((bar) => formatChartLabel(bar));
            const candle = sortedBars.map((bar) => [Number(bar.open || 0), Number(bar.close || 0), Number(bar.low || 0), Number(bar.high || 0)]);
            const emaFast = sortedBars.map((bar) => toChartValue(indicatorMap.get(Number(bar.bar_time_ms || 0))?.ema_fast));
            const emaSlow = sortedBars.map((bar) => toChartValue(indicatorMap.get(Number(bar.bar_time_ms || 0))?.ema_slow));
            const emaTrend = sortedBars.map((bar) => toChartValue(indicatorMap.get(Number(bar.bar_time_ms || 0))?.ema_trend));
            const vwap = sortedBars.map((bar) => toChartValue(indicatorMap.get(Number(bar.bar_time_ms || 0))?.vwap));
            const vwapUpper1 = sortedBars.map((bar) => {
                const indicator = indicatorMap.get(Number(bar.bar_time_ms || 0));
                return toChartValue(indicator?.vwap_upper1 ?? indicator?.vwap_upper);
            });
            const vwapLower1 = sortedBars.map((bar) => {
                const indicator = indicatorMap.get(Number(bar.bar_time_ms || 0));
                return toChartValue(indicator?.vwap_lower1 ?? indicator?.vwap_lower);
            });
            const vwapUpper2 = sortedBars.map((bar) => toChartValue(indicatorMap.get(Number(bar.bar_time_ms || 0))?.vwap_upper2));
            const vwapLower2 = sortedBars.map((bar) => toChartValue(indicatorMap.get(Number(bar.bar_time_ms || 0))?.vwap_lower2));
            const sdReg = sortedBars.map((bar) => toChartValue(indicatorMap.get(Number(bar.bar_time_ms || 0))?.sd_reg));
            const orbHigh = sortedBars.map((bar) => toChartValue(indicatorMap.get(Number(bar.bar_time_ms || 0))?.orb_high));
            const orbLow = sortedBars.map((bar) => toChartValue(indicatorMap.get(Number(bar.bar_time_ms || 0))?.orb_low));
            const sdSignalUpper = sortedBars.map((bar) => toChartValue(indicatorMap.get(Number(bar.bar_time_ms || 0))?.sd_signal_upper));
            const sdSignalLower = sortedBars.map((bar) => toChartValue(indicatorMap.get(Number(bar.bar_time_ms || 0))?.sd_signal_lower));
            const sdFilterUpper = sortedBars.map((bar) => toChartValue(indicatorMap.get(Number(bar.bar_time_ms || 0))?.sd_filter_upper));
            const sdFilterLower = sortedBars.map((bar) => toChartValue(indicatorMap.get(Number(bar.bar_time_ms || 0))?.sd_filter_lower));
            const sdChannelSpan = sortedBars.map((bar) => {
                const indicator = indicatorMap.get(Number(bar.bar_time_ms || 0));
                const upper = Number(indicator?.sd_signal_upper);
                const lower = Number(indicator?.sd_signal_lower);
                if (!Number.isFinite(upper) || !Number.isFinite(lower)) return null;
                return Math.max(upper - lower, 0);
            });
            const crsi = sortedBars.map((bar) => toChartValue(indicatorMap.get(Number(bar.bar_time_ms || 0))?.crsi));
            const crsiUpperBand = sortedBars.map((bar) => toChartValue(indicatorMap.get(Number(bar.bar_time_ms || 0))?.crsi_ub));
            const crsiLowerBand = sortedBars.map((bar) => toChartValue(indicatorMap.get(Number(bar.bar_time_ms || 0))?.crsi_db));
            const obvRsi = sortedBars.map((bar) => toChartValue(indicatorMap.get(Number(bar.bar_time_ms || 0))?.obv_rsi));
            const atrPct = sortedBars.map((bar) => toChartValue(indicatorMap.get(Number(bar.bar_time_ms || 0))?.atr_pct));
            const volume = sortedBars.map((bar) => Number(bar.volume || 0));
            const markerOffset = (indicator, bar, multiplier = 1) => {
                const atrBase = Number(indicator?.atr_raw || indicator?.atr || 0);
                const rangeBase = Math.abs(Number(bar?.high || 0) - Number(bar?.low || 0));
                const closeBase = Math.abs(Number(bar?.close || 0)) * 0.0008;
                return Math.max(atrBase, rangeBase, closeBase, 0.05) * multiplier;
            };
            const densityTier = getChartMarkerDensityTier(sortedBars.length);
            chartMarkerDensityTier = densityTier;
            const showContextMarkers = densityTier !== 'wide';
            const showMarkerLabels = densityTier !== 'wide';
            const showTradeLabels = densityTier !== 'wide';
            const sessionDividerItemsMain = buildSessionDividerMarkLineItems(sortedBars, { showLabels: densityTier !== 'wide' });
            const sessionDividerItemsSub = buildSessionDividerMarkLineItems(sortedBars, { showLabels: false });
            const longSignals = buildSignalScatter(
                signals.filter((item) => String(item.direction || '').toLowerCase() === 'long'),
                sortedFormalBars,
                (signal) => signal.entry || signal.limit_price || barMap.get(Number(signal.bar_time_ms || 0))?.close,
                '#48BB78',
                buildTradeSignalLabel
            );
            const shortSignals = buildSignalScatter(
                signals.filter((item) => String(item.direction || '').toLowerCase() === 'short'),
                sortedFormalBars,
                (signal) => signal.entry || signal.limit_price || barMap.get(Number(signal.bar_time_ms || 0))?.close,
                '#FC8181',
                buildTradeSignalLabel
            );
            const traceTimeline = Array.isArray(displayPayload?.traceTimeline) ? displayPayload.traceTimeline : [];
            const backtestEvents = Array.isArray(displayPayload?.backtestEvents) ? displayPayload.backtestEvents : [];
            const tvEvents = Array.isArray(displayPayload?.tvEvents) ? displayPayload.tvEvents : [];
            const orderEvents = Array.isArray(displayPayload?.orderEvents) ? displayPayload.orderEvents : [];
            const backtestLongEntryMarkers = buildBacktestEventScatter(
                backtestEvents,
                sortedFormalBars,
                '#22C55E',
                (event) => String(event?.event_type || '').toLowerCase() === 'entry_filled' && String(event?.direction || '').toLowerCase() !== 'short',
                (event, bar) => event.price || bar.low
            );
            const backtestShortEntryMarkers = buildBacktestEventScatter(
                backtestEvents,
                sortedFormalBars,
                '#FB7185',
                (event) => String(event?.event_type || '').toLowerCase() === 'entry_filled' && String(event?.direction || '').toLowerCase() === 'short',
                (event, bar) => event.price || bar.high
            );
            const backtestTakeProfitMarkers = buildBacktestEventScatter(
                backtestEvents,
                sortedFormalBars,
                '#38BDF8',
                (event) => String(event?.event_type || '').toLowerCase() === 'exit_take_profit',
                (event, bar) => event.price || bar.high
            );
            const backtestStopLossMarkers = buildBacktestEventScatter(
                backtestEvents,
                sortedFormalBars,
                '#F97316',
                (event) => String(event?.event_type || '').toLowerCase() === 'exit_stop_loss',
                (event, bar) => event.price || bar.low || bar.high
            );
            const backtestOtherExitMarkers = buildBacktestEventScatter(
                backtestEvents,
                sortedFormalBars,
                '#A78BFA',
                (event) => {
                    const type = String(event?.event_type || '').toLowerCase();
                    return type.startsWith('exit_') && type !== 'exit_take_profit' && type !== 'exit_stop_loss';
                },
                (event, bar) => event.price || bar.close
            );
            const tvPreAlertLongMarkers = buildBacktestEventScatter(
                tvEvents,
                sortedFormalBars,
                '#22D3EE',
                (event) => String(event?.event_type || '').toLowerCase() === 'pre_alert' && String(event?.direction || '').toLowerCase() !== 'short',
                (event, bar) => event.price || Number(bar.low || 0) - markerOffset(indicatorMap.get(Number(bar.bar_time_ms || 0)) || {}, bar, 2.9),
                formatTvEventLabel
            );
            const tvPreAlertShortMarkers = buildBacktestEventScatter(
                tvEvents,
                sortedFormalBars,
                '#67E8F9',
                (event) => String(event?.event_type || '').toLowerCase() === 'pre_alert' && String(event?.direction || '').toLowerCase() === 'short',
                (event, bar) => event.price || Number(bar.high || 0) + markerOffset(indicatorMap.get(Number(bar.bar_time_ms || 0)) || {}, bar, 2.9),
                formatTvEventLabel
            );
            const tvExitMarkers = buildBacktestEventScatter(
                tvEvents,
                sortedFormalBars,
                '#60A5FA',
                (event) => String(event?.event_type || '').toLowerCase() === 'exit',
                (event, bar) => event.price || bar.close,
                formatTvEventLabel
            );
            const liveLongEntryMarkers = buildBacktestEventScatter(
                orderEvents,
                sortedFormalBars,
                '#16A34A',
                (event) => String(event?.event_type || '').toLowerCase() === 'live_entry' && String(event?.direction || '').toLowerCase() !== 'short',
                (event, bar) => event.price || bar.low,
                formatOrderEventLabel
            );
            const liveShortEntryMarkers = buildBacktestEventScatter(
                orderEvents,
                sortedFormalBars,
                '#DC2626',
                (event) => String(event?.event_type || '').toLowerCase() === 'live_entry' && String(event?.direction || '').toLowerCase() === 'short',
                (event, bar) => event.price || bar.high,
                formatOrderEventLabel
            );
            const liveExitMarkers = buildBacktestEventScatter(
                orderEvents,
                sortedFormalBars,
                '#FACC15',
                (event) => String(event?.event_type || '').toLowerCase().startsWith('live_exit'),
                (event, bar) => event.price || bar.close,
                formatOrderEventLabel
            );
            const previewCandidateSignals = buildSignalScatter(
                traceTimeline
                    .filter((item) => Boolean(item?.is_preview) && ['candidate', 'blocked'].includes(String(item?.signal_state?.stage || '').trim().toLowerCase()))
                    .map((item) => ({
                        ...(item?.signal_state?.signal_payload || {}),
                        bar_time_ms: item?.bar_time_ms,
                        direction: item?.signal_state?.direction || item?.signal_state?.signal_payload?.direction || '',
                        signal: item?.signal_state?.signal || item?.signal_state?.signal_payload?.signal || '',
                        trace_stage: item?.signal_state?.stage || '',
                    })),
                sortedBars,
                (signal) => signal.entry || signal.limit_price || signal.close || sortedBars[sortedBars.length - 1]?.close,
                '#FBBF24',
                () => '候选'
            );
            const lifecycleSeries = buildSignalLifecycleSeries(traceTimeline, sortedBars, {
                showLabels: densityTier !== 'wide',
            });
            const blockedTraceMarkers = buildBlockedTraceMarkers(traceTimeline, sortedBars, indicatorMap, markerOffset);
            const fractalBullMarkers = buildIndicatorMarkerPoints(
                sortedBars,
                indicatorMap,
                (indicator) => Boolean(indicator?.fractal_bull),
                (indicator, bar) => Number(bar.low || 0) - markerOffset(indicator, bar, 0.45),
                () => 'F↑'
            );
            const fractalBearMarkers = buildIndicatorMarkerPoints(
                sortedBars,
                indicatorMap,
                (indicator) => Boolean(indicator?.fractal_bear),
                (indicator, bar) => Number(bar.high || 0) + markerOffset(indicator, bar, 0.45),
                () => 'F↓'
            );
            const sdLowerMarkers = buildIndicatorMarkerPoints(
                sortedBars,
                indicatorMap,
                (indicator) => Boolean(indicator?.sd_lower),
                (indicator, bar) => Number(bar.low || 0) - markerOffset(indicator, bar, 0.8),
                () => 'SD↑'
            );
            const sdUpperMarkers = buildIndicatorMarkerPoints(
                sortedBars,
                indicatorMap,
                (indicator) => Boolean(indicator?.sd_upper),
                (indicator, bar) => Number(bar.high || 0) + markerOffset(indicator, bar, 0.8),
                () => 'SD↓'
            );
            const orbBreakoutUpMarkers = buildIndicatorMarkerPoints(
                sortedBars,
                indicatorMap,
                (indicator) => Boolean(indicator?.orb_breakout_up),
                (indicator, bar) => Number(bar.high || 0) + markerOffset(indicator, bar, 1.05),
                () => 'ORB↑'
            );
            const orbBreakoutDownMarkers = buildIndicatorMarkerPoints(
                sortedBars,
                indicatorMap,
                (indicator) => Boolean(indicator?.orb_breakout_down),
                (indicator, bar) => Number(bar.low || 0) - markerOffset(indicator, bar, 1.05),
                () => 'ORB↓'
            );
            const bullTouchFastMarkers = buildIndicatorMarkerPoints(
                sortedBars,
                indicatorMap,
                (indicator) => Boolean(indicator?.bull_touch_fast),
                (indicator, bar) => Number(bar.low || 0) - markerOffset(indicator, bar, 1.05),
                () => 'E↑F'
            );
            const bullTouchSlowMarkers = buildIndicatorMarkerPoints(
                sortedBars,
                indicatorMap,
                (indicator) => Boolean(indicator?.bull_touch_slow),
                (indicator, bar) => Number(bar.low || 0) - markerOffset(indicator, bar, 1.3),
                () => 'E↑S'
            );
            const bearTouchFastMarkers = buildIndicatorMarkerPoints(
                sortedBars,
                indicatorMap,
                (indicator) => Boolean(indicator?.bear_touch_fast),
                (indicator, bar) => Number(bar.high || 0) + markerOffset(indicator, bar, 1.05),
                () => 'E↓F'
            );
            const bearTouchSlowMarkers = buildIndicatorMarkerPoints(
                sortedBars,
                indicatorMap,
                (indicator) => Boolean(indicator?.bear_touch_slow),
                (indicator, bar) => Number(bar.high || 0) + markerOffset(indicator, bar, 1.3),
                () => 'E↓S'
            );
            const crsiRegBullMarkers = buildIndicatorMarkerPoints(sortedBars, indicatorMap, (indicator) => Boolean(indicator?.crsi_reg_bull_div), (indicator, bar) => Number(bar.low || 0) - markerOffset(indicator, bar, 1.6), () => 'cR↑');
            const crsiWideBullMarkers = buildIndicatorMarkerPoints(sortedBars, indicatorMap, (indicator) => Boolean(indicator?.crsi_wide_bull_div), (indicator, bar) => Number(bar.low || 0) - markerOffset(indicator, bar, 1.9), () => 'cW↑');
            const obvRegBullMarkers = buildIndicatorMarkerPoints(sortedBars, indicatorMap, (indicator) => Boolean(indicator?.obv_reg_bull_div), (indicator, bar) => Number(bar.low || 0) - markerOffset(indicator, bar, 2.2), () => 'oR↑');
            const obvWideBullMarkers = buildIndicatorMarkerPoints(sortedBars, indicatorMap, (indicator) => Boolean(indicator?.obv_wide_bull_div), (indicator, bar) => Number(bar.low || 0) - markerOffset(indicator, bar, 2.5), () => 'oW↑');
            const crsiRegHidBullMarkers = buildIndicatorMarkerPoints(sortedBars, indicatorMap, (indicator) => Boolean(indicator?.crsi_reg_hid_bull), (indicator, bar) => Number(bar.low || 0) - markerOffset(indicator, bar, 2.8), () => 'cH↑');
            const crsiWideHidBullMarkers = buildIndicatorMarkerPoints(sortedBars, indicatorMap, (indicator) => Boolean(indicator?.crsi_wide_hid_bull), (indicator, bar) => Number(bar.low || 0) - markerOffset(indicator, bar, 3.1), () => 'cWH↑');
            const obvRegHidBullMarkers = buildIndicatorMarkerPoints(sortedBars, indicatorMap, (indicator) => Boolean(indicator?.obv_reg_hid_bull), (indicator, bar) => Number(bar.low || 0) - markerOffset(indicator, bar, 3.4), () => 'oH↑');
            const obvWideHidBullMarkers = buildIndicatorMarkerPoints(sortedBars, indicatorMap, (indicator) => Boolean(indicator?.obv_wide_hid_bull), (indicator, bar) => Number(bar.low || 0) - markerOffset(indicator, bar, 3.7), () => 'oWH↑');
            const crsiRegBearMarkers = buildIndicatorMarkerPoints(sortedBars, indicatorMap, (indicator) => Boolean(indicator?.crsi_reg_bear_div), (indicator, bar) => Number(bar.high || 0) + markerOffset(indicator, bar, 1.6), () => 'cR↓');
            const crsiWideBearMarkers = buildIndicatorMarkerPoints(sortedBars, indicatorMap, (indicator) => Boolean(indicator?.crsi_wide_bear_div), (indicator, bar) => Number(bar.high || 0) + markerOffset(indicator, bar, 1.9), () => 'cW↓');
            const obvRegBearMarkers = buildIndicatorMarkerPoints(sortedBars, indicatorMap, (indicator) => Boolean(indicator?.obv_reg_bear_div), (indicator, bar) => Number(bar.high || 0) + markerOffset(indicator, bar, 2.2), () => 'oR↓');
            const obvWideBearMarkers = buildIndicatorMarkerPoints(sortedBars, indicatorMap, (indicator) => Boolean(indicator?.obv_wide_bear_div), (indicator, bar) => Number(bar.high || 0) + markerOffset(indicator, bar, 2.5), () => 'oW↓');
            const crsiRegHidBearMarkers = buildIndicatorMarkerPoints(sortedBars, indicatorMap, (indicator) => Boolean(indicator?.crsi_reg_hid_bear), (indicator, bar) => Number(bar.high || 0) + markerOffset(indicator, bar, 2.8), () => 'cH↓');
            const crsiWideHidBearMarkers = buildIndicatorMarkerPoints(sortedBars, indicatorMap, (indicator) => Boolean(indicator?.crsi_wide_hid_bear), (indicator, bar) => Number(bar.high || 0) + markerOffset(indicator, bar, 3.1), () => 'cWH↓');
            const obvRegHidBearMarkers = buildIndicatorMarkerPoints(sortedBars, indicatorMap, (indicator) => Boolean(indicator?.obv_reg_hid_bear), (indicator, bar) => Number(bar.high || 0) + markerOffset(indicator, bar, 3.4), () => 'oH↓');
            const obvWideHidBearMarkers = buildIndicatorMarkerPoints(sortedBars, indicatorMap, (indicator) => Boolean(indicator?.obv_wide_hid_bear), (indicator, bar) => Number(bar.high || 0) + markerOffset(indicator, bar, 3.7), () => 'oWH↓');
            const preserveCursorIndex = hoverBarIndex >= 0;
            const preferredFocusIndex = getEffectiveCursorIndex(displayPayload);
            const focus = Number.isInteger(preferredFocusIndex) && preferredFocusIndex >= 0
                ? buildContext(displayPayload, preferredFocusIndex, selectedSignalId)
                : buildContext(displayPayload, sortedBars.length - 1, selectedSignalId);
            const latestClose = Number(realtimeQuoteSnapshot?.last_price ?? previewBar?.close ?? latest?.close ?? sortedBars[sortedBars.length - 1]?.close ?? 0);
            const previousClose = Number(sortedBars.length > 1 ? sortedBars[sortedBars.length - 2]?.close : latestClose);
            const latestLineColor = latestClose >= previousClose ? '#4ADE80' : '#FB7185';
            const latestPriceLineItem = buildLatestPriceMarkLineItem(latestClose, latestLineColor);
            const riskLevelLineItems = buildRiskLevelMarkLineItems(signals, sortedBars, displayPayload, {
                densityTier,
                referencePrice: latestClose,
            });
            const gridLeft = isCompactViewport() ? 66 : 78;
            const gridRight = isCompactViewport() ? 96 : 116;
            const compactChart = isCompactViewport();
            const chartGrids = compactChart
                ? [
                    { left: gridLeft, right: gridRight, top: 74, height: '34%' },
                    { left: gridLeft, right: gridRight, top: '52.5%', height: '9%' },
                    { left: gridLeft, right: gridRight, top: '66%', height: '9.5%' },
                    { left: gridLeft, right: gridRight, top: '81%', height: '9.2%' },
                ]
                : [
                    { left: gridLeft, right: gridRight, top: 68, height: '39.5%' },
                    { left: gridLeft, right: gridRight, top: '51.5%', height: '10%' },
                    { left: gridLeft, right: gridRight, top: '66%', height: '10.2%' },
                    { left: gridLeft, right: gridRight, top: '81%', height: '9.5%' },
                ];
            const subplotTitles = [
                {
                    text: chartLayerState.lifecycle ? 'Signal Flow' : 'Signal Flow Hidden',
                    subtext: '',
                    left: gridLeft,
                    top: compactChart ? '50.9%' : '49.9%',
                },
                {
                    text: 'CRSI / OBV RSI',
                    subtext: '优先展示 cRSI 动态带，80 / 20 仅作弱参考',
                    left: gridLeft,
                    top: compactChart ? '64.7%' : '64.7%',
                },
                {
                    text: chartLayerState.volume ? '成交量 / ATR%' : 'ATR%',
                    subtext: chartLayerState.volume ? '柱体=成交量，折线=ATR%' : '当前只显示 ATR% 折线',
                    left: gridLeft,
                    top: compactChart ? '79.7%' : '79.7%',
                }
            ];
            const focusMarkPoint = buildFocusMarkPointConfig(focus?.index ?? -1, displayPayload);
            const zoomWindow = getCurrentZoomWindow(sortedBars.length);

            document.getElementById('chartPanelTitle').textContent = `${currentSymbol} · ${getIntervalLabel(currentInterval)}`;
            const compareSummary = comparePayload?.comparison?.summary || null;
            const barsMetaText = `${sortedFormalBars.length} bars${previewBar ? ' + live preview' : ''}`;
            const preAlertCount = tvEvents.filter((event) => String(event?.event_type || '').toLowerCase() === 'pre_alert').length;
            const liveExitCount = orderEvents.filter((event) => String(event?.event_type || '').toLowerCase().startsWith('live_exit')).length;
            document.getElementById('chartMeta').textContent = compareSummary
                ? `${barsMetaText} · ${indicators.length} ind · ${signals.length} signals · compare bar ${compareSummary.bar_mismatch_count || 0} / ind ${compareSummary.indicator_mismatch_count || 0} / sig ${compareSummary.signal_mismatch_count || 0}`
                : `${barsMetaText} · ${indicators.length} ind · ${signals.length} signals${preAlertCount ? ` · TV pre_alert ${preAlertCount}` : ''}${liveExitCount ? ` · exits ${liveExitCount}` : ''}${backtestEvents.length ? ` · ${backtestEvents.length} backtest events` : ''} · ${latest?.us_time || sortedFormalBars[sortedFormalBars.length - 1]?.us_time || '--'}`;
            note.textContent = !indicators.length
                ? '当前窗口的 bars 尚未形成可展示的指标快照；EMA / VWAP / Osc 将暂时不可见。'
                : currentInterval === '5m'
                ? `工具条/键盘导航；交易标签${chartLayerState.tradeSignals ? '已开' : '已关'}，TP/SL${chartLayerState.riskLevels ? '已开' : '已关'}。`
                : '当前为非 5m 周期，主图仍展示 bars 实时重算出的价格结构与技术图层，但不叠加交易标签。';
            if (realtimeQuoteSnapshot?.last_price != null) {
                note.textContent += ' 价格与日内涨幅来自 WS 实时快照。';
            }
            if (previewBar) {
                note.textContent += ' 当前 5m 叠加了一根未收盘 preview bar；正式写库与指标计算仍只认官方收盘 bar。';
            }
            if (comparePayload) {
                note.textContent += ' 当前主图仍展示 stored bars 链路；右侧 Compare 与上方 Cursor 显示 IBKR API 临时对比结果。';
            } else if (compareError) {
                note.textContent += ' 最近一次 IBKR 对比失败，可查看右侧错误卡。';
            }
            if (currentBacktestRunId && backtestEvents.length) {
                note.textContent += ` 已叠加回测 ${currentBacktestRunId} 的买入 / 卖出 / TP / SL 标记。`;
            }
            if (preAlertCount || liveExitCount) {
                note.textContent += ` 已叠加 TV pre_alert ${preAlertCount} 个、真实退出 ${liveExitCount} 个。`;
            }
            const freshnessStatus = String(payload?.meta?.freshness?.status || '').trim().toLowerCase();
            if (freshnessStatus && freshnessStatus !== 'ready') {
                const repairStatus = String(payload?.meta?.repair?.status || '补偿中').trim();
                note.textContent += ` 当前周期 bars ${freshnessStatus}，已触发 IBKR API 异步补偿（${repairStatus}），图表先显示已有权威数据。`;
            }
            if (supportsSessionDividers()) {
                note.textContent += ' 竖线按美股 ET 时段分隔盘前 / 盘中 / 盘后。';
            }
            note.textContent += isCompactViewport()
                ? ' 手机端会把状态卡和信息卡改成横向滑动区；支持长按锁定光标、轻扫切换 bar、双击回到最新。'
                : ' 支持触控板横向双指平移、捏合/ctrl+滚轮缩放、拖拽平移、双击回到最新，以及 ←/→ 与 N/P 键快速导航。';

            if (chartInstance) {
                chartInstance.dispose();
            }
            canvas.innerHTML = '';
            chartInstance = echarts.init(canvas);
            const mainLabelLaneAllocator = createChartLabelLaneAllocator({
                barsCount: sortedBars.length,
                gridLeft,
                gridRight,
                densityTier,
            });
            const withMainLabelLanes = (options = {}) => ({
                ...options,
                labelLaneAllocator: mainLabelLaneAllocator,
            });
            const overlaySeries = [
                ...(chartLayerState.sdChannel ? [
                    {
                        name: 'SD Channel Base',
                        type: 'line',
                        data: sdSignalLower,
                        stack: 'sd-channel',
                        symbol: 'none',
                        connectNulls: true,
                        smooth: false,
                        silent: true,
                        tooltip: { show: false },
                        z: 1,
                        lineStyle: { width: 0, opacity: 0 },
                        areaStyle: { opacity: 0 },
                        emphasis: { disabled: true },
                    },
                    {
                        name: 'SD Channel Fill',
                        type: 'line',
                        data: sdChannelSpan,
                        stack: 'sd-channel',
                        symbol: 'none',
                        connectNulls: true,
                        smooth: false,
                        silent: true,
                        tooltip: { show: false },
                        z: 1,
                        lineStyle: { width: 0, opacity: 0 },
                        areaStyle: {
                            color: 'rgba(56, 189, 248, 0.12)',
                            shadowBlur: 18,
                            shadowColor: 'rgba(56, 189, 248, 0.08)',
                        },
                        emphasis: { disabled: true },
                    },
                    { name: 'SD Reg', type: 'line', data: sdReg, symbol: 'none', connectNulls: true, smooth: false, z: 4, lineStyle: { width: 1.25, color: 'rgba(125,211,252,0.68)', type: 'dashed' } },
                    { name: 'SD Signal Upper', type: 'line', data: sdSignalUpper, symbol: 'none', connectNulls: true, smooth: false, z: 5, lineStyle: { width: 1.55, color: 'rgba(248,113,113,0.84)' } },
                    { name: 'SD Signal Lower', type: 'line', data: sdSignalLower, symbol: 'none', connectNulls: true, smooth: false, z: 5, lineStyle: { width: 1.55, color: 'rgba(74,222,128,0.84)' } },
                    { name: 'SD Filter Upper', type: 'line', data: sdFilterUpper, symbol: 'none', connectNulls: true, smooth: false, z: 4, lineStyle: { width: 1.15, color: 'rgba(248,113,113,0.46)', type: 'dashed' } },
                    { name: 'SD Filter Lower', type: 'line', data: sdFilterLower, symbol: 'none', connectNulls: true, smooth: false, z: 4, lineStyle: { width: 1.15, color: 'rgba(74,222,128,0.46)', type: 'dashed' } },
                    { name: 'ORB High', type: 'line', data: orbHigh, symbol: 'none', connectNulls: true, smooth: false, z: 3, lineStyle: { width: 1.05, color: 'rgba(251,191,36,0.62)', type: 'dotted' } },
                    { name: 'ORB Low', type: 'line', data: orbLow, symbol: 'none', connectNulls: true, smooth: false, z: 3, lineStyle: { width: 1.05, color: 'rgba(251,191,36,0.62)', type: 'dotted' } },
                    buildMarkerScatterSeries('SD MR Bull', sdLowerMarkers, withMainLabelLanes({ color: '#4CAF50', symbol: 'triangle', symbolSize: 13, showLabel: showMarkerLabels, labelPosition: 'bottom', shadowBlur: 10, shadowColor: 'rgba(76,175,80,0.24)' })),
                    buildMarkerScatterSeries('SD MR Bear', sdUpperMarkers, withMainLabelLanes({ color: '#FF8A00', symbol: 'triangle', symbolRotate: 180, symbolSize: 13, showLabel: showMarkerLabels, labelPosition: 'top', shadowBlur: 10, shadowColor: 'rgba(255,138,0,0.24)' })),
                    buildMarkerScatterSeries('ORB Breakout Up', orbBreakoutUpMarkers, withMainLabelLanes({ color: '#FBBF24', symbol: 'arrow', symbolSize: 12, showLabel: showMarkerLabels, labelPosition: 'top', shadowBlur: 10, shadowColor: 'rgba(251,191,36,0.24)' })),
                    buildMarkerScatterSeries('ORB Breakout Down', orbBreakoutDownMarkers, withMainLabelLanes({ color: '#FBBF24', symbol: 'arrow', symbolRotate: 180, symbolSize: 12, showLabel: showMarkerLabels, labelPosition: 'bottom', shadowBlur: 10, shadowColor: 'rgba(251,191,36,0.24)' })),
                ] : []),
                ...(chartLayerState.fractal ? [
                    buildMarkerScatterSeries('Fractal Bull', fractalBullMarkers, withMainLabelLanes({ color: '#14B8A6', symbol: 'triangle', symbolSize: 11, showLabel: showMarkerLabels, labelPosition: 'bottom' })),
                    buildMarkerScatterSeries('Fractal Bear', fractalBearMarkers, withMainLabelLanes({ color: '#F44336', symbol: 'triangle', symbolRotate: 180, symbolSize: 11, showLabel: showMarkerLabels, labelPosition: 'top' })),
                ] : []),
                ...(chartLayerState.emaTouch && showContextMarkers ? [
                    buildMarkerScatterSeries('EMA Touch Bull Fast', bullTouchFastMarkers, withMainLabelLanes({ color: '#00C853', symbol: 'triangle', symbolSize: 11, showLabel: showMarkerLabels, labelPosition: 'bottom' })),
                    buildMarkerScatterSeries('EMA Touch Bull Slow', bullTouchSlowMarkers, withMainLabelLanes({ color: '#64DD17', symbol: 'triangle', symbolSize: 10, showLabel: showMarkerLabels, labelPosition: 'bottom' })),
                    buildMarkerScatterSeries('EMA Touch Bear Fast', bearTouchFastMarkers, withMainLabelLanes({ color: '#FF1744', symbol: 'triangle', symbolRotate: 180, symbolSize: 11, showLabel: showMarkerLabels, labelPosition: 'top' })),
                    buildMarkerScatterSeries('EMA Touch Bear Slow', bearTouchSlowMarkers, withMainLabelLanes({ color: '#D50000', symbol: 'triangle', symbolRotate: 180, symbolSize: 10, showLabel: showMarkerLabels, labelPosition: 'top' })),
                ] : []),
                ...(chartLayerState.divergence && showContextMarkers ? [
                    buildMarkerScatterSeries('cRSI Reg Bull Div', crsiRegBullMarkers, withMainLabelLanes({ color: '#2196F3', symbol: 'triangle', symbolSize: 10, showLabel: showMarkerLabels, labelPosition: 'bottom' })),
                    buildMarkerScatterSeries('cRSI Wide Bull Div', crsiWideBullMarkers, withMainLabelLanes({ color: '#2196F3', symbol: 'triangle', symbolSize: 10, showLabel: showMarkerLabels, labelPosition: 'bottom' })),
                    buildMarkerScatterSeries('OBV Reg Bull Div', obvRegBullMarkers, withMainLabelLanes({ color: '#2196F3', symbol: 'triangle', symbolSize: 10, showLabel: showMarkerLabels, labelPosition: 'bottom' })),
                    buildMarkerScatterSeries('OBV Wide Bull Div', obvWideBullMarkers, withMainLabelLanes({ color: '#2196F3', symbol: 'triangle', symbolSize: 10, showLabel: showMarkerLabels, labelPosition: 'bottom' })),
                    buildMarkerScatterSeries('cRSI Hid Bull', crsiRegHidBullMarkers, withMainLabelLanes({ color: '#2196F3', symbol: 'triangle', symbolSize: 10, showLabel: showMarkerLabels, labelPosition: 'bottom' })),
                    buildMarkerScatterSeries('cRSI Wide Hid Bull', crsiWideHidBullMarkers, withMainLabelLanes({ color: '#2196F3', symbol: 'triangle', symbolSize: 10, showLabel: showMarkerLabels, labelPosition: 'bottom' })),
                    buildMarkerScatterSeries('OBV Hid Bull', obvRegHidBullMarkers, withMainLabelLanes({ color: '#2196F3', symbol: 'triangle', symbolSize: 10, showLabel: showMarkerLabels, labelPosition: 'bottom' })),
                    buildMarkerScatterSeries('OBV Wide Hid Bull', obvWideHidBullMarkers, withMainLabelLanes({ color: '#2196F3', symbol: 'triangle', symbolSize: 10, showLabel: showMarkerLabels, labelPosition: 'bottom' })),
                    buildMarkerScatterSeries('cRSI Reg Bear Div', crsiRegBearMarkers, withMainLabelLanes({ color: '#9C27B0', symbol: 'triangle', symbolRotate: 180, symbolSize: 10, showLabel: showMarkerLabels, labelPosition: 'top' })),
                    buildMarkerScatterSeries('cRSI Wide Bear Div', crsiWideBearMarkers, withMainLabelLanes({ color: '#9C27B0', symbol: 'triangle', symbolRotate: 180, symbolSize: 10, showLabel: showMarkerLabels, labelPosition: 'top' })),
                    buildMarkerScatterSeries('OBV Reg Bear Div', obvRegBearMarkers, withMainLabelLanes({ color: '#9C27B0', symbol: 'triangle', symbolRotate: 180, symbolSize: 10, showLabel: showMarkerLabels, labelPosition: 'top' })),
                    buildMarkerScatterSeries('OBV Wide Bear Div', obvWideBearMarkers, withMainLabelLanes({ color: '#9C27B0', symbol: 'triangle', symbolRotate: 180, symbolSize: 10, showLabel: showMarkerLabels, labelPosition: 'top' })),
                    buildMarkerScatterSeries('cRSI Hid Bear', crsiRegHidBearMarkers, withMainLabelLanes({ color: '#9C27B0', symbol: 'triangle', symbolRotate: 180, symbolSize: 10, showLabel: showMarkerLabels, labelPosition: 'top' })),
                    buildMarkerScatterSeries('cRSI Wide Hid Bear', crsiWideHidBearMarkers, withMainLabelLanes({ color: '#9C27B0', symbol: 'triangle', symbolRotate: 180, symbolSize: 10, showLabel: showMarkerLabels, labelPosition: 'top' })),
                    buildMarkerScatterSeries('OBV Hid Bear', obvRegHidBearMarkers, withMainLabelLanes({ color: '#9C27B0', symbol: 'triangle', symbolRotate: 180, symbolSize: 10, showLabel: showMarkerLabels, labelPosition: 'top' })),
                    buildMarkerScatterSeries('OBV Wide Hid Bear', obvWideHidBearMarkers, withMainLabelLanes({ color: '#9C27B0', symbol: 'triangle', symbolRotate: 180, symbolSize: 10, showLabel: showMarkerLabels, labelPosition: 'top' })),
                ] : []),
                ...(isTradeSignalInterval() && chartLayerState.tradeSignals ? [
                    buildMarkerScatterSeries('LONG Signal', longSignals, withMainLabelLanes({ color: '#48BB78', symbol: 'circle', symbolSize: 12, showLabel: showTradeLabels, labelPosition: 'bottom', shadowBlur: 14, shadowColor: 'rgba(72,187,120,0.28)' })),
                    buildMarkerScatterSeries('SHORT Signal', shortSignals, withMainLabelLanes({ color: '#FC8181', symbol: 'circle', symbolSize: 12, showLabel: showTradeLabels, labelPosition: 'top', shadowBlur: 14, shadowColor: 'rgba(252,129,129,0.28)' })),
                    buildMarkerScatterSeries('TV pre_alert Long', tvPreAlertLongMarkers, withMainLabelLanes({ color: '#22D3EE', symbol: 'diamond', symbolSize: 15, showLabel: true, labelPosition: 'bottom', shadowBlur: 16, shadowColor: 'rgba(34,211,238,0.34)' })),
                    buildMarkerScatterSeries('TV pre_alert Short', tvPreAlertShortMarkers, withMainLabelLanes({ color: '#67E8F9', symbol: 'diamond', symbolSize: 15, showLabel: true, labelPosition: 'top', shadowBlur: 16, shadowColor: 'rgba(103,232,249,0.34)' })),
                    buildMarkerScatterSeries('TV Exit', tvExitMarkers, withMainLabelLanes({ color: '#60A5FA', symbol: 'rect', symbolSize: 12, showLabel: showTradeLabels, labelPosition: 'top', shadowBlur: 12, shadowColor: 'rgba(96,165,250,0.28)' })),
                    buildMarkerScatterSeries('Live Buy', liveLongEntryMarkers, withMainLabelLanes({ color: '#16A34A', symbol: 'pin', symbolSize: 14, showLabel: showTradeLabels, labelPosition: 'bottom', shadowBlur: 12, shadowColor: 'rgba(22,163,74,0.28)' })),
                    buildMarkerScatterSeries('Live Sell', liveShortEntryMarkers, withMainLabelLanes({ color: '#DC2626', symbol: 'pin', symbolRotate: 180, symbolSize: 14, showLabel: showTradeLabels, labelPosition: 'top', shadowBlur: 12, shadowColor: 'rgba(220,38,38,0.28)' })),
                    buildMarkerScatterSeries('Live Exit', liveExitMarkers, withMainLabelLanes({ color: '#FACC15', symbol: 'rect', symbolSize: 13, showLabel: true, labelPosition: 'top', shadowBlur: 14, shadowColor: 'rgba(250,204,21,0.3)' })),
                    buildMarkerScatterSeries('Blocked Trace', blockedTraceMarkers, withMainLabelLanes({ color: '#F97316', symbol: 'diamond', symbolSize: 13, showLabel: showTradeLabels, labelPosition: 'bottom', shadowBlur: 12, shadowColor: 'rgba(249,115,22,0.28)' })),
                    buildMarkerScatterSeries('Preview Signal', previewCandidateSignals, withMainLabelLanes({ color: '#FBBF24', symbol: 'diamond', symbolSize: 13, showLabel: showTradeLabels, labelPosition: 'top', shadowBlur: 12, shadowColor: 'rgba(251,191,36,0.26)' })),
                    buildMarkerScatterSeries('Backtest Buy', backtestLongEntryMarkers, withMainLabelLanes({ color: '#22C55E', symbol: 'pin', symbolSize: 14, showLabel: showTradeLabels, labelPosition: 'bottom', shadowBlur: 14, shadowColor: 'rgba(34,197,94,0.28)' })),
                    buildMarkerScatterSeries('Backtest Sell', backtestShortEntryMarkers, withMainLabelLanes({ color: '#FB7185', symbol: 'pin', symbolRotate: 180, symbolSize: 14, showLabel: showTradeLabels, labelPosition: 'top', shadowBlur: 14, shadowColor: 'rgba(251,113,133,0.28)' })),
                    buildMarkerScatterSeries('Backtest TP', backtestTakeProfitMarkers, withMainLabelLanes({ color: '#38BDF8', symbol: 'diamond', symbolSize: 13, showLabel: showTradeLabels, labelPosition: 'top', shadowBlur: 14, shadowColor: 'rgba(56,189,248,0.28)' })),
                    buildMarkerScatterSeries('Backtest SL', backtestStopLossMarkers, withMainLabelLanes({ color: '#F97316', symbol: 'diamond', symbolSize: 13, showLabel: showTradeLabels, labelPosition: 'bottom', shadowBlur: 14, shadowColor: 'rgba(249,115,22,0.28)' })),
                    buildMarkerScatterSeries('Backtest Exit', backtestOtherExitMarkers, withMainLabelLanes({ color: '#A78BFA', symbol: 'rect', symbolSize: 12, showLabel: showTradeLabels, labelPosition: 'top', shadowBlur: 14, shadowColor: 'rgba(167,139,250,0.28)' })),
                ] : []),
            ];
            const series = [
                {
                    id: 'price-main',
                    name: 'Price',
                    type: 'candlestick',
                    data: candle,
                    barMaxWidth: 16,
                    barMinWidth: 6,
                    itemStyle: {
                        color: '#22C55E',
                        color0: '#FB7185',
                        borderColor: '#4ADE80',
                        borderColor0: '#FB7185'
                    },
                    emphasis: {
                        itemStyle: {
                            borderWidth: 1.4
                        }
                    },
                    markLine: buildMarkLineConfig([
                        ...(latestPriceLineItem ? [latestPriceLineItem] : []),
                        ...riskLevelLineItems,
                        ...sessionDividerItemsMain,
                    ]),
                    markPoint: focusMarkPoint,
                },
                ...(chartLayerState.ema ? [
                    { name: 'EMA 20', type: 'line', data: emaFast, symbol: 'none', connectNulls: true, smooth: true, lineStyle: { width: 1.35, color: '#38BDF8' } },
                    { name: 'EMA 50', type: 'line', data: emaSlow, symbol: 'none', connectNulls: true, smooth: true, lineStyle: { width: 1.25, color: '#F59E0B' } },
                    { name: 'EMA 100', type: 'line', data: emaTrend, symbol: 'none', connectNulls: true, smooth: true, lineStyle: { width: 1.15, color: '#A78BFA' } },
                ] : []),
                ...(chartLayerState.vwap ? [
                    { name: 'VWAP', type: 'line', data: vwap, symbol: 'none', connectNulls: true, smooth: true, lineStyle: { width: 1.15, color: '#34D399' } },
                    { name: 'VWAP Upper 1', type: 'line', data: vwapUpper1, symbol: 'none', connectNulls: true, smooth: true, lineStyle: { width: 0.95, color: 'rgba(52,211,153,0.58)', type: 'dashed' } },
                    { name: 'VWAP Lower 1', type: 'line', data: vwapLower1, symbol: 'none', connectNulls: true, smooth: true, lineStyle: { width: 0.95, color: 'rgba(52,211,153,0.58)', type: 'dashed' } },
                    { name: 'VWAP Upper 2', type: 'line', data: vwapUpper2, symbol: 'none', connectNulls: true, smooth: true, lineStyle: { width: 0.85, color: 'rgba(167,243,208,0.42)', type: 'dotted' } },
                    { name: 'VWAP Lower 2', type: 'line', data: vwapLower2, symbol: 'none', connectNulls: true, smooth: true, lineStyle: { width: 0.85, color: 'rgba(167,243,208,0.42)', type: 'dotted' } },
                ] : []),
                ...overlaySeries,
                ...(isTradeSignalInterval() && chartLayerState.lifecycle ? lifecycleSeries : []),
                {
                    name: 'cRSI Upper Band',
                    type: 'line',
                    xAxisIndex: 2,
                    yAxisIndex: 2,
                    data: crsiUpperBand,
                    symbol: 'none',
                    connectNulls: true,
                    lineStyle: { width: 1, color: 'rgba(251,191,36,0.72)', type: 'dashed' },
                },
                {
                    name: 'cRSI Lower Band',
                    type: 'line',
                    xAxisIndex: 2,
                    yAxisIndex: 2,
                    data: crsiLowerBand,
                    symbol: 'none',
                    connectNulls: true,
                    lineStyle: { width: 1, color: 'rgba(251,191,36,0.72)', type: 'dashed' },
                },
                {
                    name: 'CRSI',
                    type: 'line',
                    xAxisIndex: 2,
                    yAxisIndex: 2,
                    data: crsi,
                    symbol: 'none',
                    connectNulls: true,
                    lineStyle: { width: 1.2, color: '#22C55E' },
                    markLine: buildMarkLineConfig([
                        { yAxis: 80, label: { show: false }, lineStyle: { color: 'rgba(245,158,11,0.24)', type: 'dashed' } },
                        { yAxis: 20, label: { show: false }, lineStyle: { color: 'rgba(245,158,11,0.24)', type: 'dashed' } },
                        ...sessionDividerItemsSub,
                    ])
                },
                { name: 'OBV RSI', type: 'line', xAxisIndex: 2, yAxisIndex: 2, data: obvRsi, symbol: 'none', connectNulls: true, lineStyle: { width: 1.2, color: '#F59E0B' } },
                ...(chartLayerState.volume ? [
                    { name: 'Volume', type: 'bar', xAxisIndex: 3, yAxisIndex: 3, data: volume, itemStyle: { color: 'rgba(56,189,248,0.34)' } },
                ] : []),
                {
                    name: 'ATR %',
                    type: 'line',
                    xAxisIndex: 3,
                    yAxisIndex: 4,
                    data: atrPct,
                    symbol: 'none',
                    connectNulls: true,
                    lineStyle: { width: 1.2, color: '#FB7185' },
                    markLine: buildMarkLineConfig(sessionDividerItemsSub)
                }
            ];
            chartTooltipPinned = Boolean(chartPointerLocked);
            chartInstance.setOption({
                animation: false,
                backgroundColor: 'transparent',
                toolbox: { show: false },
                title: subplotTitles.map((item) => ({
                    text: item.text,
                    subtext: item.subtext,
                    left: item.left,
                    top: item.top,
                    padding: [4, 8, 4, 8],
                    backgroundColor: 'rgba(7, 12, 20, 0.74)',
                    borderColor: 'rgba(99,179,237,0.14)',
                    borderWidth: 1,
                    itemGap: 2,
                    textStyle: {
                        color: '#d7e2f0',
                        fontSize: 10,
                        fontWeight: 700,
                        fontFamily: 'JetBrains Mono',
                    },
                    subtextStyle: {
                        color: '#8BA4C4',
                        fontSize: 9,
                        lineHeight: 12,
                        fontFamily: 'JetBrains Mono',
                    }
                })),
                legend: {
                    show: false
                },
                tooltip: {
                    trigger: 'axis',
                    axisPointer: { type: 'cross', snap: true },
                    alwaysShowContent: chartTooltipPinned,
                    transitionDuration: 0,
                    backgroundColor: 'rgba(8,12,20,0.94)',
                    borderColor: 'rgba(99,179,237,0.16)',
                    textStyle: { color: '#E2EAF4' },
                    enterable: false,
                    confine: true,
                    extraCssText: 'max-width:420px;max-height:280px;overflow:auto;box-shadow:0 14px 44px rgba(0,0,0,0.34);',
                    position(point, params, dom, rect, size) {
                        return resolveChartTooltipPosition(point, size);
                    },
                    formatter(params) {
                        const list = Array.isArray(params) ? params : [params];
                        const axisParam = list.find((item) => Number.isInteger(item?.dataIndex));
                        const index = Number(axisParam?.dataIndex);
                        if (!Number.isInteger(index) || index < 0) return '';
                        return buildTooltipHtml(displayPayload, index);
                    }
                },
                axisPointer: {
                    link: [{ xAxisIndex: 'all' }],
                    label: {
                        backgroundColor: 'rgba(15, 23, 38, 0.96)',
                        color: '#E2EAF4',
                        borderColor: 'rgba(99,179,237,0.24)',
                        borderWidth: 1,
                    },
                    lineStyle: {
                        color: 'rgba(148, 163, 184, 0.42)',
                        width: 1,
                    }
                },
                grid: [
                    ...chartGrids
                ],
                dataZoom: [
                    {
                        type: 'inside',
                        xAxisIndex: [0, 1, 2, 3],
                        start: zoomWindow.start,
                        end: zoomWindow.end,
                        // Custom wheel handling separates horizontal two-finger pan from pinch/ctrl zoom.
                        zoomOnMouseWheel: false,
                        moveOnMouseMove: true,
                        moveOnMouseWheel: false,
                    },
                    {
                        type: 'slider',
                        xAxisIndex: [0, 1, 2, 3],
                        start: zoomWindow.start,
                        end: zoomWindow.end,
                        bottom: 10,
                        height: 20,
                        borderColor: 'rgba(99,179,237,0.08)',
                        backgroundColor: 'rgba(8,12,20,0.76)',
                        fillerColor: 'rgba(56,189,248,0.16)',
                        dataBackground: {
                            lineStyle: { color: 'rgba(99,179,237,0.28)' },
                            areaStyle: { color: 'rgba(99,179,237,0.10)' },
                        },
                        handleStyle: {
                            color: '#0B1728',
                            borderColor: '#7DD3FC',
                        },
                        textStyle: { color: '#8BA4C4' }
                    }
                ],
                xAxis: [
                    {
                        type: 'category',
                        data: categories,
                        scale: true,
                        boundaryGap: true,
                        axisLine: { lineStyle: { color: '#30435E' } },
                        axisTick: { show: false },
                        axisLabel: { color: '#8BA4C4', fontSize: 10, hideOverlap: true },
                        splitLine: { show: false }
                    },
                    {
                        type: 'category',
                        gridIndex: 1,
                        data: categories,
                        axisLabel: { show: false },
                        axisTick: { show: false },
                        axisLine: { lineStyle: { color: '#30435E' } },
                        splitLine: { show: false }
                    },
                    {
                        type: 'category',
                        gridIndex: 2,
                        data: categories,
                        axisLabel: { show: false },
                        axisTick: { show: false },
                        axisLine: { lineStyle: { color: '#30435E' } },
                        splitLine: { show: false }
                    },
                    {
                        type: 'category',
                        gridIndex: 3,
                        data: categories,
                        axisLabel: { color: '#8BA4C4', fontSize: 10, hideOverlap: true },
                        axisTick: { show: false },
                        axisLine: { lineStyle: { color: '#30435E' } },
                        splitLine: { show: false }
                    }
                ],
                yAxis: [
                    {
                        scale: true,
                        position: 'right',
                        splitNumber: 5,
                        splitLine: { lineStyle: { color: 'rgba(99,179,237,0.06)' } },
                        axisLabel: { color: '#8BA4C4', fontSize: 10 },
                    },
                    {
                        gridIndex: 1,
                        min: -0.2,
                        max: 4.45,
                        interval: 1,
                        splitLine: { lineStyle: { color: 'rgba(99,179,237,0.05)' } },
                        axisTick: { show: false },
                        axisLine: { show: false },
                        axisLabel: {
                            color: '#8BA4C4',
                            fontSize: 9,
                            margin: 12,
                            formatter(value) {
                                const num = Math.round(Number(value));
                                if (num === 4) return '窗口';
                                if (num === 3) return 'DTP';
                                if (num === 2) return '组件';
                                if (num === 1) return '决策';
                                return '';
                            }
                        },
                    },
                    {
                        gridIndex: 2,
                        min: 0,
                        max: 100,
                        position: 'right',
                        splitLine: { lineStyle: { color: 'rgba(99,179,237,0.06)' } },
                        axisLabel: { color: '#8BA4C4', fontSize: 10 }
                    },
                    {
                        gridIndex: 3,
                        splitLine: { show: false },
                        axisLabel: { color: '#8BA4C4', fontSize: 10, formatter: (value) => formatCompactAxisNumber(value) },
                    },
                    {
                        gridIndex: 3,
                        position: 'right',
                        splitLine: { show: false },
                        axisLabel: { color: '#8BA4C4', fontSize: 10, formatter: '{value}%' }
                    }
                ],
                series
            });
            chartInstance.off('updateAxisPointer');
            chartInstance.off('dataZoom');
            chartInstance.off('click');
            chartInstance.off('dblclick');
            let chartSeriesClickAt = 0;
            const handleChartPointSelection = (index, signalKey = '', { tracePoint = false } = {}) => {
                if (index < 0) return;
                dismissMobileGestureHint();
                const traceKind = String(signalKey || '').startsWith('component:') ? 'component' : '';
                if (isCompactViewport() && chartPointerLocked) {
                    lockChartPointerAtIndex(index, signalKey);
                    const lockedContext = getFocusContext(getChartDisplayPayload());
                    if (lockedContext?.activeSignal) {
                        openSignalDrawer(lockedContext.activeSignal, lockedContext);
                    } else if (traceKind === 'component') {
                        openComponentDrawer(lockedContext);
                    }
                    return;
                }
                chartPointerLocked = false;
                clearTraceManualPage();
                focusBarIndex(index, signalKey);
                syncCursorIndex(index);
                ensureBarVisible(index, displayPayload);
                const context = buildContext(displayPayload, index, signalKey);
                if (context?.activeSignal) {
                    openSignalDrawer(context.activeSignal, context);
                } else if (traceKind === 'component') {
                    openComponentDrawer(context);
                } else if (tracePoint && context?.activeTraceSignal) {
                    openSignalDrawer(context.activeTraceSignal, context);
                }
            };
            chartInstance.on('updateAxisPointer', (params) => {
                const axisInfo = Array.isArray(params?.axesInfo) ? params.axesInfo[0] : null;
                const axisValue = Number(axisInfo?.value);
                if (Number.isInteger(axisValue) && axisValue >= 0) {
                    syncCursorIndex(axisValue);
                }
            });
            chartInstance.on('dataZoom', (params) => {
                const batch = Array.isArray(params?.batch) && params.batch.length ? params.batch[0] : params;
                const start = Number(batch?.start);
                const end = Number(batch?.end);
                if (!Number.isFinite(start) || !Number.isFinite(end)) return;
                applyChartViewportState({ start, end }, sortedBars.length);
                renderChartBottomBar(displayPayload);
                const nextDensityTier = getChartMarkerDensityTier(sortedBars.length);
                if (nextDensityTier !== chartMarkerDensityTier && lastPayload) {
                    window.requestAnimationFrame(() => renderChart(lastPayload));
                }
            });
            chartInstance.on('click', (params) => {
                if (suppressNextChartClick) {
                    suppressNextChartClick = false;
                    return;
                }
                chartSeriesClickAt = Date.now();
                const index = Number.isInteger(params?.data?.bar_index)
                    ? params.data.bar_index
                    : (Array.isArray(params?.value) && Number.isInteger(params.value[0]) ? params.value[0]
                    : (Number.isInteger(params?.dataIndex) ? params.dataIndex : -1));
                if (index < 0) return;
                const signalKey = params?.data?.signal_id ? String(params.data.signal_id) : '';
                const isTracePoint = String(params?.seriesName || '').startsWith('Flow ') || params?.seriesName === 'Blocked Trace';
                handleChartPointSelection(index, signalKey, { tracePoint: isTracePoint });
            });
            chartInstance.on('dblclick', () => {
                chartPointerLocked = false;
                focusLatestBarAction();
                resetChartViewAction();
            });
            const zr = chartInstance.getZr();
            if (zr) {
                zr.off('globalout');
                zr.on('globalout', () => {
                    clearTransientChartCursor();
                });
                zr.on('click', (event) => {
                    const clickAt = Date.now();
                    window.setTimeout(() => {
                        if (chartSeriesClickAt && chartSeriesClickAt >= clickAt - 40) return;
                        const point = [Number(event?.offsetX ?? event?.zrX), Number(event?.offsetY ?? event?.zrY)];
                        if (!Number.isFinite(point[0]) || !Number.isFinite(point[1])) return;
                        const resolveIndex = (xAxisIndex, yAxisIndex) => {
                            try {
                                const converted = chartInstance.convertFromPixel({ xAxisIndex, yAxisIndex }, point);
                                const rawIndex = Array.isArray(converted) ? converted[0] : converted;
                                const index = Math.round(Number(rawIndex));
                                return Number.isInteger(index) ? clampIndex(index, sortedBars.length) : -1;
                            } catch (_) {
                                return -1;
                            }
                        };
                        if (chartInstance.containPixel({ gridIndex: 1 }, point)) {
                            handleChartPointSelection(resolveIndex(1, 1), '', { tracePoint: true });
                        } else if (chartInstance.containPixel({ gridIndex: 0 }, point)) {
                            handleChartPointSelection(resolveIndex(0, 0), '', { tracePoint: false });
                        }
                    }, 0);
                });
            }
            registerChartTouchInteractions();

            if (focus) {
                if (!preserveCursorIndex || chartPointerLocked) {
                    selectedBarIndex = focus.index;
                }
                if ((!preserveCursorIndex || chartPointerLocked) && !selectedSignalId && focus.activeSignal) {
                    selectedSignalId = String(focus.activeSignal.signal_id || focus.activeSignal.id || '');
                }
                renderInfoRail(displayPayload);
                hoverBarIndex = preserveCursorIndex ? focus.index : -1;
                renderCursorStrip(displayPayload);
                if (chartPointerLocked || hoverBarIndex >= 0) {
                    scheduleChartTooltipSync(focus.index);
                } else {
                    hideChartTooltip();
                }
                if (shouldKeepViewportFollowingFocus(focus.index, displayPayload)) {
                    ensureBarVisible(focus.index, displayPayload);
                }
            }
        }

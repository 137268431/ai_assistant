        function buildTooltipHtml(payload, index) {
            const context = buildContext(payload, index, '');
            const bar = context?.bar || null;
            if (!bar) return '';
            const indicator = context?.indicator || null;
            const isPreviewBar = Boolean(context?.isPreviewBar);
            const signal = context?.activeSignal || null;
            const direction = String(signal?.direction || '').toLowerCase();
            const signalColor = direction === 'short' ? '#FC8181' : '#48BB78';
            const signalText = bar?.preview || bar?.is_preview
                ? 'Forming preview bar · 正式 5m 仍以收盘写入为准'
                : signal
                ? `${escapeHtml(buildTradeSignalLabel(signal))} · ${escapeHtml(String(signal.direction || '--').toUpperCase())}`
                : 'No signal on this bar';
            const traceStage = getTraceStage(context?.trace);
            const traceText = traceStage ? formatTraceDecisionLabel(context?.trace, signal) : '';
            const dtpState = context?.trace ? getTraceDtpState(context.trace) : null;
            const dtpLabel = dtpState && (dtpState.phase || dtpState.dir) ? formatDtpStateLabel(dtpState) : '';
            return `
                <div style="font-family:'JetBrains Mono',monospace;font-size:11px;line-height:1.75;min-width:240px;">
                    <div style="font-size:12px;font-weight:700;color:#E2EAF4;margin-bottom:6px;">${escapeHtml(bar.us_time || '--')}</div>
                    <div>O ${escapeHtml(formatPrice(bar.open))} · H ${escapeHtml(formatPrice(bar.high))}</div>
                    <div>L ${escapeHtml(formatPrice(bar.low))} · C ${escapeHtml(formatPrice(bar.close))}</div>
                    <div>Vol ${escapeHtml(formatNumber(bar.volume || 0, 0))}</div>
                    <div style="margin-top:6px;color:#8BA4C4;">${isPreviewBar && !indicator ? 'EMA / VWAP 待收盘' : `EMA20 ${escapeHtml(formatPrice(indicator?.ema_fast))} · EMA50 ${escapeHtml(formatPrice(indicator?.ema_slow))}`}</div>
                    <div style="color:#8BA4C4;">${isPreviewBar && !indicator ? '指标待收盘确认' : `EMA100 ${escapeHtml(formatPrice(indicator?.ema_trend))} · VWAP ${escapeHtml(formatPrice(indicator?.vwap))}`}</div>
                    <div style="color:#8BA4C4;">${isPreviewBar && !indicator ? '预览 bar 仅 OHLC / Volume' : `CRSI ${escapeHtml(formatNumber(indicator?.crsi))} · OBV ${escapeHtml(formatNumber(indicator?.obv_rsi))} · ATR ${escapeHtml(formatPercent(indicator?.atr_pct))}`}</div>
                    ${dtpLabel ? `<div style="margin-top:6px;color:${escapeHtml(getDtpStateColor(dtpState))};">${escapeHtml(dtpLabel)}</div>` : ''}
                    <div style="margin-top:6px;color:#8BA4C4;">${isPreviewBar && !indicator ? '收盘入库后补齐指标' : `SD ${escapeHtml(getSdZoneText(indicator?.sd_zone))} · ${escapeHtml(getSdTrendText(indicator?.sd_trend))} · Fractal ${escapeHtml(getFractalSummary(indicator))}`}</div>
                    <div style="color:#8BA4C4;">${isPreviewBar && !indicator ? 'Preview bar 仅用于盘中参考' : `Touch ${escapeHtml(getTouchSummary(indicator))} · Div ${escapeHtml(getDivergenceSummary(indicator, 6))}`}</div>
                    ${traceText ? `<div style="margin-top:6px;color:${traceStage === 'blocked' ? '#FDBA74' : traceStage === 'confirmed' ? '#86EFAC' : '#FDE68A'};">${escapeHtml(traceText)}</div>` : ''}
                    <div style="margin-top:6px;color:${signal ? signalColor : bar?.preview || bar?.is_preview ? '#7DD3FC' : '#8BA4C4'};">${signalText}</div>
                </div>
            `;
        }

        function resolveChartTooltipPosition(point, size) {
            const viewWidth = Number(size?.viewSize?.[0] || 0);
            const viewHeight = Number(size?.viewSize?.[1] || 0);
            const contentWidth = Number(size?.contentSize?.[0] || 0);
            const contentHeight = Number(size?.contentSize?.[1] || 0);
            const padding = isCompactViewport() ? 12 : 16;
            if (!Array.isArray(point) || point.length < 2 || !viewWidth || !viewHeight) {
                return [padding, padding];
            }
            if (isCompactViewport()) {
                return [Math.max(padding, viewWidth - contentWidth - padding), padding];
            }
            const anchorX = Number(point[0] || 0);
            const anchorY = Number(point[1] || 0);
            let left = anchorX + padding;
            if (left + contentWidth > viewWidth - padding) {
                left = Math.max(padding, anchorX - contentWidth - padding);
            }
            const maxTop = Math.max(padding, viewHeight - contentHeight - padding);
            const signalFlowGuard = Math.round(viewHeight * 0.48);
            const topCandidate = anchorY < signalFlowGuard
                ? padding
                : Math.max(padding, anchorY - contentHeight - padding);
            const top = Math.min(maxTop, Math.min(topCandidate, signalFlowGuard - Math.min(contentHeight, 220)));
            return [left, top];
        }


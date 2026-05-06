        function formatChartLabel(item) {
            if (item?.us_time) return String(item.us_time).slice(5, 16);
            const ms = Number(item?.bar_time_ms || 0);
            if (!ms) return '--';
            const date = new Date(ms);
            if (Number.isNaN(date.getTime())) return '--';
            return `${String(date.getUTCMonth() + 1).padStart(2, '0')}-${String(date.getUTCDate()).padStart(2, '0')} ${String(date.getUTCHours()).padStart(2, '0')}:${String(date.getUTCMinutes()).padStart(2, '0')}`;
        }

        function supportsSessionDividers(interval = currentInterval) {
            return ['5m', '15m', '30m'].includes(normalizeInterval(interval || currentInterval));
        }

        function getBarEtParts(item) {
            const source = String(item?.us_time || '').trim()
                || formatBarTimeMsToET(Number(item?.bar_time_ms || 0));
            const match = source.match(/^(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2})/);
            if (!match) return null;
            return {
                date: `${match[1]}-${match[2]}-${match[3]}`,
                hour: Number(match[4]),
                minute: Number(match[5]),
            };
        }

        function getUsSessionKey(item) {
            const parts = getBarEtParts(item);
            if (!parts) return '';
            const minuteOfDay = parts.hour * 60 + parts.minute;
            if (minuteOfDay >= 4 * 60 && minuteOfDay < 9 * 60 + 30) return 'pre';
            if (minuteOfDay >= 9 * 60 + 30 && minuteOfDay < 16 * 60) return 'regular';
            if (minuteOfDay >= 16 * 60 && minuteOfDay < 20 * 60) return 'post';
            return '';
        }

        function getUsSessionMeta(sessionKey) {
            const map = {
                pre: {
                    label: '盘前',
                    color: '#38BDF8',
                    borderColor: 'rgba(56, 189, 248, 0.56)',
                    dash: 'dashed',
                    opacity: 0.74,
                },
                regular: {
                    label: '盘中',
                    color: '#F59E0B',
                    borderColor: 'rgba(245, 158, 11, 0.60)',
                    dash: 'solid',
                    opacity: 0.82,
                },
                post: {
                    label: '盘后',
                    color: '#FB7185',
                    borderColor: 'rgba(251, 113, 133, 0.58)',
                    dash: 'dashed',
                    opacity: 0.78,
                },
            };
            return map[sessionKey] || null;
        }

        function buildSessionDividerMarkLineItems(bars, options = {}) {
            if (!supportsSessionDividers() || !Array.isArray(bars) || !bars.length) return [];
            const showLabels = Boolean(options.showLabels);
            const items = [];
            let previousSessionKey = '';
            let previousDate = '';
            bars.forEach((bar, index) => {
                const parts = getBarEtParts(bar);
                const sessionKey = getUsSessionKey(bar);
                if (!parts || !sessionKey) return;
                const dateChanged = parts.date !== previousDate;
                const sessionChanged = sessionKey !== previousSessionKey;
                if (!dateChanged && !sessionChanged) {
                    previousDate = parts.date;
                    previousSessionKey = sessionKey;
                    return;
                }
                const meta = getUsSessionMeta(sessionKey);
                if (!meta) {
                    previousDate = parts.date;
                    previousSessionKey = sessionKey;
                    return;
                }
                items.push({
                    xAxis: index,
                    name: meta.label,
                    lineStyle: {
                        color: meta.color,
                        type: meta.dash,
                        width: sessionKey === 'regular' ? 1.2 : 1,
                        opacity: dateChanged ? meta.opacity : Math.max(meta.opacity - 0.1, 0.45),
                    },
                    label: showLabels ? {
                        show: true,
                        formatter: meta.label,
                        position: 'insideEndTop',
                        distance: 8,
                        color: meta.color,
                        fontSize: 10,
                        fontWeight: 700,
                        fontFamily: 'JetBrains Mono, monospace',
                        backgroundColor: 'rgba(7,12,20,0.88)',
                        borderColor: meta.borderColor,
                        borderWidth: 1,
                        borderRadius: 6,
                        padding: [3, 6],
                    } : { show: false },
                });
                previousDate = parts.date;
                previousSessionKey = sessionKey;
            });
            return items;
        }

        function buildLatestPriceMarkLineItem(latestClose, latestLineColor) {
            if (!(Number.isFinite(latestClose) && latestClose > 0)) return null;
            return {
                yAxis: latestClose,
                lineStyle: {
                    color: latestLineColor,
                    type: 'dashed',
                    opacity: 0.78,
                    width: 1,
                },
                label: {
                    show: true,
                    position: 'insideEndTop',
                    distance: 8,
                    formatter: ` 最新 ${formatPrice(latestClose)} `,
                    color: latestLineColor,
                    backgroundColor: 'rgba(7,12,20,0.88)',
                    borderRadius: 6,
                    padding: [4, 6],
                },
            };
        }

        function buildMarkLineConfig(items) {
            if (!Array.isArray(items) || !items.length) return undefined;
            return {
                symbol: ['none', 'none'],
                silent: true,
                animation: false,
                emphasis: { disabled: true },
                data: items,
            };
        }

        function buildFocusMarkPointConfig(index, payload = getChartDisplayPayload()) {
            const bars = Array.isArray(payload?.bars) ? payload.bars : [];
            if (!bars.length || !Number.isInteger(index) || index < 0) return undefined;
            const context = buildContext(payload, index, selectedSignalId);
            const closeValue = Number(context?.bar?.close || 0);
            if (!context?.bar || !Number.isFinite(closeValue) || closeValue <= 0) return undefined;
            return {
                symbol: 'circle',
                symbolSize: 13,
                silent: true,
                label: { show: false },
                itemStyle: {
                    color: '#7DD3FC',
                    borderColor: 'rgba(8,12,20,0.96)',
                    borderWidth: 2,
                    shadowBlur: 12,
                    shadowColor: 'rgba(125,211,252,0.38)'
                },
                data: [{
                    coord: [context.index, closeValue],
                    value: 'focus'
                }],
            };
        }


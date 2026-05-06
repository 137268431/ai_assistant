        function destroyReplayChart() {
            if (replayChart) {
                replayChart.destroy();
                replayChart = null;
            }
        }

        function renderReplay(rows) {
            destroyReplayChart();
            const panel = document.getElementById('replayPanel');
            if (!rows.length) {
                panel.innerHTML = '<div class="empty-state">选择 symbol 后 Replay。</div>';
                const ctx = document.getElementById('replayChart').getContext('2d');
                ctx.clearRect(0, 0, ctx.canvas.width, ctx.canvas.height);
                ctx.font = '12px JetBrains Mono';
                ctx.fillStyle = '#8BA4C4';
                ctx.fillText('No replay loaded', 16, 28);
                return;
            }
            const replayPreview = getBacktestTablePreview('replay', rows);
            const labels = rows.map((row) => (row.us_time || '').slice(11, 16));
            const closes = rows.map((row) => Number(row.close || 0));
            const signalPoints = rows.map((row) => row.signal ? Number(row.close || 0) : null);
            const ctx = document.getElementById('replayChart').getContext('2d');
            replayChart = new Chart(ctx, {
                type: 'line',
                data: {
                    labels,
                    datasets: [
                        {
                            label: 'Close',
                            data: closes,
                            borderColor: '#63B3ED',
                            backgroundColor: 'rgba(99,179,237,0.14)',
                            borderWidth: 2,
                            pointRadius: 0,
                            tension: 0.16,
                        },
                        {
                            type: 'scatter',
                            label: 'Signal Bar',
                            data: signalPoints,
                            borderColor: '#F6AD55',
                            backgroundColor: '#F6AD55',
                            pointRadius: 4,
                            pointHoverRadius: 5,
                        },
                    ],
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        legend: { labels: { color: '#E2EAF4' } },
                    },
                    scales: {
                        x: {
                            ticks: { color: '#8BA4C4', maxTicksLimit: 10 },
                            grid: { color: 'rgba(255,255,255,0.05)' },
                        },
                        y: {
                            ticks: { color: '#8BA4C4' },
                            grid: { color: 'rgba(255,255,255,0.05)' },
                        },
                    },
                },
            });
            panel.innerHTML = `
                ${renderBacktestTablePreviewBar('replay', replayPreview, '根K线')}
                <div class="table-wrap">
                    <table class="data-table">
                        <thead>
                            <tr>
                                <th>US Time</th>
                                <th>Session</th>
                                <th>OHLC</th>
                                <th>ATR</th>
                                <th>SD Zone</th>
                                <th>DTP</th>
                                <th>Signal</th>
                            </tr>
                        </thead>
                        <tbody>
                            ${replayPreview.rows.map((row) => {
                                const signal = row.signal || null;
                                return `
                                    <tr>
                                        <td class="mono">${escapeHtml((row.us_time || '--').slice(0, 16))}</td>
                                        <td>${escapeHtml(row.session_type || '--')}</td>
                                        <td class="mono">O ${formatNumber(row.open, 2)} / H ${formatNumber(row.high, 2)}<br>L ${formatNumber(row.low, 2)} / C ${formatNumber(row.close, 2)}</td>
                                        <td>${escapeHtml(formatNumber(row.atr, 3))}</td>
                                        <td>${escapeHtml(row.sd_zone || '--')}<br><span class="mono">trend ${escapeHtml(String(row.sd_trend ?? '--'))}</span></td>
                                        <td>${escapeHtml(row.dtp_phase || '--')}</td>
                                        <td>${signal ? `<span class="tag signal">${escapeHtml((signal.signal || '--').toUpperCase())}</span><br>${escapeHtml(signal.direction || '--')} · ${escapeHtml(signal.reason || '')}` : '--'}</td>
                                    </tr>
                                `;
                            }).join('')}
                        </tbody>
                    </table>
                </div>
            `;
        }

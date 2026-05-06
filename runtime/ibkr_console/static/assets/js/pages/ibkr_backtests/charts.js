        function destroyEquityChart() {
            if (equityChart) {
                equityChart.destroy();
                equityChart = null;
            }
        }

        function renderEquityChart(run) {
            destroyEquityChart();
            const extra = run.extra || {};
            const equityCurve = Array.isArray(extra.equity_curve) ? extra.equity_curve : [];
            const benchmarkCurve = Array.isArray(extra.benchmark_curve) ? extra.benchmark_curve : [];
            const labels = Array.from(new Set([...equityCurve.map((item) => item.date), ...benchmarkCurve.map((item) => item.date)])).sort();
            const equityMap = new Map(equityCurve.map((item) => [item.date, Number(item.equity || 0)]));
            const benchmarkMap = new Map(benchmarkCurve.map((item) => [item.date, Number(item.equity || 0)]));
            const ctx = document.getElementById('equityChart').getContext('2d');
            if (!labels.length) {
                ctx.clearRect(0, 0, ctx.canvas.width, ctx.canvas.height);
                ctx.font = '12px JetBrains Mono';
                ctx.fillStyle = '#8BA4C4';
                ctx.fillText('No equity curve yet for this run', 16, 28);
                return;
            }
            equityChart = new Chart(ctx, {
                type: 'line',
                data: {
                    labels,
                    datasets: [
                        {
                            label: 'Strategy Equity',
                            data: labels.map((label) => equityMap.get(label) ?? null),
                            borderColor: '#F6AD55',
                            backgroundColor: 'rgba(246,173,85,0.16)',
                            borderWidth: 2.2,
                            pointRadius: 0,
                            tension: 0.24,
                        },
                        {
                            label: `${run.benchmark_symbol || 'SPY'} Benchmark`,
                            data: labels.map((label) => benchmarkMap.get(label) ?? null),
                            borderColor: '#63B3ED',
                            backgroundColor: 'rgba(99,179,237,0.12)',
                            borderDash: [6, 4],
                            borderWidth: 1.8,
                            pointRadius: 0,
                            tension: 0.18,
                        },
                    ],
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    interaction: { mode: 'index', intersect: false },
                    plugins: {
                        legend: { labels: { color: '#E2EAF4' } },
                    },
                    scales: {
                        x: {
                            ticks: { color: '#8BA4C4', maxTicksLimit: 8 },
                            grid: { color: 'rgba(255,255,255,0.05)' },
                        },
                        y: {
                            ticks: { color: '#8BA4C4' },
                            grid: { color: 'rgba(255,255,255,0.05)' },
                        },
                    },
                },
            });
        }

function parseMaybeJson(value, fallback) {
    if (!value) return fallback;
    if (typeof value === 'object') return value;
    try {
        const parsed = JSON.parse(value);
        return parsed && typeof parsed === 'object' ? parsed : fallback;
    } catch (_) {
        return fallback;
    }
}

function formatPct(value) {
    const num = Number(value || 0);
    const prefix = num > 0 ? '+' : '';
    return `${prefix}${num.toFixed(2)}%`;
}

function formatNum(value) {
    const num = Number(value || 0);
    return Number.isFinite(num) ? num.toFixed(2) : '--';
}

function renderBacktests(batches, runs) {
    const area = document.getElementById('backtestArea');
    const countEl = document.getElementById('backtestCount');
    const batch = batches && batches[0] ? batches[0] : null;
    const run = runs && runs[0] ? runs[0] : null;
    const cards = [];

    if (batch) {
        cards.push(`
            <div class="backtest-card">
                <div class="backtest-kicker">Latest Batch</div>
                <div class="backtest-title">${escapeHtml(String(batch.name || batch.batch_id || batch.id || 'batch'))}</div>
                <div class="backtest-meta">
                    状态 ${escapeHtml(String(batch.status || '--'))}<br>
                    变体 ${escapeHtml(String(batch.completed_count || 0))}/${escapeHtml(String(batch.variant_count || 0))}<br>
                    Best Return ${escapeHtml(formatPct(batch.best_total_return_pct || 0))}<br>
                    Best Sharpe ${escapeHtml(formatNum(batch.best_sharpe || 0))}
                </div>
            </div>
        `);
    }

    if (run) {
        const metrics = parseMaybeJson(run.metrics, {});
        cards.push(`
            <div class="backtest-card">
                <div class="backtest-kicker">Latest Run</div>
                <div class="backtest-title">${escapeHtml(String(run.name || run.run_id || run.id || 'run'))}</div>
                <div class="backtest-meta">
                    状态 ${escapeHtml(String(run.status || '--'))}<br>
                    Return ${escapeHtml(formatPct(run.total_return_pct || metrics.total_return_pct || 0))}<br>
                    Sharpe ${escapeHtml(formatNum(run.sharpe || metrics.sharpe || 0))}<br>
                    Max DD ${escapeHtml(formatPct(-Math.abs(Number(run.max_drawdown_pct || metrics.max_drawdown_pct || 0))))}
                </div>
            </div>
        `);
    }

    countEl.textContent = String((batch ? 1 : 0) + (run ? 1 : 0));
    if (!cards.length) {
        area.innerHTML = '<div class="loading-text">当前环境暂无回测统计</div>';
        return;
    }
    area.innerHTML = `<div class="backtest-grid">${cards.join('')}</div>`;
}

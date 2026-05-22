// Static smoke marker: ops summary link markup lives in renderers.js (ops-summary-link /ibkr_monitor.html).
function scheduleSystemRefresh() {
    if (refreshTimer) {
        clearTimeout(refreshTimer);
        refreshTimer = null;
    }
    const jitterMs = Math.floor(Math.random() * 5000);
    refreshTimer = setTimeout(async () => {
        refreshTimer = null;
        if (document.visibilityState !== 'hidden') {
            await loadSystemData(false);
        }
        scheduleSystemRefresh();
    }, 60000 + jitterMs);
}

document.addEventListener('DOMContentLoaded', async () => {
    if (!ensureIbkrPageAuth()) return;
    window.addEventListener('pagehide', () => {
        if (refreshTimer) clearTimeout(refreshTimer);
        refreshTimer = null;
    });
    currentFocus = String(new URLSearchParams(window.location.search).get('focus') || '').trim().toLowerCase();
    document.getElementById('nav').innerHTML = renderNav('/ibkr_system.html');
    document.getElementById('contextBar').innerHTML = renderPageContextBar('🖥️ IBKR 总览', {
        subtitle: '健康 / 数据新鲜度 / 配置',
    });
    document.getElementById('pageBridge').innerHTML = renderSystemBridge('/ibkr_system.html');
    ensureSystemPageTopSection();
    document.getElementById('configLink').href = buildPageUrl('/ibkr_config.html', {}, { allowGlobal: true, environment: currentEnvironment });
    await loadSystemData(false);
    applyFocusTarget();
    scheduleSystemRefresh();
});

window.onEnvironmentChange = function(environment) {
    currentEnvironment = environment;
    const params = currentFocus === 'stats' ? { focus: 'stats' } : {};
    window.location.href = buildPageUrl('/ibkr_system.html', params, { environment: currentEnvironment });
};

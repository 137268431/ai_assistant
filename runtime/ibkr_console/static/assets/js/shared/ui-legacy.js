// ── 通用 CSS 样式 ──
function getCommonStyles() {
  return '<link rel="stylesheet" href="/assets/css/common.css">';
}

// ── 全局 Loading 遮罩层 ──
function showLoading(text = '加载中...') {
  let loading = document.getElementById('globalLoading');
  if (!loading) {
    loading = document.createElement('div');
    loading.id = 'globalLoading';
    loading.className = 'global-loading hidden';
    loading.innerHTML = `
      <div class="spinner"></div>
      <div class="text">${text}</div>
    `;
    document.body.appendChild(loading);
  }
  loading.querySelector('.text').textContent = text;
  loading.classList.remove('hidden');
}

function hideLoading() {
  const loading = document.getElementById('globalLoading');
  if (loading) {
    loading.classList.add('hidden');
  }
}

// 自动管理 Loading 状态的包装函数
function withLoading(promise, text = '加载中...') {
  showLoading(text);
  return promise.finally(() => hideLoading());
}

/**
 * 创建带防重入 guard 的加载函数
 * 用法：const loadFn = guardedLoader(async () => { ... }, '加载中...')
 * 之后用 loadFn() 替代原始调用，重复调用会被忽略
 */
function guardedLoader(asyncFn, loadingText) {
  let isRunning = false;
  return async function() {
    if (isRunning) return;
    isRunning = true;
    showLoading(loadingText || '加载中...');
    try {
      await asyncFn();
    } finally {
      isRunning = false;
      hideLoading();
    }
  };
}

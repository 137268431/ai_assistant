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

function escapeConfirmDialogHtml(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function ensureConfirmDialogStyles() {
  if (document.getElementById('appConfirmDialogStyles')) return;
  const style = document.createElement('style');
  style.id = 'appConfirmDialogStyles';
  style.textContent = `
    .app-confirm-overlay {
      position: fixed;
      inset: 0;
      z-index: 2400;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 18px;
      background:
        radial-gradient(circle at 18% 18%, rgba(79, 209, 197, 0.14), transparent 30%),
        radial-gradient(circle at 82% 18%, rgba(246, 173, 85, 0.12), transparent 32%),
        rgba(2, 6, 16, 0.76);
      backdrop-filter: blur(14px);
    }
    .app-confirm-card {
      width: min(540px, 100%);
      border-radius: 20px;
      border: 1px solid rgba(147, 197, 253, 0.18);
      background:
        linear-gradient(180deg, rgba(11, 22, 34, 0.98), rgba(6, 12, 22, 0.98));
      box-shadow: 0 28px 80px rgba(0, 0, 0, 0.42);
      color: var(--text, #E6F4FF);
      overflow: hidden;
      transform: translateY(0);
      animation: appConfirmEnter 0.16s ease-out;
    }
    .app-confirm-head {
      padding: 18px 20px 14px;
      border-bottom: 1px solid rgba(147, 197, 253, 0.12);
      background:
        radial-gradient(circle at left top, rgba(79, 209, 197, 0.12), transparent 42%),
        rgba(255, 255, 255, 0.02);
    }
    .app-confirm-kicker {
      color: var(--accent, #4FD1C5);
      font-family: var(--font-mono, 'JetBrains Mono', monospace);
      font-size: 10px;
      letter-spacing: 0.14em;
      text-transform: uppercase;
      font-weight: 700;
    }
    .app-confirm-title {
      margin-top: 7px;
      font-size: 20px;
      line-height: 1.25;
      font-weight: 800;
      letter-spacing: -0.03em;
    }
    .app-confirm-body {
      padding: 16px 20px 18px;
      display: grid;
      gap: 12px;
    }
    .app-confirm-message {
      color: var(--muted, #8FA3B7);
      font-size: 13px;
      line-height: 1.65;
      white-space: pre-line;
    }
    .app-confirm-detail-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px;
    }
    .app-confirm-detail {
      border-radius: 12px;
      border: 1px solid rgba(147, 197, 253, 0.12);
      background: rgba(8, 14, 24, 0.74);
      padding: 9px 10px;
      min-width: 0;
    }
    .app-confirm-detail-label {
      color: var(--muted, #8FA3B7);
      font-family: var(--font-mono, 'JetBrains Mono', monospace);
      font-size: 10px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }
    .app-confirm-detail-value {
      margin-top: 5px;
      color: var(--text, #E6F4FF);
      font-size: 13px;
      font-weight: 800;
      word-break: break-word;
    }
    .app-confirm-input {
      width: 100%;
      border-radius: 12px;
      border: 1px solid rgba(147, 197, 253, 0.16);
      background: rgba(2, 8, 18, 0.9);
      color: var(--text, #E6F4FF);
      padding: 12px 13px;
      font-family: var(--font-mono, 'JetBrains Mono', monospace);
      outline: none;
    }
    .app-confirm-input:focus {
      border-color: rgba(79, 209, 197, 0.5);
      box-shadow: 0 0 0 3px rgba(79, 209, 197, 0.12);
    }
    .app-confirm-actions {
      display: flex;
      justify-content: flex-end;
      gap: 10px;
      flex-wrap: wrap;
      padding-top: 2px;
    }
    .app-confirm-btn {
      min-width: 112px;
      border: 1px solid rgba(147, 197, 253, 0.14);
      border-radius: 12px;
      padding: 11px 14px;
      background: rgba(255, 255, 255, 0.04);
      color: var(--text, #E6F4FF);
      font-family: var(--font-mono, 'JetBrains Mono', monospace);
      font-size: 12px;
      font-weight: 800;
      cursor: pointer;
    }
    .app-confirm-btn.primary {
      border-color: rgba(79, 209, 197, 0.26);
      background: linear-gradient(180deg, rgba(79, 209, 197, 0.24), rgba(79, 209, 197, 0.12));
      color: #DFFFFB;
    }
    .app-confirm-overlay.tone-danger .app-confirm-kicker,
    .app-confirm-overlay.tone-danger .app-confirm-detail-value {
      color: var(--short, #FC8181);
    }
    .app-confirm-overlay.tone-danger .app-confirm-btn.primary {
      border-color: rgba(252, 129, 129, 0.32);
      background: linear-gradient(180deg, rgba(252, 129, 129, 0.26), rgba(252, 129, 129, 0.13));
      color: #FFE1E1;
    }
    .app-confirm-overlay.tone-warn .app-confirm-kicker {
      color: var(--pending, #F6AD55);
    }
    .app-confirm-overlay.tone-warn .app-confirm-btn.primary {
      border-color: rgba(246, 173, 85, 0.32);
      background: linear-gradient(180deg, rgba(246, 173, 85, 0.24), rgba(246, 173, 85, 0.12));
      color: #FFE8C2;
    }
    .app-confirm-btn:disabled {
      opacity: 0.48;
      cursor: not-allowed;
    }
    @keyframes appConfirmEnter {
      from { opacity: 0; transform: translateY(8px) scale(0.98); }
      to { opacity: 1; transform: translateY(0) scale(1); }
    }
    @media (max-width: 560px) {
      .app-confirm-card { border-radius: 16px; }
      .app-confirm-head, .app-confirm-body { padding-left: 14px; padding-right: 14px; }
      .app-confirm-detail-grid { grid-template-columns: 1fr; }
      .app-confirm-actions { flex-direction: column-reverse; }
      .app-confirm-btn { width: 100%; }
    }
  `;
  document.head.appendChild(style);
}

function renderConfirmDialogDetails(details) {
  if (!Array.isArray(details) || !details.length) return '';
  return `
    <div class="app-confirm-detail-grid">
      ${details.map((item) => {
        if (typeof item === 'string') {
          return `
            <div class="app-confirm-detail">
              <div class="app-confirm-detail-value">${escapeConfirmDialogHtml(item)}</div>
            </div>
          `;
        }
        return `
          <div class="app-confirm-detail">
            <div class="app-confirm-detail-label">${escapeConfirmDialogHtml(item.label || '')}</div>
            <div class="app-confirm-detail-value">${escapeConfirmDialogHtml(item.value || '')}</div>
          </div>
        `;
      }).join('')}
    </div>
  `;
}

function showConfirmDialog(options = {}) {
  if (typeof document === 'undefined') {
    return Promise.resolve(typeof confirm === 'function' ? confirm(options.message || options.title || '确认操作？') : true);
  }
  ensureConfirmDialogStyles();
  const title = options.title || '确认操作';
  const kicker = options.kicker || 'Confirm';
  const message = options.message || '';
  const confirmText = options.confirmText || '确认';
  const cancelText = options.cancelText || '取消';
  const tone = ['danger', 'warn', 'info'].includes(String(options.tone || '').toLowerCase())
    ? String(options.tone).toLowerCase()
    : 'info';
  const requireText = String(options.requireText || '');

  return new Promise((resolve) => {
    const existing = document.getElementById('appConfirmDialog');
    if (existing) existing.remove();

    const overlay = document.createElement('div');
    overlay.id = 'appConfirmDialog';
    overlay.className = `app-confirm-overlay tone-${tone}`;
    overlay.setAttribute('role', 'dialog');
    overlay.setAttribute('aria-modal', 'true');
    overlay.innerHTML = `
      <div class="app-confirm-card" role="document">
        <div class="app-confirm-head">
          <div class="app-confirm-kicker">${escapeConfirmDialogHtml(kicker)}</div>
          <div class="app-confirm-title">${escapeConfirmDialogHtml(title)}</div>
        </div>
        <div class="app-confirm-body">
          ${message ? `<div class="app-confirm-message">${escapeConfirmDialogHtml(message)}</div>` : ''}
          ${renderConfirmDialogDetails(options.details || [])}
          ${requireText ? `<input class="app-confirm-input" data-confirm-input placeholder="输入 ${escapeConfirmDialogHtml(requireText)} 继续">` : ''}
          <div class="app-confirm-actions">
            <button class="app-confirm-btn" type="button" data-confirm-cancel>${escapeConfirmDialogHtml(cancelText)}</button>
            <button class="app-confirm-btn primary" type="button" data-confirm-ok ${requireText ? 'disabled' : ''}>${escapeConfirmDialogHtml(confirmText)}</button>
          </div>
        </div>
      </div>
    `;

    const cleanup = (value) => {
      document.removeEventListener('keydown', onKeyDown);
      overlay.remove();
      resolve(value);
    };
    const onKeyDown = (event) => {
      if (event.key === 'Escape') cleanup(false);
      if (event.key === 'Enter') {
        const okButton = overlay.querySelector('[data-confirm-ok]');
        if (okButton && !okButton.disabled) cleanup(true);
      }
    };
    overlay.addEventListener('click', (event) => {
      if (event.target === overlay) cleanup(false);
    });
    overlay.querySelector('[data-confirm-cancel]')?.addEventListener('click', () => cleanup(false));
    overlay.querySelector('[data-confirm-ok]')?.addEventListener('click', () => cleanup(true));
    const input = overlay.querySelector('[data-confirm-input]');
    const okButton = overlay.querySelector('[data-confirm-ok]');
    if (input && okButton) {
      input.addEventListener('input', () => {
        okButton.disabled = String(input.value || '').trim() !== requireText;
      });
    }
    document.addEventListener('keydown', onKeyDown);
    document.body.appendChild(overlay);
    window.setTimeout(() => {
      (input || okButton || overlay.querySelector('[data-confirm-cancel]'))?.focus();
    }, 0);
  });
}

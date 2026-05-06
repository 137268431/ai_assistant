    function escapeHtml(value) {
      return String(value ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
    }

    function initAuth() {
      try {
        requireAuth(`${location.pathname}${location.search}`);
        return true;
      } catch (_) {
        return false;
      }
    }

    async function requestJson(path, { method = 'GET', body = null } = {}) {
      const token = getToken();
      const headers = { 'Content-Type': 'application/json' };
      if (token) headers.Authorization = `Bearer ${token}`;
      const response = await fetchWithRetry(`${BASE_URL}${path}`, {
        method,
        headers,
        body: body ? JSON.stringify(body) : null
      }, {
        attempts: 3,
        retryDelayMs: 500
      });
      const text = await response.text();
      let payload = {};
      try {
        payload = text ? JSON.parse(text) : {};
      } catch (_) {
        payload = { ok: false, raw: text };
      }
      if (response.status === 401 || response.status === 403) {
        handleAuthError();
        throw new Error('Authentication failed');
      }
      if (!response.ok) {
        throw new Error(payload.message || payload.error || `Request failed (${response.status})`);
      }
      return payload;
    }

    async function deleteRecord(collection, recordId) {
      const token = getToken();
      const headers = {};
      if (token) headers.Authorization = `Bearer ${token}`;
      const response = await fetch(`${PB_AUTH_BASE_URL}/api/collections/${collection}/records/${recordId}`, {
        method: 'DELETE',
        headers
      });
      if (response.status === 401 || response.status === 403) {
        handleAuthError();
        throw new Error('Authentication failed');
      }
      if (!response.ok) {
        let payload = {};
        try {
          payload = await response.json();
        } catch (_) {
          payload = {};
        }
        throw new Error(payload.message || payload.error || `Delete failed (${response.status})`);
      }
    }

    function getRuntimeWarning(payload) {
      const reconcile = payload?.runtime_reconcile;
      if (reconcile && reconcile.ok === false) {
        return reconcile.error || 'runtime reconcile failed';
      }
      return '';
    }

    function getWatchlistSyncWarning(payload) {
      const sync = payload?.watchlist_sync;
      if (sync && sync.ok === false) {
        return sync.error || 'watchlist sync failed';
      }
      return '';
    }

    function buildQuery(params) {
      const query = new URLSearchParams();
      Object.entries(params || {}).forEach(([key, value]) => {
        if (value !== undefined && value !== null && value !== '') {
          query.set(key, String(value));
        }
      });
      const text = query.toString();
      return text ? `?${text}` : '';
    }

    function formatNumber(value, digits = 0) {
      const num = Number(value);
      if (!Number.isFinite(num)) return '--';
      return num.toLocaleString('en-US', {
        minimumFractionDigits: digits,
        maximumFractionDigits: digits
      });
    }

    function formatPrice(value) {
      const num = Number(value);
      if (!Number.isFinite(num) || num <= 0) return '--';
      return `$${formatNumber(num, 2)}`;
    }

    function formatPct(value) {
      const num = Number(value);
      if (!Number.isFinite(num)) return '--';
      const prefix = num > 0 ? '+' : '';
      return `${prefix}${formatNumber(num, 2)}%`;
    }

    function formatVolume(value) {
      const num = Number(value);
      if (!Number.isFinite(num) || num <= 0) return '--';
      if (num >= 1000000) return `${formatNumber(num / 1000000, 2)}M`;
      if (num >= 1000) return `${formatNumber(num / 1000, 1)}K`;
      return formatNumber(num, 0);
    }

    function formatFreshness(value) {
      const num = Number(value);
      if (!Number.isFinite(num) || num < 0) return '无当日bar';
      if (num < 60) return `${num} min`;
      return `${formatNumber(num / 60, 1)} h`;
    }

    function dataQualityChip(row) {
      const quality = row?.data_quality && typeof row.data_quality === 'object' ? row.data_quality : {};
      const status = String(quality.status || '').trim().toLowerCase();
      if (!status) return '';
      if (status === 'ready') return statusChip('数据完整', 'active');
      const count = Array.isArray(quality.needs_repair_intervals) ? quality.needs_repair_intervals.length : 0;
      return statusChip(`补偿中${count ? ` ${count}TF` : ''}`, status === 'stale' ? 'candidate' : 'stale');
    }

    function getUsDate() {
      return new Intl.DateTimeFormat('en-CA', {
        timeZone: 'America/New_York',
        year: 'numeric',
        month: '2-digit',
        day: '2-digit'
      }).format(new Date());
    }


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

    function requestCachedJson(path, requestOptions = {}, cacheOptions = {}) {
      const method = String(requestOptions.method || 'GET').toUpperCase();
      if (method === 'GET' && typeof cachedCustomJson === 'function') {
        return cachedCustomJson(path, '', requestOptions, cacheOptions);
      }
      return requestJson(path, requestOptions);
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

    function isPlainObject(value) {
      return Boolean(value && typeof value === 'object' && !Array.isArray(value));
    }

    function parseMaybeObject(value) {
      if (isPlainObject(value)) return value;
      if (typeof value !== 'string') return {};
      const text = value.trim();
      if (!text || text[0] !== '{') return {};
      try {
        const parsed = JSON.parse(text);
        return isPlainObject(parsed) ? parsed : {};
      } catch (_) {
        return {};
      }
    }

    function isDisplayValue(value) {
      if (value === undefined || value === null) return false;
      if (typeof value === 'string' && !value.trim()) return false;
      if (Array.isArray(value) && value.length === 0) return false;
      if (isPlainObject(value) && Object.keys(value).length === 0) return false;
      return true;
    }

    function firstDisplayValue(values) {
      for (const value of values || []) {
        if (isDisplayValue(value)) return value;
      }
      return undefined;
    }

    function normalizeTruth(value) {
      if (value === true || value === 1) return true;
      if (value === false || value === 0) return false;
      const text = String(value ?? '').trim().toLowerCase();
      if (['true', 'yes', 'y', '1', 'on', 'needed', 'needs_backfill', 'backfill_needed', 'stale', 'repair'].includes(text)) return true;
      if (['false', 'no', 'n', '0', 'off', 'ok', 'ready', 'clean'].includes(text)) return false;
      return Boolean(text);
    }

    function getRowExtra(row) {
      return parseMaybeObject(row?.extra);
    }

    function getRowScreenerSnapshot(row) {
      const extra = getRowExtra(row);
      return parseMaybeObject(extra.screener_snapshot);
    }

    function getRowAdmissionPayload(row) {
      const extra = getRowExtra(row);
      const direct = parseMaybeObject(row?.admission);
      return isDisplayValue(direct) ? direct : parseMaybeObject(extra.intraday_window_admission);
    }

    const FUNDAMENTALS_PROFILE_HINT = '基础面扩展待补全';
    const SAFE_PROFILE_KEYS = [
      'profile',
      'name',
      'source',
      'provider',
      'sector',
      'industry',
      'country',
      'beta',
      'market_cap',
      'market_cap_usd',
      'market_cap_tier',
      'shares_float',
      'float_shares',
      'shares_outstanding',
      'share_outstanding_millions',
      'short_float_pct',
      'avg_volume_10d_provider',
      'avg_10d_volume',
      'liquidity_tier',
      'activity_profile',
      'float_profile',
      'short_interest_profile',
    ];

    function getSymbolProfile(row) {
      const extra = getRowExtra(row);
      const snapshot = getRowScreenerSnapshot(row);
      const admission = parseMaybeObject(extra.intraday_window_admission);
      const eligibility = parseMaybeObject(row?.eligibility);
      return firstDisplayValue([
        row?.symbol_profile,
        row?.fundamentals_profile,
        row?.fundamentals,
        row?.symbol_fundamentals,
        row?.profile,
        eligibility.symbol_profile,
        eligibility.fundamentals_profile,
        snapshot.symbol_profile,
        snapshot.fundamentals_profile,
        snapshot.fundamentals,
        extra.symbol_profile,
        extra.fundamentals_profile,
        extra.fundamentals,
        admission.symbol_profile,
        admission.profile,
      ]);
    }

    function getSafeSymbolProfile(row) {
      const extra = getRowExtra(row);
      const snapshot = getRowScreenerSnapshot(row);
      const admission = parseMaybeObject(extra.intraday_window_admission);
      const eligibility = parseMaybeObject(row?.eligibility);
      const candidates = [
        row?.fundamentals,
        row?.symbol_fundamentals,
        row?.symbol_profile,
        row?.fundamentals_profile,
        eligibility.symbol_profile,
        eligibility.fundamentals_profile,
        snapshot.symbol_profile,
        snapshot.fundamentals_profile,
        snapshot.fundamentals,
        extra.symbol_profile,
        extra.fundamentals_profile,
        extra.fundamentals,
        admission.profile,
        admission.symbol_profile,
      ];
      const safe = {};
      candidates.forEach((candidate) => {
        const parsed = parseMaybeObject(candidate);
        if (!isDisplayValue(parsed)) return;
        const nestedExtra = parseMaybeObject(parsed.extra);
        const combined = { ...nestedExtra, ...parsed };
        SAFE_PROFILE_KEYS.forEach((key) => {
          if (safe[key] !== undefined) return;
          if (isDisplayValue(combined[key]) && !isPlainObject(combined[key]) && !Array.isArray(combined[key])) {
            safe[key] = combined[key];
          }
        });
      });
      return safe;
    }

    function formatCompactCount(value, prefix = '') {
      const num = Number(value);
      if (!Number.isFinite(num)) return escapeHtml(value ?? '--');
      const abs = Math.abs(num);
      if (abs >= 1000000000000) return `${prefix}${formatNumber(num / 1000000000000, 2)}T`;
      if (abs >= 1000000000) return `${prefix}${formatNumber(num / 1000000000, 2)}B`;
      if (abs >= 1000000) return `${prefix}${formatNumber(num / 1000000, 2)}M`;
      if (abs >= 1000) return `${prefix}${formatNumber(num / 1000, 1)}K`;
      return `${prefix}${formatNumber(num, 0)}`;
    }

    function formatProfileValue(key, value) {
      const normalizedKey = String(key || '').trim().toLowerCase();
      if (value === undefined || value === null || value === '') return '--';
      if (typeof value === 'boolean') return value ? 'yes' : 'no';
      if (normalizedKey.includes('market_cap')) return formatCompactCount(value, '$');
      if (normalizedKey === 'short_float_pct') {
        const pct = Number(value);
        return Number.isFinite(pct) ? `${formatNumber(pct, 2)}%` : String(value);
      }
      if (normalizedKey.includes('volume') || normalizedKey.includes('shares') || normalizedKey === 'float_shares') {
        return formatCompactCount(value);
      }
      if (normalizedKey === 'beta') {
        const beta = Number(value);
        return Number.isFinite(beta) ? formatNumber(beta, 2) : String(value);
      }
      if (typeof value === 'number') return formatNumber(value, Number.isInteger(value) ? 0 : 2);
      return String(value);
    }

    function renderSymbolProfileSummary(row, { maxItems = 7, emptyText = FUNDAMENTALS_PROFILE_HINT } = {}) {
      const profile = getSafeSymbolProfile(row);
      const hint = `<span class="muted" title="等待 ibkr_fundamentals.extra 或动态准入画像补齐">${escapeHtml(emptyText)}</span>`;
      if (!isDisplayValue(profile)) return emptyText ? hint : '';
      const parsed = parseMaybeObject(profile);
      if (!isDisplayValue(parsed)) return emptyText ? hint : '';
      const title = firstDisplayValue([
        parsed.name,
        parsed.profile,
        parsed.market_cap_tier,
        parsed.liquidity_tier,
      ]);
      const profileKeys = [
        ['source', 'source'],
        ['provider', 'provider'],
        ['sector', 'sector'],
        ['country', 'country'],
        ['beta', 'beta'],
        ['shares_float', 'float'],
        ['float_shares', 'float'],
        ['short_float_pct', 'short float'],
        ['avg_volume_10d_provider', '10d provider vol'],
        ['avg_10d_volume', '10d vol'],
        ['market_cap_usd', 'cap'],
        ['market_cap', 'cap'],
        ['industry', 'industry'],
        ['activity_profile', 'activity'],
        ['float_profile', 'float profile'],
        ['short_interest_profile', 'short profile'],
      ];
      const chips = [];
      if (title) chips.push(`profile: ${title}`);
      profileKeys.forEach(([key, label]) => {
        if (chips.length >= maxItems) return;
        const value = parsed[key];
        if (!isDisplayValue(value)) return;
        chips.push(`${label}: ${formatProfileValue(key, value)}`);
      });
      return chips.length
        ? `<div class="reason-wrap">${chips.slice(0, maxItems).map((item) => `<span class="reason-pill" title="ibkr_fundamentals.extra / dynamic admission normalized field">${escapeHtml(item)}</span>`).join('')}</div>`
        : (emptyText ? hint : '');
    }

    function getNeedsBackfillState(row) {
      const extra = getRowExtra(row);
      const snapshot = getRowScreenerSnapshot(row);
      const quality = parseMaybeObject(row?.data_quality);
      const coverage = parseMaybeObject(row?.coverage);
      const backfill = parseMaybeObject(row?.backfill);
      const value = firstDisplayValue([
        row?.needs_backfill,
        row?.backfill_needed,
        quality.needs_backfill,
        quality.needs_repair,
        quality.backfill_needed,
        coverage.needs_backfill,
        backfill.needs_backfill,
        snapshot.needs_backfill,
        extra.needs_backfill,
      ]);
      const detail = firstDisplayValue([
        row?.backfill_reason,
        quality.reason,
        quality.status,
        coverage.reason,
        backfill.reason,
        snapshot.backfill_reason,
        extra.backfill_reason,
      ]);
      return {
        explicit: value !== undefined,
        needsBackfill: value !== undefined ? normalizeTruth(value) : false,
        detail: detail ? String(detail) : '',
      };
    }

    function renderNeedsBackfillChip(row) {
      const state = getNeedsBackfillState(row);
      if (!state.explicit) return '';
      const label = state.needsBackfill ? 'needs_backfill' : 'backfill ok';
      return statusChip(label, state.needsBackfill ? 'stale' : 'active');
    }

    function getAdmissionScore(row) {
      const extra = getRowExtra(row);
      const snapshot = getRowScreenerSnapshot(row);
      const admission = parseMaybeObject(extra.intraday_window_admission);
      const eligibility = parseMaybeObject(row?.eligibility);
      const value = firstDisplayValue([
        row?.admission_score,
        parseMaybeObject(row?.admission).score,
        parseMaybeObject(row?.admission).admission_score,
        eligibility.admission_score,
        eligibility.score,
        admission.admission_score,
        admission.score,
        snapshot.admission_score,
        extra.admission_score,
      ]);
      const score = Number(value);
      return Number.isFinite(score) ? score : null;
    }

    function renderAdmissionScoreChip(row) {
      const score = getAdmissionScore(row);
      if (score === null) return '';
      const tone = score >= 70 ? 'active' : (score >= 45 ? 'candidate' : 'stale');
      return statusChip(`admission ${formatNumber(score, 1)}`, tone);
    }

    function formatGateLabel(item) {
      if (!isPlainObject(item)) return String(item || '').trim();
      const label = firstDisplayValue([
        item.label,
        item.note,
        item.reason,
        item.message,
        item.code,
        item.gate,
        item.bucket,
        item.metric,
      ]) || 'gate';
      const parts = [String(label)];
      if (isDisplayValue(item.count)) parts.push(`${item.count}d`);
      if (isDisplayValue(item.actual) && isDisplayValue(item.threshold)) {
        parts.push(`${formatProfileValue(item.metric || '', item.actual)}/${formatProfileValue(item.metric || '', item.threshold)}`);
      }
      return parts.join(' ');
    }

    function normalizeGateList(value) {
      if (!isDisplayValue(value)) return [];
      if (Array.isArray(value)) return value.map(formatGateLabel).filter(Boolean);
      if (isPlainObject(value)) {
        const nested = firstDisplayValue([
          value.failed_gates,
          value.gates,
          value.fail_reasons,
          value.reasons,
        ]);
        if (nested !== undefined) return normalizeGateList(nested);
        return [formatGateLabel(value)].filter(Boolean);
      }
      const text = String(value || '').trim();
      if (!text) return [];
      return text.split(/[;,，；]/).map((item) => item.trim()).filter(Boolean);
    }

    function getFailedGates(row) {
      const extra = getRowExtra(row);
      const snapshot = getRowScreenerSnapshot(row);
      const admission = parseMaybeObject(extra.intraday_window_admission);
      const eligibility = parseMaybeObject(row?.eligibility);
      const values = [
        row?.failed_gates,
        row?.failedGates,
        row?.failed_gate,
        row?.gate_failures,
        eligibility.failed_gates,
        eligibility.fail_reasons,
        admission.failed_gates,
        admission.rejected_gates,
        snapshot.failed_gates,
        snapshot.fail_reasons,
        extra.failed_gates,
      ];
      const seen = new Set();
      return values.flatMap(normalizeGateList).filter((item) => {
        const key = String(item || '').trim();
        if (!key || seen.has(key)) return false;
        seen.add(key);
        return true;
      });
    }

    function renderFailedGatesPills(row, emptyText = '') {
      const gates = getFailedGates(row);
      if (!gates.length) return emptyText ? `<span class="muted">${escapeHtml(emptyText)}</span>` : '';
      return `<div class="reason-wrap">${gates.slice(0, 6).map((item) => `<span class="reason-pill">${escapeHtml(item)}</span>`).join('')}</div>`;
    }

    function renderAdmissionControlRow(row) {
      const parts = [
        renderAdmissionScoreChip(row),
        renderNeedsBackfillChip(row),
      ].filter(Boolean);
      return parts.length ? `<div class="pill-row">${parts.join('')}</div>` : '';
    }

    function renderSymbolProfileBlock(row, label = 'Symbol profile') {
      const profileHtml = renderSymbolProfileSummary(row);
      if (!profileHtml) return '';
      return `
        <div class="reason-block" style="margin-top:10px;">
          <div class="reason-label">${escapeHtml(label)}</div>
          ${profileHtml}
        </div>
      `;
    }

    function renderAdmissionDiagnosticsBlock(row, label = 'Admission / Data Gates') {
      const controls = renderAdmissionControlRow(row);
      const failedGates = renderFailedGatesPills(row);
      if (!controls && !failedGates) return '';
      return `
        <div class="reason-block" style="margin-top:10px;">
          <div class="reason-label">${escapeHtml(label)}</div>
          ${controls || ''}
          ${failedGates || '<span class="muted">failed_gates: none</span>'}
        </div>
      `;
    }

    function dataQualityChip(row) {
      const backfillChip = renderNeedsBackfillChip(row);
      if (backfillChip) return backfillChip;
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

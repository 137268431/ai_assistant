// Shared frontend data cache with TTL, stale-while-revalidate, persistence,
// and in-flight request de-duping.
(function initIbkrDataCenter(root) {
  const STORAGE_PREFIX = 'ibkr:data-center:';
  const DEFAULT_TTL_MS = 30000;
  const DEFAULT_SWR_MS = 0;

  const memoryCache = new Map();
  const inFlight = new Map();

  function nowMs() {
    return Date.now();
  }

  function normalizeMs(value, fallback) {
    const number = Number(value);
    return Number.isFinite(number) && number >= 0 ? number : fallback;
  }

  function normalizeTags(tags) {
    return [...new Set((Array.isArray(tags) ? tags : [tags])
      .map((tag) => String(tag || '').trim())
      .filter(Boolean))];
  }

  function stableSerialize(value) {
    if (value === null || value === undefined) return String(value);
    const type = typeof value;
    if (type === 'number' || type === 'boolean' || type === 'string') {
      return JSON.stringify(value);
    }
    if (type === 'bigint') return JSON.stringify(String(value));
    if (value instanceof Date) return JSON.stringify(value.toISOString());
    if (Array.isArray(value)) {
      return `[${value.map((item) => stableSerialize(item)).join(',')}]`;
    }
    if (type === 'object') {
      const keys = Object.keys(value).sort();
      return `{${keys.map((key) => `${JSON.stringify(key)}:${stableSerialize(value[key])}`).join(',')}}`;
    }
    return JSON.stringify(String(value));
  }

  function buildDataCacheKey(namespace, payload) {
    const safeNamespace = String(namespace || 'default').trim() || 'default';
    return `${safeNamespace}:${stableSerialize(payload == null ? {} : payload)}`;
  }

  function storageKey(key) {
    return `${STORAGE_PREFIX}${key}`;
  }

  function getSessionStorage() {
    try {
      if (root && root.sessionStorage) return root.sessionStorage;
    } catch (_) {
      return null;
    }
    return null;
  }

  function readPersistedEntry(key) {
    const storage = getSessionStorage();
    if (!storage) return null;
    try {
      const raw = storage.getItem(storageKey(key));
      if (!raw) return null;
      const parsed = JSON.parse(raw);
      if (!parsed || typeof parsed !== 'object') return null;
      return parsed;
    } catch (_) {
      return null;
    }
  }

  function writePersistedEntry(key, entry) {
    const storage = getSessionStorage();
    if (!storage || !entry.persist) return;
    try {
      storage.setItem(storageKey(key), JSON.stringify(entry));
    } catch (_) {
      // Ignore quota and serialization failures; memory cache remains valid.
    }
  }

  function removePersistedEntry(key) {
    const storage = getSessionStorage();
    if (!storage) return;
    try {
      storage.removeItem(storageKey(key));
    } catch (_) {
      // Best effort only.
    }
  }

  function removeAllPersistedEntries() {
    const storage = getSessionStorage();
    if (!storage) return;
    try {
      const keys = [];
      for (let index = 0; index < storage.length; index += 1) {
        const key = storage.key(index);
        if (key && key.indexOf(STORAGE_PREFIX) === 0) keys.push(key);
      }
      keys.forEach((key) => storage.removeItem(key));
    } catch (_) {
      // Best effort only.
    }
  }

  function listPersistedEntries() {
    const storage = getSessionStorage();
    const entries = [];
    if (!storage) return entries;
    try {
      for (let index = 0; index < storage.length; index += 1) {
        const storageName = storage.key(index);
        if (!storageName || storageName.indexOf(STORAGE_PREFIX) !== 0) continue;
        const key = storageName.slice(STORAGE_PREFIX.length);
        const entry = readPersistedEntry(key);
        if (entry) entries.push([key, entry]);
      }
    } catch (_) {
      // Best effort only.
    }
    return entries;
  }

  function getEntry(key) {
    if (memoryCache.has(key)) return memoryCache.get(key);
    const persisted = readPersistedEntry(key);
    if (persisted) {
      memoryCache.set(key, persisted);
      return persisted;
    }
    return null;
  }

  function resolveEntryOptions(value, options) {
    const source = options && typeof options === 'object' ? options : {};
    if (typeof source.resolveOptions !== 'function') return source;
    try {
      const resolved = source.resolveOptions(value, { updatedAt: nowMs() });
      if (resolved && typeof resolved === 'object') {
        return { ...source, ...resolved };
      }
    } catch (_) {
      // Dynamic TTL failures should not prevent caching with the base options.
    }
    return source;
  }

  function makeEntry(value, options) {
    const effectiveOptions = resolveEntryOptions(value, options);
    const timestamp = nowMs();
    const ttlMs = normalizeMs(effectiveOptions.ttlMs != null ? effectiveOptions.ttlMs : effectiveOptions.ttl, DEFAULT_TTL_MS);
    const swrMs = normalizeMs(effectiveOptions.swrMs != null ? effectiveOptions.swrMs : effectiveOptions.swr, DEFAULT_SWR_MS);
    return {
      value,
      updatedAt: timestamp,
      expiresAt: timestamp + ttlMs,
      staleUntil: timestamp + ttlMs + swrMs,
      ttlMs,
      swrMs,
      persist: effectiveOptions.persist === true,
      tags: normalizeTags(effectiveOptions.tags)
    };
  }

  function saveEntry(key, value, options) {
    const entry = makeEntry(value, options);
    memoryCache.set(key, entry);
    if (entry.persist) writePersistedEntry(key, entry);
    else removePersistedEntry(key);
    return entry;
  }

  function refresh(key, loader, options, notify) {
    if (!options.force && inFlight.has(key)) return inFlight.get(key);
    const promise = Promise.resolve()
      .then(() => loader())
      .then((value) => {
        saveEntry(key, value, options);
        if (notify && typeof options.onRefresh === 'function') {
          try {
            options.onRefresh(value, { key, refreshedAt: nowMs() });
          } catch (_) {
            // Consumer callbacks should not poison cache state.
          }
        }
        return value;
      })
      .finally(() => {
        if (inFlight.get(key) === promise) inFlight.delete(key);
      });
    inFlight.set(key, promise);
    return promise;
  }

  function get(key, loader, options = {}) {
    const cacheKey = typeof key === 'string' ? key : stableSerialize(key);
    if (typeof loader !== 'function') {
      return Promise.reject(new Error('IbkrDataCenter.get requires a loader function'));
    }

    const entry = options.force ? null : getEntry(cacheKey);
    const timestamp = nowMs();
    if (entry && timestamp <= Number(entry.expiresAt || 0)) {
      return Promise.resolve(entry.value);
    }
    if (entry && timestamp <= Number(entry.staleUntil || 0)) {
      refresh(cacheKey, loader, options, true).catch(() => {});
      return Promise.resolve(entry.value);
    }
    return refresh(cacheKey, loader, options, true);
  }

  function matchesInvalidation(entryKey, entry, matchOrTags) {
    if (typeof matchOrTags === 'function') {
      return Boolean(matchOrTags(entryKey, entry));
    }
    const entryTags = normalizeTags(entry && entry.tags);
    if (Array.isArray(matchOrTags)) {
      const tagSet = new Set(normalizeTags(matchOrTags));
      return entryTags.some((tag) => tagSet.has(tag));
    }
    if (matchOrTags && typeof matchOrTags === 'object') {
      if (matchOrTags.key && String(matchOrTags.key) === entryKey) return true;
      if (matchOrTags.prefix && entryKey.indexOf(String(matchOrTags.prefix)) === 0) return true;
      if (matchOrTags.tags) return matchesInvalidation(entryKey, entry, matchOrTags.tags);
      return false;
    }
    const text = String(matchOrTags || '').trim();
    if (!text) return true;
    return entryKey === text || entryKey.indexOf(text) === 0 || entryTags.includes(text);
  }

  function invalidate(matchOrTags) {
    const removed = [];
    memoryCache.forEach((entry, key) => {
      if (!matchesInvalidation(key, entry, matchOrTags)) return;
      memoryCache.delete(key);
      inFlight.delete(key);
      removePersistedEntry(key);
      removed.push(key);
    });
    listPersistedEntries().forEach(([key, entry]) => {
      if (memoryCache.has(key) || !matchesInvalidation(key, entry, matchOrTags)) return;
      inFlight.delete(key);
      removePersistedEntry(key);
      removed.push(key);
    });
    return removed.length;
  }

  function clear() {
    const count = memoryCache.size;
    memoryCache.clear();
    inFlight.clear();
    removeAllPersistedEntries();
    return count;
  }

  function stats() {
    let persisted = 0;
    const tags = {};
    memoryCache.forEach((entry) => {
      if (entry && entry.persist) persisted += 1;
      normalizeTags(entry && entry.tags).forEach((tag) => {
        tags[tag] = (tags[tag] || 0) + 1;
      });
    });
    return {
      entries: memoryCache.size,
      inFlight: inFlight.size,
      persisted,
      tags
    };
  }

  const api = {
    get,
    invalidate,
    clear,
    stats,
    buildDataCacheKey,
    stableSerialize
  };

  root.IbkrDataCenter = root.IbkrDataCenter || api;
  root.buildDataCacheKey = root.buildDataCacheKey || buildDataCacheKey;
  root.stableDataSerialize = root.stableDataSerialize || stableSerialize;

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = api;
  }
})(typeof globalThis !== 'undefined' ? globalThis : window);

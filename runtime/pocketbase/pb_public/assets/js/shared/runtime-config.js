(function initIbkrRuntimeConfig() {
  function deriveDefaultBaseUrl(defaultPort) {
    if (typeof window === 'undefined' || !window.location) {
      return `http://127.0.0.1:${defaultPort}`;
    }
    const protocol = window.location.protocol || 'http:';
    const hostname = window.location.hostname || '127.0.0.1';
    const origin = String(window.location.origin || '').trim();
    const isLoopback = hostname === '127.0.0.1' || hostname === 'localhost';
    if (isLoopback) {
      return `${protocol}//${hostname}:${defaultPort}`;
    }
    if (origin && origin !== 'null') {
      return origin;
    }
    return `http://${hostname}:${defaultPort}`;
  }

  const defaults = {
    API_BASE_URL: deriveDefaultBaseUrl('5102'),
    // Default auth requests to the API compatibility layer; override this
    // when the deployment exposes PocketBase auth on a dedicated origin.
    PB_AUTH_BASE_URL: deriveDefaultBaseUrl('5102'),
    CONSOLE_BASE_URL: deriveDefaultBaseUrl('5104'),
  };
  const existing = (typeof window !== 'undefined' && window.__IBKR_RUNTIME_CONFIG__)
    ? window.__IBKR_RUNTIME_CONFIG__
    : {};
  if (typeof window !== 'undefined') {
    window.__IBKR_RUNTIME_CONFIG__ = {
      ...defaults,
      ...existing,
    };
  }
})();

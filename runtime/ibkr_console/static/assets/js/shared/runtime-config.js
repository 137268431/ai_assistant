(function initIbkrRuntimeConfig() {
  const DEFAULT_CONSOLE_PUBLIC_URL = 'https://quant.lzw-glory.top';
  const DEFAULT_PB_AUTH_PUBLIC_URL = 'https://pb.lzw-glory.top';

  function deriveDefaultBaseUrl(defaultPort, options = {}) {
    const { publicUrl = '' } = options;
    if (typeof window === 'undefined' || !window.location) {
      return publicUrl || `http://127.0.0.1:${defaultPort}`;
    }
    const protocol = window.location.protocol || 'http:';
    const hostname = window.location.hostname || '127.0.0.1';
    const origin = String(window.location.origin || '').trim();
    const isLoopback = hostname === '127.0.0.1' || hostname === 'localhost';
    const isSplitStackPublicHost = hostname === 'quant.lzw-glory.top' || hostname === 'pb.lzw-glory.top';
    if (isLoopback) {
      return `${protocol}//${hostname}:${defaultPort}`;
    }
    if (isSplitStackPublicHost && publicUrl) {
      return publicUrl;
    }
    if (origin && origin !== 'null') {
      return origin;
    }
    return `http://${hostname}:${defaultPort}`;
  }

  const defaults = {
    API_BASE_URL: deriveDefaultBaseUrl('5102', { publicUrl: DEFAULT_CONSOLE_PUBLIC_URL }),
    // PocketBase auth/data stays on the dedicated PB origin even after the split.
    PB_AUTH_BASE_URL: deriveDefaultBaseUrl('8090', { publicUrl: DEFAULT_PB_AUTH_PUBLIC_URL }),
    CONSOLE_BASE_URL: deriveDefaultBaseUrl('5104', { publicUrl: DEFAULT_CONSOLE_PUBLIC_URL }),
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

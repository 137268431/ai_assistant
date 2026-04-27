// Shared compatibility entrypoint.
// Source-of-truth browser modules now live under /assets/js/shared and /assets/css.
(function loadSharedCommonBundle() {
  const scriptTags = [
    '<script src="/assets/js/shared/runtime-config.js"></script>',
    '<script src="/assets/js/shared/base.js"></script>',
    '<script src="/assets/js/shared/ui.js"></script>'
  ];

  if (typeof document !== 'undefined') {
    if (document.readyState === 'loading') {
      document.write(scriptTags.join(''));
    } else {
      [
        '/assets/js/shared/runtime-config.js',
        '/assets/js/shared/base.js',
        '/assets/js/shared/ui.js'
      ].forEach((src) => {
        if (document.querySelector(`script[src="${src}"]`)) return;
        const script = document.createElement('script');
        script.src = src;
        script.async = false;
        document.head.appendChild(script);
      });
    }
  }

  if (typeof module !== 'undefined' && module.exports) {
    const pick = (name) => globalThis[name];
    module.exports = {
      get BASE_URL() { return pick('BASE_URL'); },
      get API_BASE_URL() { return pick('API_BASE_URL'); },
      get PB_AUTH_BASE_URL() { return pick('PB_AUTH_BASE_URL'); },
      get CONSOLE_BASE_URL() { return pick('CONSOLE_BASE_URL'); },
      createPocketBaseClient: (...args) => pick('createPocketBaseClient')(...args),
      createPocketBaseAuthClient: (...args) => pick('createPocketBaseAuthClient')(...args),
      getToken: (...args) => pick('getToken')(...args),
      requireAuth: (...args) => pick('requireAuth')(...args),
      fetchWithRetry: (...args) => pick('fetchWithRetry')(...args),
      apiFetch: (...args) => pick('apiFetch')(...args),
      showToast: (...args) => pick('showToast')(...args),
      renderNav: (...args) => pick('renderNav')(...args),
      renderDatePicker: (...args) => pick('renderDatePicker')(...args),
      renderDirectionTabs: (...args) => pick('renderDirectionTabs')(...args),
      renderRefreshBar: (...args) => pick('renderRefreshBar')(...args),
      getCommonStyles: (...args) => pick('getCommonStyles')(...args),
      formatBeijingTime: (...args) => pick('formatBeijingTime')(...args),
      formatEtRefreshClock: (...args) => pick('formatEtRefreshClock')(...args),
      formatEtRefreshDateTime: (...args) => pick('formatEtRefreshDateTime')(...args),
      setPageRefreshTime: (...args) => pick('setPageRefreshTime')(...args),
      setPageContextMeta: (...args) => pick('setPageContextMeta')(...args),
      formatRelativeTime: (...args) => pick('formatRelativeTime')(...args),
      formatTime: (...args) => pick('formatTime')(...args),
      showLoading: (...args) => pick('showLoading')(...args),
      hideLoading: (...args) => pick('hideLoading')(...args),
      withLoading: (...args) => pick('withLoading')(...args),
    };
  }
})();

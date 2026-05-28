// Shared compatibility entrypoint.
// Source-of-truth browser modules now live under /assets/js/shared and /assets/css.
(function loadSharedCommonBundle() {
  const sharedBundleVersion = '20260528-night-session-review-entry-1';
  const sharedScriptPaths = [
    `/assets/js/shared/runtime-config.js?v=${sharedBundleVersion}`,
    `/assets/js/shared/data-center.js?v=${sharedBundleVersion}`,
    `/assets/js/shared/base.js?v=${sharedBundleVersion}`,
    `/assets/js/shared/ui-toast-nav.js?v=${sharedBundleVersion}`,
    `/assets/js/shared/ui-time-indicator.js?v=${sharedBundleVersion}`,
    `/assets/js/shared/ui-page.js?v=${sharedBundleVersion}`,
    `/assets/js/shared/ui-bridges.js?v=${sharedBundleVersion}`,
    `/assets/js/shared/ui-legacy.js?v=${sharedBundleVersion}`,
    `/assets/js/shared/ui.js?v=${sharedBundleVersion}`
  ];
  const scriptTags = sharedScriptPaths.map((src) => `<script src="${src}"></script>`);

  if (typeof document !== 'undefined') {
    if (document.readyState === 'loading') {
      document.write(scriptTags.join(''));
    } else {
      sharedScriptPaths.forEach((src) => {
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
      get IbkrDataCenter() { return pick('IbkrDataCenter'); },
      buildDataCacheKey: (...args) => pick('buildDataCacheKey')(...args),
      getIbkrCacheProfile: (...args) => pick('getIbkrCacheProfile')(...args),
      getMarketCalendarCacheOptions: (...args) => pick('getMarketCalendarCacheOptions')(...args),
      invalidateIbkrDataCache: (...args) => pick('invalidateIbkrDataCache')(...args),
      cachedApiFetch: (...args) => pick('cachedApiFetch')(...args),
      cachedCustomJson: (...args) => pick('cachedCustomJson')(...args),
      cachedPageJson: (...args) => pick('cachedPageJson')(...args),
      cachedMarketCalendar: (...args) => pick('cachedMarketCalendar')(...args),
      fetchCollectionFullListCached: (...args) => pick('fetchCollectionFullListCached')(...args),
      cachedCountFetch: (...args) => pick('cachedCountFetch')(...args),
      fetchRealtimeQuotes: (...args) => pick('fetchRealtimeQuotes')(...args),
      fetchRealtimeQuotesIfNeeded: (...args) => pick('fetchRealtimeQuotesIfNeeded')(...args),
      getRealtimeQuote: (...args) => pick('getRealtimeQuote')(...args),
      isFreshRealtimeQuote: (...args) => pick('isFreshRealtimeQuote')(...args),
      mergeIndicatorWithRealtimeQuote: (...args) => pick('mergeIndicatorWithRealtimeQuote')(...args),
      showToast: (...args) => pick('showToast')(...args),
      renderNav: (...args) => pick('renderNav')(...args),
      renderDatePicker: (...args) => pick('renderDatePicker')(...args),
      renderDirectionTabs: (...args) => pick('renderDirectionTabs')(...args),
      renderRefreshBar: (...args) => pick('renderRefreshBar')(...args),
      getCommonStyles: (...args) => pick('getCommonStyles')(...args),
      formatMarketTime: (...args) => pick('formatMarketTime')(...args),
      formatBeijingTime: (...args) => pick('formatBeijingTime')(...args),
      formatEtRefreshClock: (...args) => pick('formatEtRefreshClock')(...args),
      formatEtRefreshDateTime: (...args) => pick('formatEtRefreshDateTime')(...args),
      getDateStringInTimeZone: (...args) => pick('getDateStringInTimeZone')(...args),
      setPageRefreshTime: (...args) => pick('setPageRefreshTime')(...args),
      setPageContextMeta: (...args) => pick('setPageContextMeta')(...args),
      createClientPaginationModel: (...args) => pick('createClientPaginationModel')(...args),
      renderClientPaginationBar: (...args) => pick('renderClientPaginationBar')(...args),
      renderPageRefreshControl: (...args) => pick('renderPageRefreshControl')(...args),
      renderPageTopSection: (...args) => pick('renderPageTopSection')(...args),
      renderPageLoadingOverlay: (...args) => pick('renderPageLoadingOverlay')(...args),
      ensurePageLoadingOverlay: (...args) => pick('ensurePageLoadingOverlay')(...args),
      setPageLoading: (...args) => pick('setPageLoading')(...args),
      withPageLoading: (...args) => pick('withPageLoading')(...args),
      spinPageRefreshButton: (...args) => pick('spinPageRefreshButton')(...args),
      renderSystemBridge: (...args) => pick('renderSystemBridge')(...args),
      renderExecutionBridge: (...args) => pick('renderExecutionBridge')(...args),
      renderAnalyticsBridge: (...args) => pick('renderAnalyticsBridge')(...args),
      renderHomeBridge: (...args) => pick('renderHomeBridge')(...args),
      renderOpsBridge: (...args) => pick('renderOpsBridge')(...args),
      renderBacktestsBridge: (...args) => pick('renderBacktestsBridge')(...args),
      getCurrentEtDateString: (...args) => pick('getCurrentEtDateString')(...args),
      getUsDate: (...args) => pick('getUsDate')(...args),
      getUsDateFromTime: (...args) => pick('getUsDateFromTime')(...args),
      getEtDateStringFromMs: (...args) => pick('getEtDateStringFromMs')(...args),
      shiftDateString: (...args) => pick('shiftDateString')(...args),
      getEtDayStartMs: (...args) => pick('getEtDayStartMs')(...args),
      getEtDayBoundsMs: (...args) => pick('getEtDayBoundsMs')(...args),
      getEtDateRangeBoundsMs: (...args) => pick('getEtDateRangeBoundsMs')(...args),
      formatUtcDateTimeForPocketBase: (...args) => pick('formatUtcDateTimeForPocketBase')(...args),
      buildModePayload: (...args) => pick('buildModePayload')(...args),
      refreshBrokerModeContext: (...args) => pick('refreshBrokerModeContext')(...args),
      formatRelativeTime: (...args) => pick('formatRelativeTime')(...args),
      formatTime: (...args) => pick('formatTime')(...args),
      showLoading: (...args) => pick('showLoading')(...args),
      hideLoading: (...args) => pick('hideLoading')(...args),
      withLoading: (...args) => pick('withLoading')(...args),
      guardedLoader: (...args) => pick('guardedLoader')(...args),
      showConfirmDialog: (...args) => pick('showConfirmDialog')(...args),
      getIndicatorModalStyles: (...args) => pick('getIndicatorModalStyles')(...args),
      showIndicatorModal: (...args) => pick('showIndicatorModal')(...args),
    };
  }
})();

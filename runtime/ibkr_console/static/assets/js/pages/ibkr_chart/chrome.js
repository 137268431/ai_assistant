function renderChartWorkspaceChrome(payload = getChartDisplayPayload(), { includeTrace = true } = {}) {
            renderChartToolbar(payload);
            renderChartFloatingLegend(payload);
            renderChartBottomBar(payload);
            renderMobileDock(payload);
            renderMobileQuickPanel(payload);
            renderInspectorDrawer(payload);
            renderMobileGestureHint(payload);
            if (includeTrace) renderTracePanel(payload);
        }

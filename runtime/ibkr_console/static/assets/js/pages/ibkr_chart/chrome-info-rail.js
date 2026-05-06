        function getRailNavLabel(kicker) {
            const text = String(kicker || '').trim();
            const map = {
                'IBKR Compare': 'Compare',
                'Focus': 'Focus',
                'Stored Bars Chain': 'Stored',
                'IBKR API Chain': 'IBKR',
                'Comparison': 'Diff',
                'Snapshot': 'Snapshot',
                'Structure': 'Structure',
                'Signal Flow': 'Signals',
                'Recent Signals': 'Recent',
                'Load Error': 'Error',
            };
            return map[text] || text || 'Card';
        }
        function renderRailNav() {
            const nav = document.getElementById('railNav');
            const rail = document.getElementById('infoRail');
            if (!nav || !rail) return;
            const cards = Array.from(rail.querySelectorAll('.rail-card'));
            if (!cards.length) {
                nav.innerHTML = '';
                activeRailCardIndex = 0;
                return;
            }
            nav.innerHTML = cards.map((card, index) => {
                const kicker = card.querySelector('.rail-kicker')?.textContent || '';
                card.dataset.railIndex = String(index);
                return `<button class="rail-nav-chip" type="button" onclick="scrollInfoRailCard(${index})">${escapeHtml(getRailNavLabel(kicker))}</button>`;
            }).join('');
            setActiveRailCard(activeRailCardIndex, { syncScroll: false });
        }
        function setActiveRailCard(index, { syncScroll = true } = {}) {
            const rail = document.getElementById('infoRail');
            const nav = document.getElementById('railNav');
            const cards = rail ? Array.from(rail.querySelectorAll('.rail-card')) : [];
            const chips = nav ? Array.from(nav.querySelectorAll('.rail-nav-chip')) : [];
            if (!cards.length) return;
            const safeIndex = Math.max(0, Math.min(Number(index) || 0, cards.length - 1));
            activeRailCardIndex = safeIndex;
            const compact = isCompactViewport();
            if (rail) rail.classList.toggle('compact-tabs', compact);
            cards.forEach((card, cardIndex) => {
                card.classList.toggle('active', cardIndex === safeIndex);
            });
            chips.forEach((chip, chipIndex) => {
                chip.classList.toggle('active', chipIndex === safeIndex);
            });
            if (!syncScroll) return;
            const target = cards[safeIndex];
            if (!target) return;
            if (compact) {
                target.scrollIntoView({ behavior: 'smooth', block: 'nearest', inline: 'nearest' });
                return;
            }
            target.scrollIntoView({ behavior: 'smooth', block: 'nearest', inline: 'start' });
        }
        function renderInspectorDrawer(payload = getChartDisplayPayload()) {
            const drawer = document.getElementById('inspectorDrawer');
            const title = document.getElementById('inspectorDrawerTitle');
            const sub = document.getElementById('inspectorDrawerSub');
            const body = document.getElementById('inspectorDrawerBody');
            const rail = document.getElementById('infoRail');
            if (!drawer || !title || !sub || !body) return;
            const focus = payload ? buildContext(payload, getEffectiveCursorIndex(payload), selectedSignalId) : null;
            title.textContent = `${currentSymbol || '--'} · ${getIntervalLabel(currentInterval)} Inspector`;
            const stateText = comparePayload
                ? 'Compare 已联动'
                : chartFocusMode
                ? '焦点模式'
                : '工作台模式';
            sub.innerHTML = `${escapeHtml(focus?.bar?.us_time || '等待图表数据')}<br>${escapeHtml(stateText)}`;
            body.innerHTML = rail?.innerHTML || '<div class="rail-card"><div class="rail-kicker">Inspector</div><div class="rail-sub">等待图表数据...</div></div>';
            drawer.classList.toggle('show', inspectorDrawerOpen);
            drawer.setAttribute('aria-hidden', inspectorDrawerOpen ? 'false' : 'true');
        }
        function setInspectorDrawerOpen(enabled) {
            inspectorDrawerOpen = Boolean(enabled);
            if (inspectorDrawerOpen) {
                mobileQuickPanelOpen = false;
                closeSignalDrawer();
            }
            renderMobileQuickPanel(getChartDisplayPayload());
            renderInspectorDrawer(getChartDisplayPayload());
            renderMobileGestureHint(getChartDisplayPayload());
        }
        function toggleInspectorDrawer() {
            dismissMobileGestureHint();
            setInspectorDrawerOpen(!inspectorDrawerOpen);
        }
        function closeInspectorDrawer() {
            setInspectorDrawerOpen(false);
        }

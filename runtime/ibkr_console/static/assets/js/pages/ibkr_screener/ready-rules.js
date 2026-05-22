    function renderSummaryCards(cards) {
      document.getElementById('summaryGrid').innerHTML = cards.map((item) => `
        <div class="summary-card">
          <div class="summary-label-row">
            <div class="summary-label">${escapeHtml(item.label)}</div>
            ${item.tip ? renderReadyTipButton(item.tip) : ''}
          </div>
          <div class="summary-value ${item.className || ''}">${escapeHtml(String(item.value))}</div>
          <div class="summary-copy">${escapeHtml(item.copy)}</div>
        </div>
      `).join('');
    }

    function normalizeTipList(value) {
      return Array.isArray(value) ? value.map((item) => String(item || '').trim()).filter(Boolean) : [];
    }

    function buildReadyDefinitionTip(definition) {
      const thresholds = definition?.thresholds || {};
      const requiredFlags = Number(definition?.required_aligned_flags || 2) || 2;
      const avgVolume = Number(thresholds.avg_10d_volume_gte || 500000) || 500000;
      const score = Number(thresholds.tradability_score_gte || 60) || 60;
      const freshness = Number(thresholds.freshness_lte_min || 90) || 90;
      return {
        title: definition?.title || 'READY 判定',
        summary: definition?.summary || 'READY = 可操作条件通过 + 方向一致技术条件达到阈值；等待信号触发，不代表已执行。',
        sections: [
          {
            label: '硬条件',
            items: [
              '当日 5m bar 已更新',
              `freshness <= ${freshness}m`,
              'price > 0',
              `10D 均量 >= ${formatVolume(avgVolume)}`,
              `tradability_score >= ${score}`,
              `方向一致技术条件 >= ${requiredFlags}`,
            ],
          },
          {
            label: 'Long 标签',
            items: normalizeTipList(definition?.long_flags),
          },
          {
            label: 'Short 标签',
            items: normalizeTipList(definition?.short_flags),
          },
        ],
      };
    }

    function buildReadyRowTip(row) {
      const explanation = row?.ready_explanation || {};
      const passed = normalizeTipList(explanation.passed);
      const missing = normalizeTipList(explanation.missing);
      const alignedFlags = normalizeTipList(explanation.aligned_flags);
      return {
        title: `${row?.symbol || '标的'} READY`,
        summary: explanation.summary || (row?.technical_state === 'ready'
          ? '已满足 READY 判定；等待信号触发，不代表已下单或成交。'
          : '尚未达到 READY；先处理缺失条件，再等待 5m close 刷新。'),
        tone: row?.technical_state === 'ready' ? 'good' : 'warn',
        sections: [
          { label: '已满足', items: passed },
          { label: '还缺', items: missing.length ? missing : ['当前无明显缺口'] },
          { label: '方向标签', items: alignedFlags.length ? alignedFlags : ['暂无方向一致技术标签'] },
        ],
      };
    }

    function renderReadyTipContent(tip, className = '', id = '') {
      const sections = Array.isArray(tip?.sections) ? tip.sections : [];
      return `
        <span class="ready-tip-popover ${escapeHtml(className)}" ${id ? `id="${escapeHtml(id)}"` : ''} role="tooltip">
          <span class="ready-tip-title">${escapeHtml(tip?.title || 'READY 判定')}</span>
          <span class="ready-tip-summary">${escapeHtml(tip?.summary || '')}</span>
          ${sections.map((section) => `
            <span class="ready-tip-section">
              <span class="ready-tip-section-label">${escapeHtml(section.label || '--')}</span>
              <span class="ready-tip-list">
                ${normalizeTipList(section.items).map((item) => `<span class="ready-tip-line">${escapeHtml(item)}</span>`).join('')}
              </span>
            </span>
          `).join('')}
        </span>
      `;
    }

    function renderReadyTipButton(tip) {
      const id = `ready-tip-${Math.random().toString(36).slice(2, 10)}`;
      return `
        <span class="ready-tip" data-ready-tip-id="${escapeHtml(id)}" data-ready-tip="${escapeHtml(JSON.stringify(tip || {}))}">
          <button
            class="ready-tip-button ${escapeHtml(tip?.tone || '')}"
            type="button"
            aria-label="${escapeHtml(tip?.title || 'READY tips')}"
            aria-expanded="false"
            data-ready-tip-trigger
          >i</button>
        </span>
      `;
    }

    function renderTechnicalStateWithTip(row) {
      return `
        <span class="state-chip-with-tip">
          ${statusChip(formatCurrentStateLabel(row?.technical_state), row?.technical_state || 'watch')}
          ${renderReadyTipButton(buildReadyRowTip(row))}
        </span>
      `;
    }

    function parseReadyTip(root) {
      try {
        return JSON.parse(root?.dataset.readyTip || '{}');
      } catch (_) {
        return {};
      }
    }

    function closeReadyTips() {
      window.clearTimeout(readyTipCloseTimer);
      readyTipCloseTimer = 0;
      activeReadyTipId = '';
      activeReadyTipTrigger = null;
      activeReadyTipHoverRoot = null;
      document.querySelectorAll('.ready-tip.is-open').forEach((node) => {
        node.classList.remove('is-open');
        const trigger = node.querySelector('[data-ready-tip-trigger]');
        trigger?.setAttribute('aria-expanded', 'false');
        trigger?.removeAttribute('aria-describedby');
      });
      document.getElementById('readyTipPortal')?.remove();
    }

    function scheduleReadyTipClose() {
      window.clearTimeout(readyTipCloseTimer);
      readyTipCloseTimer = window.setTimeout(() => {
        const rootHovered = Boolean(activeReadyTipHoverRoot?.matches(':hover'));
        const portalHovered = Boolean(document.getElementById('readyTipPortal')?.matches(':hover'));
        if (!rootHovered && !portalHovered) closeReadyTips();
      }, 140);
    }

    function positionReadyTipPortal(trigger, portal) {
      const rect = trigger.getBoundingClientRect();
      const gap = 8;
      const width = Math.min(340, Math.max(280, window.innerWidth - 24));
      let left = Math.min(window.innerWidth - width - 12, Math.max(12, rect.left));
      let top = rect.bottom + gap;
      portal.style.width = `${width}px`;
      portal.style.left = `${left}px`;
      portal.style.top = `${top}px`;
      portal.style.visibility = 'hidden';
      requestAnimationFrame(() => {
        const portalRect = portal.getBoundingClientRect();
        if (portalRect.bottom > window.innerHeight - 12) {
          top = Math.max(12, rect.top - portalRect.height - gap);
          portal.style.top = `${top}px`;
        }
        if (portalRect.right > window.innerWidth - 12) {
          left = Math.max(12, window.innerWidth - portalRect.width - 12);
          portal.style.left = `${left}px`;
        }
        portal.style.visibility = 'visible';
      });
    }

    function openReadyTipPortal(root, trigger) {
      const tip = parseReadyTip(root);
      document.getElementById('readyTipPortal')?.remove();
      const portal = document.createElement('div');
      portal.id = 'readyTipPortal';
      portal.className = 'ready-tip-portal';
      portal.innerHTML = renderReadyTipContent(tip, 'ready-tip-popover-portal', `${root.dataset.readyTipId || 'ready-tip'}-portal`);
      document.body.appendChild(portal);
      portal.addEventListener('mouseenter', () => {
        window.clearTimeout(readyTipCloseTimer);
      });
      portal.addEventListener('mouseleave', () => {
        if (activeReadyTipHoverRoot) scheduleReadyTipClose();
      });
      trigger.setAttribute('aria-describedby', `${root.dataset.readyTipId || 'ready-tip'}-portal`);
      positionReadyTipPortal(trigger, portal);
    }

    function openReadyTip(root, trigger, { hover = false } = {}) {
      if (!root || !trigger) return;
      const id = root.dataset.readyTipId || '';
      if (!hover) closeReadyTips();
      root.classList.add('is-open');
      trigger.setAttribute('aria-expanded', 'true');
      activeReadyTipId = id;
      activeReadyTipTrigger = trigger;
      if (hover) activeReadyTipHoverRoot = root;
      openReadyTipPortal(root, trigger);
    }

    function bindReadyTipEvents() {
      document.addEventListener('click', (event) => {
        const trigger = event.target.closest('[data-ready-tip-trigger]');
        if (!trigger) {
          if (!event.target.closest('.ready-tip')) closeReadyTips();
          return;
        }
        const root = trigger.closest('.ready-tip');
        const id = root?.dataset.readyTipId || '';
        const wasHoverOpen = root && root === activeReadyTipHoverRoot;
        const shouldOpen = wasHoverOpen || activeReadyTipId !== id || !root.classList.contains('is-open');
        closeReadyTips();
        if (!shouldOpen || !root) return;
        openReadyTip(root, trigger);
        event.preventDefault();
        event.stopPropagation();
      });
      document.addEventListener('mouseover', (event) => {
        const root = event.target.closest('.ready-tip');
        const trigger = root?.querySelector('[data-ready-tip-trigger]');
        if (!root || !trigger || activeReadyTipId) return;
        window.clearTimeout(readyTipCloseTimer);
        openReadyTip(root, trigger, { hover: true });
      });
      document.addEventListener('mouseout', (event) => {
        const root = event.target.closest('.ready-tip');
        if (!root || root !== activeReadyTipHoverRoot || root.contains(event.relatedTarget)) return;
        scheduleReadyTipClose();
      });
      document.addEventListener('focusin', (event) => {
        const trigger = event.target.closest('[data-ready-tip-trigger]');
        const root = trigger?.closest('.ready-tip');
        if (!root || !trigger || activeReadyTipId) return;
        window.clearTimeout(readyTipCloseTimer);
        openReadyTip(root, trigger, { hover: true });
      });
      document.addEventListener('focusout', (event) => {
        const root = event.target.closest('.ready-tip');
        if (!root || root !== activeReadyTipHoverRoot || root.contains(event.relatedTarget)) return;
        closeReadyTips();
      });
      document.addEventListener('keydown', (event) => {
        if (event.key === 'Escape') closeReadyTips();
      });
      window.addEventListener('resize', () => {
        const portal = document.getElementById('readyTipPortal');
        if (portal && activeReadyTipTrigger) positionReadyTipPortal(activeReadyTipTrigger, portal);
      });
      window.addEventListener('scroll', () => {
        const portal = document.getElementById('readyTipPortal');
        if (portal && activeReadyTipTrigger) positionReadyTipPortal(activeReadyTipTrigger, portal);
      }, true);
    }

    function setHeroMetaLine(id, text = '') {
      const el = document.getElementById(id);
      if (!el) return;
      const value = String(text || '').trim();
      el.textContent = value;
      el.style.display = value ? '' : 'none';
    }

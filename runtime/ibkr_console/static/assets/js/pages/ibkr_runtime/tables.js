        function renderSignalsTable(items) {
            if (!items.length) {
                document.getElementById('signalsTable').innerHTML = renderEmpty('暂无最近信号');
                return;
            }
            document.getElementById('signalsTable').innerHTML = `
                <table class="data-table">
                    <thead>
                        <tr><th>TIME</th><th>SYMBOL</th><th>DIRECTION</th><th>STATUS</th><th>ENTRY</th><th>RR</th></tr>
                    </thead>
                    <tbody>
                        ${items.map((item) => {
                            const direction = String(item.direction || '').toLowerCase();
                            const status = String(item.status || '').toLowerCase();
                            return `
                                <tr>
                                    <td class="mono">${escapeHtml((item.us_time || item.created || '--').slice(0, 19))}</td>
                                    <td>${escapeHtml(item.symbol || '--')}</td>
                                    <td><span class="pill ${direction === 'short' ? 'pill-short' : 'pill-long'}">${escapeHtml((item.direction || '--').toUpperCase())}</span></td>
                                    <td><span class="pill ${statusClass(status)}">${escapeHtml(formatRuntimeSignalStatus(item.status))}</span></td>
                                    <td>${formatMoney(item.entry)}</td>
                                    <td>${escapeHtml(String(item.rr || '--'))}</td>
                                </tr>
                            `;
                        }).join('')}
                    </tbody>
                </table>
            `;
        }

        function renderOrdersTable(items) {
            if (!items.length) {
                document.getElementById('ordersTable').innerHTML = renderEmpty('暂无最近订单');
                return;
            }
            document.getElementById('ordersTable').innerHTML = `
                <table class="data-table">
                    <thead>
                        <tr><th>TIME</th><th>SYMBOL</th><th>ROLE</th><th>STATUS</th><th>QTY</th><th>PRICE</th></tr>
                    </thead>
                    <tbody>
                        ${items.map((item) => {
                            const status = String(item.status || '').toLowerCase();
                            const direction = String(item.direction || '').toLowerCase();
                            return `
                                <tr>
                                    <td class="mono">${escapeHtml((item.us_time || item.created || '--').slice(0, 19))}</td>
                                    <td>${escapeHtml(item.symbol || '--')}</td>
                                    <td><span class="pill ${direction === 'short' ? 'pill-short' : 'pill-long'}">${escapeHtml(item.role || item.order_type || '--')}</span></td>
                                    <td><span class="pill ${statusClass(status)}">${escapeHtml(item.status || '--')}</span></td>
                                    <td>${escapeHtml(String(item.quantity || 0))}</td>
                                    <td>${item.fill_price ? formatMoney(item.fill_price) : formatMoney(item.limit_price)}</td>
                                </tr>
                            `;
                        }).join('')}
                    </tbody>
                </table>
            `;
        }

        function renderEventsTable(items) {
            if (!items.length) {
                document.getElementById('eventsTable').innerHTML = renderEmpty('暂无系统事件');
                return;
            }
            document.getElementById('eventsTable').innerHTML = `
                <table class="data-table">
                    <thead>
                        <tr><th>TIME</th><th>LEVEL</th><th>SOURCE</th><th>TITLE</th></tr>
                    </thead>
                    <tbody>
                        ${items.map((item) => `
                            <tr>
                                <td class="mono">${escapeHtml((item.us_time || item.created || '--').slice(0, 19))}</td>
                                <td><span class="pill ${statusClass(String(item.level || '').toLowerCase())}">${escapeHtml(item.level || '--')}</span></td>
                                <td>${escapeHtml(item.source || '--')}</td>
                                <td style="white-space:normal">${escapeHtml(item.title || '--')}</td>
                            </tr>
                        `).join('')}
                    </tbody>
                </table>
            `;
        }

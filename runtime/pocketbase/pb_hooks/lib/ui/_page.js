/// <reference path="../pb_data/types.d.ts" />

/**
 * _page.js
 * 共享的 HTML 页面生成函数
 */

const card = function(emoji, title, detail, color) {
    color = color || "#333"
    return `<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{font-family:-apple-system,sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;background:#f5f5f5}
.card{background:#fff;border-radius:12px;padding:40px;text-align:center;box-shadow:0 2px 12px rgba(0,0,0,.1);max-width:360px;width:90%}
.emoji{font-size:48px;margin-bottom:16px}.title{font-size:22px;font-weight:600;color:${color};margin-bottom:8px}
.detail{color:#888;font-size:14px;margin-top:8px}</style></head>
<body><div class="card"><div class="emoji">${emoji}</div><div class="title">${title}</div><div class="detail">${detail}</div></div></body></html>`
};

module.exports = {
    // 确认类页面 - 绿色主题
    ok: function(title, detail, symbol) {
        return card("✅", title, detail + (symbol ? "<br><small>" + symbol + "</small>" : ""), "#38a169");
    },

    // 拒绝类页面 - 红色主题
    fail: function(title, detail, symbol) {
        return card("❌", title, detail + (symbol ? "<br><small>" + symbol + "</small>" : ""), "#e53e3e");
    },

    // 警告类页面 - 橙色主题
    warn: function(title, detail, symbol) {
        return card("⚠️", title, detail + (symbol ? "<br><small>" + symbol + "</small>" : ""), "#dd6b20");
    },

    // 信息类页面 - 蓝色主题
    info: function(title, detail, symbol) {
        return card("ℹ️", title, detail + (symbol ? "<br><small>" + symbol + "</small>" : ""), "#3182ce");
    },
}

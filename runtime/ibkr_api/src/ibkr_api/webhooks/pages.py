from __future__ import annotations

from html import escape
from typing import Any

from ibkr_api.orders.values import to_text


_PAGE_THEMES = {
    "ok": {"emoji": "✅", "color": "#166534", "border": "#86efac", "tint": "#f0fdf4"},
    "warn": {"emoji": "⚠️", "color": "#9a3412", "border": "#fdba74", "tint": "#fff7ed"},
    "fail": {"emoji": "❌", "color": "#991b1b", "border": "#fca5a5", "tint": "#fef2f2"},
    "info": {"emoji": "ℹ️", "color": "#1d4ed8", "border": "#93c5fd", "tint": "#eff6ff"},
}


def _theme(page_kind: str) -> dict[str, str]:
    return dict(_PAGE_THEMES.get(to_text(page_kind).lower() or "info", _PAGE_THEMES["info"]))


def build_webhook_page_html(page_kind: str, title: Any, detail: Any, symbol: Any = "") -> str:
    theme = _theme(page_kind)
    safe_title = escape(to_text(title) or "IBKR")
    safe_detail = escape(to_text(detail) or "")
    safe_symbol = escape(to_text(symbol) or "")
    symbol_html = f'<div class="symbol">{safe_symbol}</div>' if safe_symbol else ""
    detail_html = f'<div class="detail">{safe_detail}</div>' if safe_detail else ""
    return f"""<!DOCTYPE html>
<html lang=\"zh-CN\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>{safe_title}</title>
  <style>
    :root {{
      color-scheme: light;
      --page-bg: linear-gradient(180deg, #f8fafc 0%, #eef2ff 100%);
      --card-bg: rgba(255, 255, 255, 0.96);
      --text: #0f172a;
      --muted: #475569;
      --accent: {theme['color']};
      --accent-border: {theme['border']};
      --accent-tint: {theme['tint']};
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      padding: 24px;
      background: var(--page-bg);
      color: var(--text);
      font-family: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    .card {{
      width: min(420px, 100%);
      background: var(--card-bg);
      border: 1px solid rgba(148, 163, 184, 0.18);
      border-radius: 20px;
      padding: 28px 24px;
      box-shadow: 0 24px 60px rgba(15, 23, 42, 0.12);
      text-align: center;
    }}
    .badge {{
      width: 72px;
      height: 72px;
      margin: 0 auto 18px;
      border-radius: 999px;
      display: grid;
      place-items: center;
      font-size: 34px;
      background: var(--accent-tint);
      border: 1px solid var(--accent-border);
    }}
    .title {{
      font-size: 26px;
      font-weight: 700;
      line-height: 1.2;
      color: var(--accent);
      margin: 0;
    }}
    .detail {{
      margin-top: 10px;
      font-size: 15px;
      line-height: 1.6;
      color: var(--muted);
      white-space: pre-wrap;
    }}
    .symbol {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      margin-top: 16px;
      padding: 7px 12px;
      border-radius: 999px;
      border: 1px solid rgba(148, 163, 184, 0.3);
      background: rgba(248, 250, 252, 0.9);
      color: #334155;
      font-size: 13px;
      letter-spacing: 0.04em;
    }}
  </style>
</head>
<body>
  <main class=\"card\">
    <div class=\"badge\">{theme['emoji']}</div>
    <h1 class=\"title\">{safe_title}</h1>
    {detail_html}
    {symbol_html}
  </main>
</body>
</html>
"""


def ok_page(title: Any, detail: Any, symbol: Any = "") -> str:
    return build_webhook_page_html("ok", title, detail, symbol)


def warn_page(title: Any, detail: Any, symbol: Any = "") -> str:
    return build_webhook_page_html("warn", title, detail, symbol)


def fail_page(title: Any, detail: Any, symbol: Any = "") -> str:
    return build_webhook_page_html("fail", title, detail, symbol)


def info_page(title: Any, detail: Any, symbol: Any = "") -> str:
    return build_webhook_page_html("info", title, detail, symbol)

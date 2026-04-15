from __future__ import annotations

from flask import jsonify

from ibkr_compute.api.compute.runtime_state.universe import get_active_trade_symbols
from ibkr_compute.api.market.screener.scoring import build_tradability_rule_summary
from ibkr_compute.api.market.screener.watchlist import load_effective_watchlist
from ibkr_compute.api.shared.route_runtime import get_app_module, get_requested_environment
from ibkr_compute.core.indicator_engine import DEFAULT_PARAMS
from ibkr_compute.signal.signal_processor import (
    DEFAULT_ORDER_WINDOW_END,
    DEFAULT_SIGNAL_EXPIRY_MINUTES,
    DEFAULT_TRADE_WINDOW_END,
    DEFAULT_TRADE_WINDOW_START,
)
from ibkr_compute.workflows.daily_scanner import build_daily_scan_rule_summary


def _config_source(cfg, key: str, environment: str) -> str:
    runtime_environment = str(environment or "").strip().lower()
    best_rank = -1
    source = "default"
    for record in cfg._records_by_key.get(key, []):
        value = record.get("value", "")
        if value in (None, ""):
            continue
        record_environment = str(record.get("environment", "") or "").strip().lower()
        if record_environment == runtime_environment:
            rank = 2
            next_source = "env config"
        elif record_environment in ("", "global"):
            rank = 1
            next_source = "global config"
        else:
            rank = -1
            next_source = "default"
        if rank > best_rank:
            best_rank = rank
            source = next_source
    return source


def _resolve_config_text(cfg, key: str, environment: str, default: str) -> tuple[str, str]:
    return (
        str(cfg.get_for_environment(key, environment, default) or default),
        _config_source(cfg, key, environment),
    )


def _format_number(value: float | int) -> str:
    number = float(value)
    if number.is_integer():
        return str(int(number))
    return f"{number:.2f}".rstrip("0").rstrip(".")


def _format_count(value: int) -> str:
    return f"{int(value):,}"


def _selection_panel(environment: str) -> dict:
    app_mod = get_app_module()
    app_mod.cfg.refresh()

    watchlist_map = load_effective_watchlist(environment)
    trade_watchlist = [
        symbol
        for symbol, row in watchlist_map.items()
        if app_mod.normalize_watchlist_symbol_role((row or {}).get("symbol_role"))
        == app_mod.WATCHLIST_SYMBOL_ROLE_TRADE
    ]
    active_trade_symbols = sorted(get_active_trade_symbols(environment))
    daily_scan = build_daily_scan_rule_summary()
    tradability = build_tradability_rule_summary()
    scan_schedule, scan_schedule_source = _resolve_config_text(
        app_mod.cfg,
        "ibkr_scan_schedule",
        environment,
        "7:00-10:00",
    )

    return {
        "title": "当前选标规则",
        "subtitle": "盘前先按 multi-TF 投票生成 candidate / active，再按 tradability score 排序看盘中优先级。",
        "chips": [
            {
                "label": "扫描时段",
                "value": f"{scan_schedule} ET",
                "copy": f"ibkr_scan_schedule · {scan_schedule_source}",
            },
            {
                "label": "Trade Watchlist",
                "value": _format_count(len(trade_watchlist)),
                "copy": "当前参与日筛的 trade 标池数量",
            },
            {
                "label": "Active Targets",
                "value": _format_count(len(active_trade_symbols)),
                "copy": "当前 active target 数量，盘中优先盯这个集合",
            },
            {
                "label": "Operable Gate",
                "value": f">= {tradability['operable_requirements']['score_gte']}",
                "copy": "tradability score 达标后才算可操作",
            },
        ],
        "sections": [
            {
                "title": "盘前日筛投票",
                "copy": "每个 ready timeframe 只要命中任一条件，就给对应方向加票并加分。",
                "lines": [
                    f"做多主条件: {' / '.join(daily_scan['long_primary'])} 任一命中 => long +1票, +{daily_scan['primary_weight']}分",
                    f"做空主条件: {' / '.join(daily_scan['short_primary'])} 任一命中 => short +1票, +{daily_scan['primary_weight']}分",
                    f"做多辅助条件: {' / '.join(daily_scan['long_secondary'])} 任一命中 => long +1票, +{daily_scan['secondary_weight']}分",
                    f"做空辅助条件: {' / '.join(daily_scan['short_secondary'])} 任一命中 => short +1票, +{daily_scan['secondary_weight']}分",
                    daily_scan["tie_behavior"],
                    daily_scan["final_bonus"],
                ],
            },
            {
                "title": "全量筛选榜打分",
                "copy": "轻量 tradability 评分直接来自 compute 当前阈值。",
                "lines": [
                    "价位: " + "; ".join(
                        f"{rule['summary']} +{rule['score']}" for rule in tradability["price"]
                    ),
                    "10D 均量: " + "; ".join(
                        f"{rule['summary']} +{rule['score']}" for rule in tradability["avg_10d_volume"]
                    ),
                    "活跃度: " + "; ".join(
                        f"{rule['summary']} +{rule['score']}" for rule in tradability["activity"]
                    ),
                    "ATR 波动: " + "; ".join(
                        f"{rule['summary']} +{rule['score']}" for rule in tradability["atr_pct"]
                    ),
                    "日内波动: " + "; ".join(
                        f"{rule['summary']} +{rule['score']}" for rule in tradability["day_change_pct"]
                    ),
                    "bars 新鲜度: " + "; ".join(
                        f"{rule['summary']} +{rule['score']}" for rule in tradability["freshness"]
                    ),
                    "目标池加分: " + "; ".join(
                        f"{rule['summary']} +{rule['score']}" for rule in tradability["target_score"]
                    ),
                    f"方向奖励: {tradability['direction']['summary']} +{tradability['direction']['score']}",
                ],
            },
            {
                "title": "可操作判定",
                "copy": "页面上的 operable 标签还会叠加硬门槛，不只看 score。",
                "lines": [
                    "has_live_bar=true",
                    f"price > {tradability['operable_requirements']['price_gt']}",
                    f"avg_10d_volume >= {tradability['operable_requirements']['avg_10d_volume_gte']:,}",
                    f"tradability_score >= {tradability['operable_requirements']['score_gte']}",
                    f"freshness_min <= {tradability['operable_requirements']['freshness_lte_min']}m",
                ],
            },
        ],
    }


def _signal_panel(environment: str) -> dict:
    app_mod = get_app_module()
    app_mod.cfg.refresh()

    active_trade_symbols = sorted(get_active_trade_symbols(environment))
    trade_window_start, trade_window_start_source = _resolve_config_text(
        app_mod.cfg,
        "trade_window_start_time",
        environment,
        f"{DEFAULT_TRADE_WINDOW_START[0]:02d}:{DEFAULT_TRADE_WINDOW_START[1]:02d}",
    )
    trade_window_end, trade_window_end_source = _resolve_config_text(
        app_mod.cfg,
        "trade_window_end_time",
        environment,
        f"{DEFAULT_TRADE_WINDOW_END[0]:02d}:{DEFAULT_TRADE_WINDOW_END[1]:02d}",
    )
    order_window_end, order_window_end_source = _resolve_config_text(
        app_mod.cfg,
        "order_window_end_time",
        environment,
        f"{DEFAULT_ORDER_WINDOW_END[0]:02d}:{DEFAULT_ORDER_WINDOW_END[1]:02d}",
    )
    signal_validity_minutes, signal_validity_source = _resolve_config_text(
        app_mod.cfg,
        "signal_validity_minutes",
        environment,
        str(DEFAULT_SIGNAL_EXPIRY_MINUTES),
    )
    manual_confirm_enabled, manual_confirm_source = _resolve_config_text(
        app_mod.cfg,
        "signal_manual_confirm_enabled",
        environment,
        "true",
    )
    trading_enabled, trading_enabled_source = _resolve_config_text(
        app_mod.cfg,
        "ibkr_trading_enabled",
        environment,
        "true",
    )
    params = DEFAULT_PARAMS
    dtp_early_bars = int(params.get("dtp_early_bars", 12))

    return {
        "title": "当前信号规则",
        "subtitle": "结构规则来自 SignalGenerator，时间窗口来自 SignalProcessor / runtime config，参数直接读取当前代码默认值与运行配置。",
        "chips": [
            {
                "label": "EMA Touch",
                "value": str(params.get("ema_touch_type", "slow")),
                "copy": "IndicatorEngine.DEFAULT_PARAMS.ema_touch_type",
            },
            {
                "label": "DTP Early",
                "value": f"{dtp_early_bars} bars",
                "copy": "DTP 初期过滤阈值",
            },
            {
                "label": "交易窗口",
                "value": f"{trade_window_start}-{trade_window_end} ET",
                "copy": f"start={trade_window_start_source}, end={trade_window_end_source}",
            },
            {
                "label": "信号有效期",
                "value": f"{signal_validity_minutes}m",
                "copy": f"signal_validity_minutes · {signal_validity_source}",
            },
            {
                "label": "当前 Active",
                "value": _format_count(len(active_trade_symbols)),
                "copy": "当前 active target 数量，用于盘中关注集合",
            },
        ],
        "sections": [
            {
                "title": "四类结构信号",
                "copy": "MR 窗口被 SD 上下轨触发后，组件集齐才会出信号。",
                "lines": [
                    "做多 Type 1: sdUpper 窗口 + EMA bull touch + fractal_bull + bull divergence(cRSI/OBV 任一)",
                    "做多 Type 2: sdLower 窗口 + fractal_bull + bull divergence(cRSI/OBV 任一)",
                    "做空 Type 3: sdUpper 窗口 + fractal_bear + bear divergence(cRSI/OBV 任一)",
                    f"做空 Type 4: sdLower 窗口 + EMA bear touch + fractal_bear + bear divergence(cRSI/OBV 任一)",
                ],
            },
            {
                "title": "过滤与阻断",
                "copy": "组件满足后还要通过 DTP / EMA / 震荡过滤。",
                "lines": [
                    f"做多 Type 2: 若 dtp_dir=-1 且 (dtp_phase_bars <= {dtp_early_bars} 或 dtp_phase=confirmed) => block",
                    f"做空 Type 3: 若 dtp_dir=1 且 (dtp_phase_bars <= {dtp_early_bars} 或 dtp_phase=confirmed) => block",
                    "做多 Type 1 / 做空 Type 4: 若 block_ema_trend=true => block",
                    "任意方向: block_all_signals=true => block",
                    "market_monitor symbol 不出交易信号",
                ],
            },
            {
                "title": "时间与执行窗口",
                "copy": "即使有结构信号，也要通过运行态窗口校验。",
                "lines": [
                    f"trade_window_start_time={trade_window_start} ET ({trade_window_start_source})",
                    f"trade_window_end_time={trade_window_end} ET ({trade_window_end_source})",
                    f"order_window_end_time={order_window_end} ET ({order_window_end_source})",
                    f"signal_validity_minutes={signal_validity_minutes} ({signal_validity_source})",
                    f"signal_manual_confirm_enabled={manual_confirm_enabled} ({manual_confirm_source})",
                    f"ibkr_trading_enabled={trading_enabled} ({trading_enabled_source})",
                ],
            },
            {
                "title": "仓位与价格公式",
                "copy": "入场、止损、止盈和股数直接使用当前参数。",
                "lines": [
                    (
                        "Long: entry = close - ATR*"
                        f"{_format_number(params.get('entry_atr_mult', 1.0))}, "
                        "SL = max(entry - ATR*"
                        f"{_format_number(params.get('sl_atr_mult', 2.0))}, entry - max_loss/shares), "
                        "TP = entry + risk*"
                        f"{_format_number(params.get('rr_ratio', 1.5))}"
                    ),
                    (
                        "Short: entry = close + ATR*"
                        f"{_format_number(params.get('entry_atr_mult', 1.0))}, "
                        "SL = min(entry + ATR*"
                        f"{_format_number(params.get('sl_atr_mult', 2.0))}, entry + max_loss/shares), "
                        "TP = entry - risk*"
                        f"{_format_number(params.get('rr_ratio', 1.5))}"
                    ),
                    (
                        "position_amount="
                        f"{_format_number(params.get('position_amount', 10000))}, "
                        "max_loss_per_trade="
                        f"{_format_number(params.get('max_loss_per_trade', 150))}, "
                        "atr_multiplier="
                        f"{_format_number(params.get('atr_multiplier', 1.5))}"
                    ),
                ],
            },
        ],
    }


def build_rules_response():
    app_mod = get_app_module()
    environment = get_requested_environment("live")
    if environment not in app_mod.SUPPORTED_COMPUTE_ENVIRONMENTS:
        return jsonify({"ok": False, "error": "invalid_environment", "environment": environment}), 400

    timestamps = app_mod.build_runtime_timestamps()
    return jsonify(
        {
            "ok": True,
            "environment": environment,
            **timestamps,
            "selection": _selection_panel(environment),
            "signals": _signal_panel(environment),
        }
    )

from __future__ import annotations

import json

from flask import jsonify

from ibkr_compute.api.compute.runtime_state.universe import get_active_trade_symbols
from ibkr_compute.api.market.screener.scoring import build_tradability_rule_summary
from ibkr_compute.api.market.screener.watchlist import load_effective_watchlist
from ibkr_compute.api.shared.route_runtime import get_app_module, get_requested_environment
from ibkr_compute.core.indicator_engine import DEFAULT_PARAMS, indicator_ready_bar_count, params_for_interval
from ibkr_compute.signal.signal_processor import (
    DEFAULT_ORDER_WINDOW_END,
    DEFAULT_SIGNAL_EXPIRY_MINUTES,
    DEFAULT_TRADE_WINDOW_END,
    DEFAULT_TRADE_WINDOW_START,
)
from ibkr_compute.workflows.daily_scanner import build_daily_scan_rule_summary


SYSTEM_LOGIC_SCHEMA_VERSION = "system_logic_v1"
SYSTEM_LOGIC_TIMEFRAMES = ("5m", "15m", "30m", "1h", "4h", "1d")

SYSTEM_LOGIC_COVERAGE_DOMAINS = [
    {
        "id": "target_selection",
        "label": "标的选择 / 目标池",
        "source_modules": [
            "ibkr_compute.workflows.daily_scanner_constants",
            "ibkr_compute.workflows.daily_scanner_settings",
            "ibkr_compute.workflows.daily_scanner_evaluate",
            "ibkr_compute.api.market.screener.scoring",
            "ibkr_compute.api.market.screener.payload",
        ],
    },
    {
        "id": "data_indicators",
        "label": "行情数据 / 指标",
        "source_modules": [
            "ibkr_compute.core.indicator_engine",
            "ibkr_compute.core.indicators.ema_trend_matrix",
            "ibkr_compute.core.indicators.fractal_pivot",
            "ibkr_compute.core.indicators.sd_channel",
            "ibkr_compute.core.indicators.dtp",
            "ibkr_compute.core.indicators.divergence",
            "ibkr_compute.core.indicators.filters",
        ],
    },
    {
        "id": "signal_generation",
        "label": "信号生成",
        "source_modules": [
            "ibkr_compute.core.signal_generator",
            "ibkr_compute.core.position_sizing",
            "ibkr_compute.core.exit_policy",
        ],
    },
    {
        "id": "execution_validation",
        "label": "执行校验",
        "source_modules": [
            "ibkr_compute.signal.signal_processor",
            "ibkr_compute.orchestration.lifecycle",
            "ibkr_api.orders.routes",
        ],
    },
    {
        "id": "order_lifecycle",
        "label": "订单 / 生命周期 / 反向信号",
        "source_modules": [
            "ibkr_api.orders",
            "ibkr_api.reverse",
            "ibkr_api.universe.lifecycle_flow",
            "ibkr_compute.signal.reverse_signal",
        ],
    },
    {
        "id": "scheduler",
        "label": "定时调度",
        "source_modules": [
            "ibkr_scheduler.cron_registry",
            "ibkr_scheduler.scheduler_app",
            "ibkr_api.system.jobs",
        ],
    },
    {
        "id": "data_quality",
        "label": "数据质量 / 修复",
        "source_modules": [
            "ibkr_compute.market.bar_freshness",
            "ibkr_compute.backtest.market_data_coverage",
            "ibkr_api.system.jobs.data_gap",
        ],
    },
    {
        "id": "backtest_validation",
        "label": "回测 / 验证",
        "source_modules": [
            "ibkr_compute.backtest.portfolio",
            "ibkr_compute.backtest.scan_replay",
            "ibkr_compute.backtest.tv_parity",
            "ibkr_compute.backtest.execution_cost",
        ],
    },
]


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


def _parse_bool_text(value: str) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _resolve_config_bool_text(cfg, key: str, environment: str, default: str) -> tuple[str, str]:
    text, source = _resolve_config_text(cfg, key, environment, default)
    return ("true" if _parse_bool_text(text) else "false", source)


def _config_line(cfg, key: str, environment: str, default: str, label: str | None = None, suffix: str = "") -> str:
    value, source = _resolve_config_text(cfg, key, environment, default)
    display = f"{value}{suffix}" if suffix and value else value
    return f"{label or key} = {display} ({source})"


def _param_line(cfg, key: str, environment: str, label: str | None = None) -> str:
    default = DEFAULT_PARAMS.get(key, "")
    return _config_line(cfg, key, environment, str(default), label or key)


def _format_param_map(params: dict, keys: tuple[str, ...]) -> str:
    return " / ".join(f"{key}={params.get(key)}" for key in keys)


def _parse_json_object(value) -> dict:
    if isinstance(value, dict):
        return dict(value)
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _build_logic_coverage(section_ids: set[str]) -> list[dict]:
    return [
        {
            "id": item["id"],
            "label": item["label"],
            "status": "covered" if item["id"] in section_ids else "missing",
            "source_modules": list(item.get("source_modules") or []),
        }
        for item in SYSTEM_LOGIC_COVERAGE_DOMAINS
    ]


def _source_refs() -> list[dict]:
    return [
        {
            "domain": item["id"],
            "label": item["label"],
            "source_modules": list(item.get("source_modules") or []),
        }
        for item in SYSTEM_LOGIC_COVERAGE_DOMAINS
    ]


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
    daily_scan = build_daily_scan_rule_summary(environment)
    tradability = build_tradability_rule_summary()
    daily_scan_time, daily_scan_time_source = _resolve_config_text(
        app_mod.cfg,
        "ibkr_daily_scan_time_et",
        environment,
        str(daily_scan.get("scan_time_et") or "09:20"),
    )
    quality_gates = daily_scan.get("quality_gates") or {}
    subscription_budget = daily_scan.get("subscription_budget") or {}
    trade_budget = subscription_budget.get("trade_budget") or "unlimited"
    total_limit = int(subscription_budget.get("total_limit") or 0)
    monitor_count = int(subscription_budget.get("monitor_count") or 0)
    if total_limit > 0:
        budget_value = f"{trade_budget} trade / {total_limit} total"
        budget_copy = f"市场监控先预留 {monitor_count} 个 WS 名额"
    else:
        budget_value = str(trade_budget)
        budget_copy = f"当前 market monitor 预留 {monitor_count} 个名额"

    return {
        "title": "当前选标规则",
        "subtitle": "09:20 ET 先按 multi-TF 投票叠加量能/波动门槛产出 candidate / active，其中 trade active 会先扣除 monitor 订阅名额。",
        "chips": [
            {
                "label": "日筛时间",
                "value": f"{daily_scan_time} ET",
                "copy": f"ibkr_daily_scan_time_et · {daily_scan_time_source}",
            },
            {
                "label": "Trade Watchlist",
                "value": _format_count(len(trade_watchlist)),
                "copy": "当前参与日筛的 trade 标池数量",
            },
            {
                "label": "Trade WS Budget",
                "value": budget_value,
                "copy": budget_copy,
            },
            {
                "label": "Active Targets",
                "value": _format_count(len(active_trade_symbols)),
                "copy": "当前有效 active trade 集合，会参与盘中信号与优先盯盘",
            },
        ],
        "coverage_domains": ["target_selection"],
        "source_refs": [
            "ibkr_compute.workflows.daily_scanner",
            "ibkr_compute.api.market.screener.scoring",
            "ibkr_compute.api.market.screener.payload",
        ],
        "sections": [
            {
                "title": "09:20 日筛投票",
                "copy": "每个 ready timeframe 只要命中任一条件，就给对应方向加票并加分；方向打平直接淘汰。",
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
                "title": "量能与波动门槛",
                "copy": "只有同时过掉下面四个硬门槛的 symbol，才会进入今日自动目标池。",
                "lines": [
                    f"avg_10d_volume >= {_format_count(int(quality_gates.get('avg_10d_volume_gte') or 0))}",
                    f"premarket_volume >= {_format_count(int(quality_gates.get('premarket_volume_gte') or 0))}",
                    f"atr_pct >= {_format_number(quality_gates.get('atr_pct_gte') or 0)}",
                    f"|day_change_pct| >= {_format_number(quality_gates.get('abs_day_change_pct_gte') or 0)}%",
                    "通过后的自动候选会按 technical_score、|day_change_pct|、premarket_volume、avg_10d_volume、atr_pct 顺序排序。",
                ],
            },
            {
                "title": "WS 订阅预算",
                "copy": "active 不是无限扩张，先扣市场监控订阅，再给 trade 标的分配剩余额度。",
                "lines": [
                    f"总订阅上限: {total_limit if total_limit > 0 else 'unlimited'}",
                    f"market monitor 预留: {monitor_count}",
                    f"trade 可用预算: {trade_budget}",
                    "manual active 会优先保留；自动日筛只会把预算内的标的写成 active，其余保留 candidate。",
                ],
            },
            {
                "title": "盘中优先级",
                "copy": "进入目标池后，页面上的全量榜仍会按 tradability score 继续排序，用来做盘中盯盘优先级。",
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
    intraday_signal_validity_minutes = int(params.get("intraday_signal_validity_minutes", 15) or 15)
    trade_window_value = f"{trade_window_start}-{trade_window_end} ET"
    signal_validity_value = f"{signal_validity_minutes}m"
    active_count = _format_count(len(active_trade_symbols))
    formula_long = (
        "entry = close - ATR*"
        f"{_format_number(params.get('entry_atr_mult', 1.0))}, "
        "SL = max(entry - ATR*"
        f"{_format_number(params.get('sl_atr_mult', 2.0))}, entry - max_loss/shares), "
        "TP = entry + risk*"
        f"{_format_number(params.get('rr_ratio', 1.5))}"
    )
    formula_short = (
        "entry = close + ATR*"
        f"{_format_number(params.get('entry_atr_mult', 1.0))}, "
        "SL = min(entry + ATR*"
        f"{_format_number(params.get('sl_atr_mult', 2.0))}, entry + max_loss/shares), "
        "TP = entry - risk*"
        f"{_format_number(params.get('rr_ratio', 1.5))}"
    )
    formula_risk = (
        "position_amount="
        f"{_format_number(params.get('position_amount', 10000))}, "
        "max_loss_per_trade="
        f"{_format_number(params.get('max_loss_per_trade', 150))}, "
        "atr_multiplier="
        f"{_format_number(params.get('atr_multiplier', 1.5))}"
    )

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
                "value": trade_window_value,
                "copy": f"start={trade_window_start_source}, end={trade_window_end_source}",
            },
            {
                "label": "信号有效期",
                "value": signal_validity_value,
                "copy": f"signal_validity_minutes · {signal_validity_source}",
            },
            {
                "label": "Intraday Valid",
                "value": f"{intraday_signal_validity_minutes}m",
                "copy": "IndicatorEngine.DEFAULT_PARAMS.intraday_signal_validity_minutes",
            },
            {
                "label": "当前 Active",
                "value": active_count,
                "copy": "当前 active target 数量，用于盘中关注集合",
            },
        ],
        "highlights": [
            {
                "id": "window",
                "label": "交易窗口",
                "value": trade_window_value,
                "note": f"start={trade_window_start_source} · end={trade_window_end_source}",
                "tone": "accent",
            },
            {
                "id": "validity",
                "label": "信号有效期",
                "value": signal_validity_value,
                "note": f"signal_validity_minutes · {signal_validity_source}",
                "tone": "neutral",
            },
            {
                "id": "structure",
                "label": "结构入口",
                "value": "4 类结构 / 2 多 2 空",
                "note": "MR 窗口触发后，再看 EMA / fractal / divergence 组件",
                "tone": "neutral",
            },
            {
                "id": "blockers",
                "label": "关键阻断",
                "value": "DTP / EMA / 全局阻断",
                "note": "monitor-only symbol 不出交易信号",
                "tone": "warn",
            },
        ],
        "details": [
            {
                "id": "trigger",
                "title": "结构触发",
                "summary": "4 类结构 / 2 多 2 空",
                "tone": "neutral",
                "lines": [
                    "LONG T1 · sdUpper 窗口 + EMA bull touch + fractal_bull + bull divergence(cRSI/OBV 任一)",
                    "LONG T2 · sdLower 窗口 + fractal_bull + bull divergence(cRSI/OBV 任一)",
                    "SHORT T3 · sdUpper 窗口 + fractal_bear + bear divergence(cRSI/OBV 任一)",
                    "SHORT T4 · sdLower 窗口 + EMA bear touch + fractal_bear + bear divergence(cRSI/OBV 任一)",
                ],
            },
            {
                "id": "blockers",
                "title": "阻断条件",
                "summary": "DTP / EMA / 全局阻断 / monitor-only",
                "tone": "warn",
                "lines": [
                    f"DTP · LONG T2: dtp_dir=-1 且 (dtp_phase_bars <= {dtp_early_bars} 或 dtp_phase=confirmed) => block",
                    f"DTP · SHORT T3: dtp_dir=1 且 (dtp_phase_bars <= {dtp_early_bars} 或 dtp_phase=confirmed) => block",
                    "EMA trend · LONG T1 / SHORT T4: block_ema_trend=true => block",
                    "Global kill · 任意方向: block_all_signals=true => block",
                    "Monitor-only · market_monitor symbol 不出交易信号",
                ],
            },
            {
                "id": "execution",
                "title": "执行窗口",
                "summary": f"{trade_window_value} · valid {signal_validity_value}",
                "tone": "accent",
                "lines": [
                    f"trade_window_start_time = {trade_window_start} ET ({trade_window_start_source})",
                    f"trade_window_end_time = {trade_window_end} ET ({trade_window_end_source})",
                    f"order_window_end_time = {order_window_end} ET ({order_window_end_source})",
                    f"signal_validity_minutes = {signal_validity_minutes} ({signal_validity_source})",
                    f"intraday_signal_validity_minutes = {intraday_signal_validity_minutes} (code default)",
                    f"signal_manual_confirm_enabled = {manual_confirm_enabled} ({manual_confirm_source})",
                    f"ibkr_trading_enabled = {trading_enabled} ({trading_enabled_source})",
                ],
            },
            {
                "id": "formula",
                "title": "价格与仓位公式",
                "summary": "RR / 仓位 / 单笔风险",
                "tone": "muted",
                "lines": [
                    f"Long Formula: {formula_long}",
                    f"Short Formula: {formula_short}",
                    f"Risk Params: {formula_risk}",
                ],
            },
        ],
        "coverage_domains": ["signal_generation", "execution_validation"],
        "source_refs": [
            "ibkr_compute.core.signal_generator",
            "ibkr_compute.signal.signal_processor",
            "ibkr_compute.core.position_sizing",
            "ibkr_compute.core.exit_policy",
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
                    f"intraday_signal_validity_minutes={intraday_signal_validity_minutes} (code default)",
                    f"signal_manual_confirm_enabled={manual_confirm_enabled} ({manual_confirm_source})",
                    f"ibkr_trading_enabled={trading_enabled} ({trading_enabled_source})",
                ],
            },
            {
                "title": "仓位与价格公式",
                "copy": "入场、止损、止盈和股数直接使用当前参数。",
                "lines": [
                    f"Long: {formula_long}",
                    f"Short: {formula_short}",
                    formula_risk,
                ],
            },
        ],
    }


def _system_flow_panel(environment: str) -> dict:
    return {
        "title": "系统逻辑地图",
        "subtitle": "从观察池到信号、订单、生命周期、调度与验证的完整链路。",
        "coverage_domains": [
            "target_selection",
            "data_indicators",
            "signal_generation",
            "execution_validation",
            "order_lifecycle",
            "scheduler",
            "data_quality",
            "backtest_validation",
        ],
        "chips": [
            {"label": "Universe", "value": "watchlist -> targets", "copy": "trade / market_monitor 角色分离"},
            {"label": "Compute", "value": "bars -> indicators -> signals", "copy": "5m close 驱动盘中计算"},
            {"label": "Execution", "value": "signals -> orders -> lifecycle", "copy": "人工确认/风控/券商订单链路"},
            {"label": "Ops", "value": "scheduler + quality", "copy": "调度、修复、审计、日报"},
        ],
        "stages": [
            {
                "id": "watchlist",
                "label": "观察池",
                "summary": "按 symbol_role 划分 trade 与 market_monitor；trade 进入选标与信号链路，monitor 主要保留行情监控名额。",
                "links": ["/ibkr_screener.html?tab=watchlist", "/ibkr_config.html"],
            },
            {
                "id": "daily_scan",
                "label": "日筛 / 目标池",
                "summary": "09:20 ET 使用多周期投票、量能/波动硬门槛和订阅预算产出 candidate / active。",
                "links": ["/ibkr_screener.html?tab=screener&view=current"],
            },
            {
                "id": "bars_indicators",
                "label": "行情与指标",
                "summary": "IBKR bars 落库后按 5m/15m/30m/1h/4h/1d 计算指标快照，并检查 freshness / completeness。",
                "links": ["/ibkr_indicators.html", "/ibkr_chart.html"],
            },
            {
                "id": "signals",
                "label": "信号生成",
                "summary": "SignalGenerator 维护 SD 窗口状态，收集 EMA touch、分形和背离，经过 DTP/EMA/global 过滤后输出信号。",
                "links": ["/ibkr_signals.html", "/ibkr_screener.html?view=window-progress"],
            },
            {
                "id": "execution",
                "label": "执行校验",
                "summary": "SignalProcessor 校验交易开关、时间窗口、有效期、仓位容量、cooldown、方向一致性和价格结构。",
                "links": ["/ibkr_runtime.html", "/orders.html"],
            },
            {
                "id": "orders",
                "label": "订单生命周期",
                "summary": "信号确认后进入订单、成交、保护单、平仓/反向信号和 lifecycle event 可视化链路。",
                "links": ["/ibkr_lifecycle_flow.html", "/ibkr_reverse_signals.html"],
            },
            {
                "id": "scheduler_quality",
                "label": "调度 / 质量 / 验证",
                "summary": "ibkr-scheduler 统一触发日筛、补池、compute、数据质量、系统报告和存储治理；回测用于验证逻辑变更。",
                "links": ["/ibkr_system.html", "/ibkr_data_quality.html", "/ibkr_backtests.html"],
            },
        ],
        "sections": [
            {
                "title": "统一维护原则",
                "copy": "这张页面是系统逻辑的入口；新增或修改核心逻辑时必须同步更新自动规则源、展示或同步校验。",
                "lines": [
                    "策略/指标/筛选/执行/订单/调度/数据质量/回测逻辑都属于覆盖范围。",
                    "纯 UI 样式和普通文案不属于核心逻辑覆盖范围。",
                    "source_refs 和 coverage 清单用于发现页面没有覆盖的新逻辑域。",
                ],
            },
        ],
    }


def _indicator_panel(environment: str) -> dict:
    app_mod = get_app_module()
    app_mod.cfg.refresh()
    cfg = app_mod.cfg
    profile_text, profile_source = _resolve_config_text(
        cfg,
        "ibkr_timeframe_param_profiles_json",
        environment,
        str(getattr(cfg, "DEFAULTS", {}).get("ibkr_timeframe_param_profiles_json", "")),
    )
    parsed_profiles = _parse_json_object(profile_text)
    timeframe_lines = []
    for interval in SYSTEM_LOGIC_TIMEFRAMES:
        params = params_for_interval(DEFAULT_PARAMS, interval)
        timeframe_lines.append(
            f"{interval}: ready_bars={indicator_ready_bar_count(params)} / "
            f"{_format_param_map(params, ('sd_length', 'dtp_sma_length', 'dtp_atr_length', 'ema_slope_lookback'))}"
        )

    return {
        "title": "数据与指标规则",
        "subtitle": "IndicatorEngine 每根 bar 顺序计算子指标并合并为统一 snapshot；timeframe profile 可覆盖默认参数。",
        "coverage_domains": ["data_indicators"],
        "source_refs": [
            "ibkr_compute.core.indicator_engine",
            "ibkr_compute.core.indicators.*",
            "ibkr_compute.market.bar_freshness",
        ],
        "chips": [
            {
                "label": "Ready Bars",
                "value": str(indicator_ready_bar_count(DEFAULT_PARAMS)),
                "copy": "indicator_ready_bar_count(DEFAULT_PARAMS)",
            },
            {
                "label": "Timeframes",
                "value": ", ".join(SYSTEM_LOGIC_TIMEFRAMES),
                "copy": f"profile source: {profile_source}",
            },
            {
                "label": "EMA Touch",
                "value": str(DEFAULT_PARAMS.get("ema_touch_type", "slow")),
                "copy": "ema_touch_type 默认值",
            },
            {
                "label": "DTP Early",
                "value": f"{DEFAULT_PARAMS.get('dtp_early_bars', 12)} bars",
                "copy": "DTP 初期过滤阈值",
            },
        ],
        "sections": [
            {
                "title": "计算流水线",
                "copy": "每根新 bar 按固定顺序更新子指标，旧 bar 或重复 bar_time_ms 不重复计算。",
                "lines": [
                    "1. EMA Trend Matrix -> ema_bullish / ema_bearish / ema_touch / trend_dir",
                    "2. Fractal Pivot -> fractal_bull / fractal_bear，使用 Donchian baseline 确认",
                    "3. SD Channel -> sd_upper / sd_lower / sd_trend / squeeze / regime",
                    "4. DTP -> dtp_dir / dtp_phase / dtp_phase_bars，并读取 sd_trend 作为上下文",
                    "5. ATR + VWAP -> atr / atr_pct / vwap / rvol / dollar_volume",
                    "6. cRSI + OBV RSI + Divergence -> 多空背离与 hidden divergence",
                    "7. SignalFilters -> block_mr_long / block_mr_short / block_ema_trend / block_all_signals",
                ],
            },
            {
                "title": "核心参数",
                "copy": "默认值来自 IndicatorEngine.DEFAULT_PARAMS；环境配置可覆盖同名 key。",
                "lines": [
                    _param_line(cfg, "ema_slope_lookback", environment),
                    _param_line(cfg, "ema_min_angle", environment),
                    _param_line(cfg, "ema_min_spacing", environment),
                    _param_line(cfg, "fractal_period", environment),
                    _param_line(cfg, "donchian_period", environment),
                    _param_line(cfg, "sd_length", environment),
                    _param_line(cfg, "sd_signal_band", environment),
                    _param_line(cfg, "sd_filter_band", environment),
                    _param_line(cfg, "dtp_sma_length", environment),
                    _param_line(cfg, "dtp_atr_length", environment),
                    _param_line(cfg, "dtp_signal_band", environment),
                    _param_line(cfg, "atr_length", environment),
                    _param_line(cfg, "crsi_domcycle", environment),
                    _param_line(cfg, "obv_rsi_len", environment),
                    _param_line(cfg, "div_type", environment),
                ],
            },
            {
                "title": "分周期 Profile",
                "copy": f"ibkr_timeframe_param_profiles_json ({profile_source})；用于覆盖不同周期的指标窗口长度。",
                "lines": timeframe_lines + [
                    f"profile keys: {', '.join(sorted(parsed_profiles.keys())) if parsed_profiles else 'none'}",
                ],
            },
            {
                "title": "数据新鲜度与修复",
                "copy": "bars freshness 决定 operable / ready 状态，并影响数据质量 sweep 与修复计划。",
                "lines": [
                    _config_line(cfg, "ibkr_official_5m_close_delay_sec", environment, "3", "official 5m close delay", "s"),
                    _config_line(cfg, "ibkr_daily_scan_data_completeness_enabled", environment, "true"),
                    _config_line(cfg, "ibkr_daily_scan_data_completeness_blocking_enabled", environment, "true"),
                    _config_line(cfg, "ibkr_daily_scan_data_completeness_intervals", environment, "5m,15m,30m,1h,4h,1d"),
                    _config_line(cfg, "ibkr_daily_scan_data_completeness_blocking_intervals", environment, "5m"),
                ],
            },
        ],
    }


def _execution_panel(environment: str) -> dict:
    app_mod = get_app_module()
    app_mod.cfg.refresh()
    cfg = app_mod.cfg
    trade_window_start, trade_window_start_source = _resolve_config_text(
        cfg,
        "trade_window_start_time",
        environment,
        f"{DEFAULT_TRADE_WINDOW_START[0]:02d}:{DEFAULT_TRADE_WINDOW_START[1]:02d}",
    )
    trade_window_end, trade_window_end_source = _resolve_config_text(
        cfg,
        "trade_window_end_time",
        environment,
        f"{DEFAULT_TRADE_WINDOW_END[0]:02d}:{DEFAULT_TRADE_WINDOW_END[1]:02d}",
    )
    order_window_end, order_window_end_source = _resolve_config_text(
        cfg,
        "order_window_end_time",
        environment,
        f"{DEFAULT_ORDER_WINDOW_END[0]:02d}:{DEFAULT_ORDER_WINDOW_END[1]:02d}",
    )
    signal_validity_minutes, signal_validity_source = _resolve_config_text(
        cfg,
        "signal_validity_minutes",
        environment,
        str(DEFAULT_SIGNAL_EXPIRY_MINUTES),
    )
    manual_confirm_enabled, manual_confirm_source = _resolve_config_bool_text(
        cfg,
        "signal_manual_confirm_enabled",
        environment,
        "true",
    )
    position_limit_max, position_limit_source = _resolve_config_text(
        cfg,
        "position_limit_max",
        environment,
        "0",
    )
    max_strategy_positions, max_strategy_positions_source = _resolve_config_text(
        cfg,
        "max_strategy_open_positions",
        environment,
        "5",
    )
    return {
        "title": "执行校验与风控规则",
        "subtitle": "结构信号生成后还必须通过 SignalProcessor 与订单链路的运行态校验。",
        "coverage_domains": ["execution_validation"],
        "source_refs": [
            "ibkr_compute.signal.signal_processor",
            "ibkr_compute.core.position_sizing",
            "ibkr_compute.core.exit_policy",
            "ibkr_api.orders.routes",
        ],
        "chips": [
            {
                "label": "Trade Window",
                "value": f"{trade_window_start}-{trade_window_end} ET",
                "copy": f"{trade_window_start_source} / {trade_window_end_source}",
            },
            {
                "label": "Order Window",
                "value": f"until {order_window_end} ET",
                "copy": f"order_window_end_time · {order_window_end_source}",
            },
            {
                "label": "Signal Validity",
                "value": f"{signal_validity_minutes}m",
                "copy": f"signal_validity_minutes · {signal_validity_source}",
            },
            {
                "label": "Manual Confirm",
                "value": manual_confirm_enabled,
                "copy": manual_confirm_source,
            },
        ],
        "sections": [
            {
                "title": "SignalProcessor 校验顺序",
                "copy": "任何一步失败都会返回原因并阻止进入执行。",
                "lines": [
                    "ibkr_trading_enabled / ibkr_live_trading_enabled 必须开启；live 环境额外检查 live trading 开关。",
                    "warmup/readiness provider 必须 open，否则返回 warmup_incomplete / warmup_status_error。",
                    f"当前 ET 时间必须位于 trade window {trade_window_start}-{trade_window_end} 且下单不晚于 {order_window_end}。",
                    f"order_window_end_time = {order_window_end} ET ({order_window_end_source})",
                    f"signal_time 超过 signal_validity_minutes={signal_validity_minutes} 后过期。",
                    "止损熔断、position limit、strategy capacity、fixed_position_symbols 都可阻断。",
                    "cooldown、同标的方向冲突、target direction alignment、entry/SL/TP 价格结构必须通过。",
                ],
            },
            {
                "title": "容量与冷却",
                "copy": "用于避免同方向过度暴露或止损后立即重复入场。",
                "lines": [
                    f"position_limit_max = {position_limit_max} ({position_limit_source})",
                    f"max_strategy_open_positions = {max_strategy_positions} ({max_strategy_positions_source})",
                    _config_line(cfg, "cooldown_bars_after_sl", environment, "6"),
                    _config_line(cfg, "cooldown_bars_after_reverse", environment, "3"),
                    _config_line(cfg, "ibkr_require_target_direction_alignment", environment, "true"),
                    _config_line(cfg, "fixed_position_symbols", environment, "BOXX,IBKR"),
                ],
            },
            {
                "title": "价格、仓位与退出策略",
                "copy": "SignalGenerator 构造信号时计算 entry / stop_loss / take_profit / shares，再套用 exit policy。",
                "lines": [
                    _param_line(cfg, "position_amount", environment),
                    _param_line(cfg, "max_loss_per_trade", environment),
                    _param_line(cfg, "entry_atr_mult", environment),
                    _param_line(cfg, "sl_atr_mult", environment),
                    _param_line(cfg, "rr_ratio", environment),
                    _param_line(cfg, "entry_limit_mode", environment),
                    _param_line(cfg, "exit_policy_profile", environment),
                    "Long: entry = close - ATR*entry_atr_mult; SL 在 ATR stop 与 max_loss 约束内取更保守值; TP = entry + risk*rr_ratio。",
                    "Short: entry = close + ATR*entry_atr_mult; SL 在 ATR stop 与 max_loss 约束内取更保守值; TP = entry - risk*rr_ratio。",
                ],
            },
        ],
    }


def _orders_panel(environment: str) -> dict:
    app_mod = get_app_module()
    app_mod.cfg.refresh()
    cfg = app_mod.cfg
    return {
        "title": "订单生命周期与反向信号",
        "subtitle": "确认后的信号会进入订单、成交、保护单、平仓/反向和生命周期事件链路。",
        "coverage_domains": ["order_lifecycle"],
        "source_refs": [
            "ibkr_api.orders.*",
            "ibkr_api.universe.lifecycle_flow",
            "ibkr_api.reverse.*",
            "ibkr_compute.signal.reverse_signal",
        ],
        "chips": [
            {"label": "Order Validity", "value": f"{_resolve_config_text(cfg, 'order_validity_minutes', environment, '30')[0]}m", "copy": "订单过期取消窗口"},
            {"label": "Reverse", "value": _resolve_config_text(cfg, "reverse_flip_enabled", environment, "false")[0], "copy": "reverse_flip_enabled"},
            {"label": "SL Circuit", "value": _resolve_config_text(cfg, "consecutive_stop_loss_limit", environment, "3")[0], "copy": "连续止损熔断阈值"},
            {"label": "EOD", "value": _resolve_config_text(cfg, "eod_close_time", environment, "15:55")[0], "copy": "收盘处理时间"},
        ],
        "sections": [
            {
                "title": "主订单链路",
                "copy": "从信号到券商订单的核心状态流。",
                "lines": [
                    "signal pending / awaiting_confirm -> 人工确认或自动策略确认 -> order place。",
                    "Init / Submitted 订单由 active polling 和 webhook/同步逻辑更新。",
                    "fill 后记录实际成交来源，并创建或校验保护单。",
                    "生命周期页按 signal_id / trade_group_id / order_id 汇总节点与事件。",
                ],
            },
            {
                "title": "保护单与平仓",
                "copy": "止盈止损、动态保护单和 EOD 策略共同控制退出。",
                "lines": [
                    _config_line(cfg, "atr_dynamic_stop_enabled", environment, "true"),
                    _config_line(cfg, "live_exit_policy_stop_update_enabled", environment, "false"),
                    _config_line(cfg, "atr_stop_min_profit_r", environment, "0.3"),
                    _config_line(cfg, "atr_stop_deviation_threshold", environment, "0.30"),
                    _config_line(cfg, "eod_close_time", environment, "15:55"),
                    _config_line(cfg, "eod_keep_symbols", environment, "BOXX,IBKR"),
                ],
            },
            {
                "title": "反向信号",
                "copy": "反向信号用于平仓、调整或翻向；默认不开启直接 flip。",
                "lines": [
                    _config_line(cfg, "reverse_flip_enabled", environment, "false"),
                    _config_line(cfg, "reverse_signal_threshold", environment, "6"),
                    "reverse_signal.py 负责反向条件计算，ibkr_api.reverse 负责 ack / dispatch / pending。",
                    "反向结果会关联原 signal_id / trade_group_id，便于生命周期链路追踪。",
                ],
            },
        ],
    }


def _quality_panel(environment: str) -> dict:
    app_mod = get_app_module()
    app_mod.cfg.refresh()
    cfg = app_mod.cfg
    return {
        "title": "数据质量与修复逻辑",
        "subtitle": "数据完整性影响 daily scan、ready/operable、信号生成和回测可信度。",
        "coverage_domains": ["data_quality"],
        "source_refs": [
            "ibkr_compute.market.bar_freshness",
            "ibkr_compute.backtest.market_data_coverage",
            "ibkr_compute.backtest.market_data_backfill",
            "ibkr_api.system.jobs.data_gap",
        ],
        "chips": [
            {"label": "5m Delay", "value": f"{_resolve_config_text(cfg, 'ibkr_official_5m_close_delay_sec', environment, '3')[0]}s", "copy": "official close 等待"},
            {"label": "Completeness", "value": _resolve_config_text(cfg, "ibkr_daily_scan_data_completeness_blocking_enabled", environment, "true")[0], "copy": "日筛阻断开关"},
            {"label": "Repair", "value": "sweep + targeted", "copy": "缺口扫描与修复"},
            {"label": "Truth Audit", "value": "IBKR history", "copy": "权威历史对账"},
        ],
        "sections": [
            {
                "title": "Freshness 判定",
                "copy": "通过 latest stored bar 与 expected closed bar 对比，产出 ready/stale/missing。",
                "lines": [
                    _config_line(cfg, "ibkr_official_5m_close_delay_sec", environment, "3", "official close delay", "s"),
                    "5m 使用 latest_expected_extended_5m_ms；高周期使用 latest 5m 推导 expected closed time。",
                    "freshness 会进入 screener operable、current targets、data quality 和 monitor flags。",
                ],
            },
            {
                "title": "日筛数据完整性",
                "copy": "日筛前检查所需 interval，blocking interval 不完整时可阻断候选。",
                "lines": [
                    _config_line(cfg, "ibkr_daily_scan_data_completeness_enabled", environment, "true"),
                    _config_line(cfg, "ibkr_daily_scan_data_completeness_blocking_enabled", environment, "true"),
                    _config_line(cfg, "ibkr_daily_scan_data_completeness_intervals", environment, "5m,15m,30m,1h,4h,1d"),
                    _config_line(cfg, "ibkr_daily_scan_data_completeness_blocking_intervals", environment, "5m"),
                    _config_line(cfg, "ibkr_daily_scan_auto_retry_enabled", environment, "true"),
                    _config_line(cfg, "ibkr_daily_scan_retry_delays_sec", environment, "60,120,240"),
                ],
            },
            {
                "title": "修复与审计",
                "copy": "数据质量页和 scheduler 负责全观察池 sweep、truth audit、targeted repair。",
                "lines": [
                    "repair sweep 尝试修复可回补缺口；truth audit 对比 stored bars 与 IBKR authoritative history。",
                    "data_gap_guard 根据 monitor flags 和市场活动状态发告警。",
                    "storage governor 清理可重建指标、旧日志、TV 兼容数据和旧回测产物。",
                ],
            },
        ],
    }


def _backtest_validation_panel(environment: str) -> dict:
    return {
        "title": "回测与验证链路",
        "subtitle": "任何规则变更都应能用 backtest / replay / parity 工具复盘验证。",
        "coverage_domains": ["backtest_validation"],
        "source_refs": [
            "ibkr_compute.backtest.portfolio",
            "ibkr_compute.backtest.scan_replay",
            "ibkr_compute.backtest.runtime_records",
            "ibkr_compute.backtest.tv_parity",
            "ibkr_compute.backtest.execution_cost",
        ],
        "chips": [
            {"label": "Default Source", "value": "daily_scan_replay", "copy": "使用每日入选结果重放"},
            {"label": "Signal Engine", "value": "live SD", "copy": "尽量贴近盘中逻辑"},
            {"label": "Cost", "value": "execution profile", "copy": "成交成本/滑点画像"},
            {"label": "Parity", "value": "TV / live compare", "copy": "与 TradingView/实时结果对照"},
        ],
        "sections": [
            {
                "title": "推荐验证路径",
                "copy": "新增或修改策略逻辑时，至少验证 daily scan -> signal -> portfolio 链路。",
                "lines": [
                    "daily_scan_replay: 使用每日筛选缓存重建入选集合，避免手工标的偏差。",
                    "portfolio stream: 验证 confirm/fill/TP/SL/reverse/保护单调整链路。",
                    "TV parity: 对照 TradingView 指标或信号，定位指标/窗口差异。",
                    "execution cost profile: 使用实际/近期 fills 校准滑点和成交成本。",
                ],
            },
            {
                "title": "验收口径",
                "copy": "回测不是交易结果保证，但应覆盖逻辑分支与数据质量风险。",
                "lines": [
                    "必须报告未覆盖场景，不补造数据。",
                    "规则变更前后应比较 selected count、signal count、blocked reasons 和核心绩效指标。",
                    "回测产物应记录 request params、source refs、signal_validity_minutes、trade_window_start_time 等关键信息。",
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
    section_ids = {
        item["id"]
        for item in SYSTEM_LOGIC_COVERAGE_DOMAINS
    }
    return jsonify(
        {
            "ok": True,
            "environment": environment,
            "schema_version": SYSTEM_LOGIC_SCHEMA_VERSION,
            **timestamps,
            "system_flow": _system_flow_panel(environment),
            "selection": _selection_panel(environment),
            "indicators": _indicator_panel(environment),
            "signals": _signal_panel(environment),
            "execution": _execution_panel(environment),
            "orders": _orders_panel(environment),
            "quality": _quality_panel(environment),
            "backtest_validation": _backtest_validation_panel(environment),
            "source_refs": _source_refs(),
            "logic_coverage": _build_logic_coverage(section_ids),
        }
    )

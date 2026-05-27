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
from ibkr_compute.universe.activity_gate import parse_activity_gate_stages
from ibkr_compute.workflows.daily_scanner import build_daily_scan_rule_summary


SYSTEM_LOGIC_SCHEMA_VERSION = "system_logic_v2"
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
        "id": "order_flow",
        "label": "订单流 / 自动确认",
        "source_modules": [
            "ibkr_compute.order_flow.manager",
            "ibkr_compute.order_flow.aggregator",
            "ibkr_compute.order_flow.candidate_queue",
            "ibkr_compute.order_flow.execution_pool",
            "ibkr_compute.orchestration.signals",
            "ibkr_compute.order.order_placer",
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
        "id": "broker_mode_switch",
        "label": "Broker 模式切换 / 2FA",
        "source_modules": [
            "ibkr_api.control.broker_mode_switch",
            "ibkr_api.control.routes",
            "ibkr_api.runtime.two_factor",
            "ibkr_api.two_factor.*",
            "ibkr_compute.broker.ib_gateway_service",
            "ibkr_compute.orchestration.auth_recovery",
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


def _format_activity_threshold(key: str, value) -> str:
    metric = str(key or "").removesuffix("_gte")
    labels = {
        "premarket_volume": "premarket_volume",
        "regular_volume": "regular_volume",
        "today_volume": "today_volume",
        "elapsed_rvol": "elapsed_rvol",
        "rvol_20": "rvol_20",
    }
    if metric in {"elapsed_rvol", "rvol_20"}:
        display_value = _format_number(value)
    else:
        display_value = _format_count(int(float(value or 0)))
    return f"{labels.get(metric, metric)} >= {display_value}"


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
        str(daily_scan.get("scan_time_et") or "08:20"),
    )
    scan_schedule, scan_schedule_source = _resolve_config_text(
        app_mod.cfg,
        "ibkr_scan_schedule",
        environment,
        "08:20-09:20",
    )
    quality_gates = daily_scan.get("quality_gates") or {}
    activity_stage_lines = []
    for stage in parse_activity_gate_stages(quality_gates.get("activity_gate_stages_json")):
        thresholds = " or ".join(
            _format_activity_threshold(key, value)
            for key, value in (stage.get("any_of") or {}).items()
        )
        activity_stage_lines.append(
            f"{stage.get('start_et')}-{stage.get('end_et')}: {thresholds}"
        )
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
        "subtitle": "08:20-09:20 ET 每 5 分钟覆盖预筛，09:25-11:00 ET 每 5 分钟只做增量入池；multi-TF 投票叠加分阶段量能/波动门槛产出 candidate / active。",
        "chips": [
            {
                "label": "预筛窗口",
                "value": f"{scan_schedule} ET",
                "copy": f"ibkr_scan_schedule · {scan_schedule_source}",
            },
            {
                "label": "预筛起始",
                "value": f"{daily_scan_time} ET",
                "copy": f"ibkr_daily_scan_time_et · {daily_scan_time_source}",
            },
            {
                "label": "增量入池",
                "value": "09:25-11:00 ET",
                "copy": "ibkr_early_expansion_topup · 只新增符合条件的标的，不移除已有 active",
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
                "title": "08:20-09:20 覆盖预筛投票",
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
                "title": "分阶段成交活跃度与波动门槛",
                "copy": "10D 均量、波动仍是硬门槛；成交活跃度按扫描阶段切换，盘前看 premarket_volume，开盘后看 regular_volume / elapsed_rvol。",
                "lines": [
                    f"avg_10d_volume >= {_format_count(int(quality_gates.get('avg_10d_volume_gte') or 0))}",
                    *(activity_stage_lines or [
                        f"premarket_volume >= {_format_count(int(quality_gates.get('premarket_volume_gte') or 0))}"
                    ]),
                    f"atr_pct >= {_format_number(quality_gates.get('atr_pct_gte') or 0)}",
                    f"|day_change_pct| >= {_format_number(quality_gates.get('abs_day_change_pct_gte') or 0)}%",
                    "通过后的自动候选会按 technical_score、|day_change_pct|、阶段成交活跃度、avg_10d_volume、atr_pct 顺序排序。",
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
            "order_flow",
            "order_lifecycle",
            "broker_mode_switch",
            "scheduler",
            "data_quality",
            "backtest_validation",
        ],
        "chips": [
            {"label": "Universe", "value": "watchlist -> targets", "copy": "trade / market_monitor 角色分离"},
            {"label": "Compute", "value": "bars -> indicators -> signals", "copy": "5m close 驱动盘中计算"},
            {"label": "Order Flow", "value": "CVD -> confirm / exit", "copy": "订单流自动确认、提前退出、只收紧止损"},
            {"label": "Execution", "value": "signals -> orders -> lifecycle", "copy": "风控 / 券商订单 / 生命周期链路"},
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
                "summary": "08:20-09:20 ET 覆盖预筛使用多周期投票、分阶段量能/波动硬门槛和订阅预算产出 candidate / active；09:25-11:00 ET 只做增量入池。",
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
                "id": "order_flow",
                "label": "订单流确认",
                "summary": "OrderFlowManager 聚合 tick-by-tick CVD，管理 candidate queue / execution pool；enforce 模式确认开仓、提前平仓或只收紧止损。",
                "links": ["/ibkr_runtime.html", "/ibkr_signals.html"],
            },
            {
                "id": "execution",
                "label": "执行校验",
                "summary": "SignalProcessor 校验交易开关、时间窗口、有效期、仓位容量、cooldown、方向一致性和价格结构；订单流通过后才进入券商下单。",
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
                "label": "运行控制 / 调度 / 验证",
                "summary": "Broker 模式切换先走账户/2FA/Gateway 风险检查；ibkr-scheduler 统一触发日筛、compute、数据质量、系统报告和存储治理。",
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


def _order_flow_panel(environment: str) -> dict:
    app_mod = get_app_module()
    app_mod.cfg.refresh()
    cfg = app_mod.cfg

    enabled, enabled_source = _resolve_config_bool_text(cfg, "ibkr_order_flow_enabled", environment, "true")
    mode, mode_source = _resolve_config_text(cfg, "ibkr_order_flow_mode", environment, "enforce")
    pool_size, pool_size_source = _resolve_config_text(cfg, "ibkr_order_flow_execution_pool_size", environment, "3")
    active_limit, active_limit_source = _resolve_config_text(cfg, "ibkr_order_flow_active_limit", environment, "3")
    position_slots, position_slots_source = _resolve_config_text(
        cfg,
        "ibkr_order_flow_max_position_slots",
        environment,
        "1",
    )
    confirm_window, confirm_window_source = _resolve_config_text(
        cfg,
        "ibkr_order_flow_confirm_window_sec",
        environment,
        "60",
    )
    min_delta_ratio, min_delta_source = _resolve_config_text(
        cfg,
        "ibkr_order_flow_min_delta_ratio",
        environment,
        "0.12",
    )
    max_spread_bps, max_spread_source = _resolve_config_text(
        cfg,
        "ibkr_order_flow_max_spread_bps",
        environment,
        "12",
    )
    entry_timeout, entry_timeout_source = _resolve_config_text(
        cfg,
        "ibkr_order_flow_entry_timeout_sec",
        environment,
        "60",
    )
    marketable_bps, marketable_bps_source = _resolve_config_text(
        cfg,
        "ibkr_order_flow_marketable_limit_bps",
        environment,
        "8",
    )
    manual_confirm, manual_confirm_source = _resolve_config_bool_text(
        cfg,
        "signal_manual_confirm_enabled",
        environment,
        "false",
    )

    return {
        "title": "订单流自动确认与执行规则",
        "subtitle": "OrderFlowManager 使用 tick-by-tick Last 聚合 CVD / delta，管理候选队列和执行池；默认 enforce 模式会参与开仓、提前平仓与止损收紧。",
        "coverage_domains": ["order_flow", "execution_validation"],
        "source_refs": [
            "ibkr_compute.order_flow.manager",
            "ibkr_compute.order_flow.aggregator",
            "ibkr_compute.order_flow.candidate_queue",
            "ibkr_compute.order_flow.execution_pool",
            "ibkr_compute.orchestration.signals",
            "ibkr_compute.order.order_placer",
        ],
        "chips": [
            {
                "label": "Order Flow",
                "value": f"{enabled} / {mode}",
                "copy": f"ibkr_order_flow_enabled={enabled_source}, ibkr_order_flow_mode={mode_source}",
            },
            {
                "label": "Execution Pool",
                "value": pool_size,
                "copy": f"ibkr_order_flow_execution_pool_size · {pool_size_source}",
            },
            {
                "label": "Position Slots",
                "value": position_slots,
                "copy": f"ibkr_order_flow_max_position_slots · {position_slots_source}",
            },
            {
                "label": "CVD Window",
                "value": f"{confirm_window}s / {min_delta_ratio}",
                "copy": f"window={confirm_window_source}, ratio={min_delta_source}",
            },
            {
                "label": "Entry Timeout",
                "value": f"{entry_timeout}s",
                "copy": f"ibkr_order_flow_entry_timeout_sec · {entry_timeout_source}",
            },
            {
                "label": "Manual Confirm",
                "value": manual_confirm,
                "copy": f"signal_manual_confirm_enabled · {manual_confirm_source}",
            },
        ],
        "highlights": [
            {
                "id": "mode",
                "label": "默认模式",
                "value": f"{enabled} / {mode}",
                "note": "confirm/enforce 会等待订单流确认；shadow 只记录与订阅，不阻断交易",
                "tone": "accent",
            },
            {
                "id": "capacity",
                "label": "执行池",
                "value": f"{pool_size} symbols / {position_slots} position",
                "note": "entry slots = execution_pool_size - max_position_slots",
                "tone": "neutral",
            },
            {
                "id": "risk",
                "label": "硬保护",
                "value": "never widen stop",
                "note": "订单流只能提前退出、减仓或收紧止损，不能放宽止损",
                "tone": "warn",
            },
        ],
        "details": [
            {
                "id": "cvd",
                "title": "CVD 聚合",
                "summary": "tick-by-tick Last -> 10/30/60s bars",
                "tone": "accent",
                "lines": [
                    "OrderFlowAggregator 将 signed trade ticks 聚合成 10s / 30s / 60s CVD bars。",
                    "buy_volume / sell_volume / delta / cvd_close 会进入确认逻辑。",
                    "同 symbol 的 out-of-order tick 会被拒绝并写入 last_error。",
                    _config_line(cfg, "ibkr_order_flow_tick_types", environment, "Last"),
                    f"ibkr_order_flow_confirm_window_sec = {confirm_window} ({confirm_window_source})",
                    f"ibkr_order_flow_min_delta_ratio = {min_delta_ratio} ({min_delta_source})",
                ],
            },
            {
                "id": "entry",
                "title": "开仓确认",
                "summary": "wait -> allow / reject",
                "tone": "neutral",
                "lines": [
                    "disabled 或 auto_entry=false 时直接 allow，并标记 enforced=false。",
                    "shadow 模式只 observe candidate，不阻断下单。",
                    "confirm/enforce 模式必须同时通过 execution pool 分配、quote spread、方向 delta ratio。",
                    f"max_spread_bps = {max_spread_bps} ({max_spread_source})",
                    f"entry_timeout_sec = {entry_timeout} ({entry_timeout_source})；超时返回 order_flow_timeout 并释放候选 watch。",
                    f"确认后使用 marketable LMT: long=ask+{marketable_bps}bps, short=bid-{marketable_bps}bps ({marketable_bps_source})。",
                ],
            },
            {
                "id": "position",
                "title": "持仓管理",
                "summary": "提前平仓或只收紧止损",
                "tone": "warn",
                "lines": [
                    _config_line(cfg, "ibkr_order_flow_auto_exit_enabled", environment, "true"),
                    _config_line(cfg, "ibkr_order_flow_stop_tighten_enabled", environment, "true"),
                    _config_line(cfg, "ibkr_order_flow_exit_delta_ratio", environment, "0.18"),
                    _config_line(cfg, "ibkr_order_flow_stop_delta_ratio", environment, "0.12"),
                    _config_line(cfg, "ibkr_order_flow_close_fill_timeout_sec", environment, "5", suffix="s"),
                    _config_line(cfg, "never_widen_stop_by_order_flow", environment, "true"),
                    "强反向 CVD 且 pnl_r <= 0.15 时触发 full_exit；否则达到 stop 阈值时只尝试 tighten_stop。",
                ],
            },
        ],
        "sections": [
            {
                "title": "Candidate Queue",
                "copy": "订单流候选先进入队列，再争取 execution pool 名额。",
                "lines": [
                    _config_line(cfg, "candidate_queue_max", environment, "10"),
                    _config_line(cfg, "candidate_breakout_ttl_sec", environment, "120", suffix="s"),
                    _config_line(cfg, "candidate_pullback_ttl_sec", environment, "300", suffix="s"),
                    _config_line(cfg, "candidate_reversal_ttl_sec", environment, "600", suffix="s"),
                    "同 symbol 同向同策略会 merge；反向冲突时强者替换弱者，弱者被 rejected_conflict。",
                ],
            },
            {
                "title": "Execution Pool",
                "copy": "4 核 8G 默认限制为小池，避免 tick-by-tick 订阅和持仓 watch 无限扩张。",
                "lines": [
                    f"ibkr_order_flow_execution_pool_size = {pool_size} ({pool_size_source})",
                    f"ibkr_order_flow_active_limit = {active_limit} ({active_limit_source})；旧 key 仅兼容。",
                    f"ibkr_order_flow_max_position_slots = {position_slots} ({position_slots_source})",
                    _config_line(cfg, "entry_watch_after_fill_sec", environment, "180", suffix="s"),
                    "filled 后保留 position watch；cancelled / rejected / timeout 会释放 entry slot。",
                ],
            },
            {
                "title": "默认自动确认链路",
                "copy": "当前默认不再要求人工确认每条新信号，而是先走订单流与风控校验。",
                "lines": [
                    f"signal_manual_confirm_enabled = {manual_confirm} ({manual_confirm_source})",
                    _config_line(cfg, "quality_auto_full_min", environment, "80"),
                    _config_line(cfg, "quality_auto_small_min", environment, "75"),
                    _config_line(cfg, "quality_shadow_min", environment, "70"),
                    _config_line(cfg, "new_entry_cutoff_time", environment, "14:45"),
                    _config_line(cfg, "force_flat_time", environment, "15:45"),
                ],
            },
        ],
    }


def _broker_mode_switch_panel(environment: str) -> dict:
    return {
        "title": "Broker 模式切换与 2FA 安全规则",
        "subtitle": "paper/live 切换必须先通过账户、PB 订单组、Gateway 配置和 2FA 状态检查；切换写配置后按固定顺序重启服务。",
        "coverage_domains": ["broker_mode_switch"],
        "source_refs": [
            "ibkr_api.control.broker_mode_switch",
            "ibkr_api.control.routes",
            "ibkr_api.runtime.two_factor",
            "ibkr_api.two_factor.*",
            "ibkr_compute.broker.ib_gateway_service",
            "ibkr_compute.orchestration.auth_recovery",
        ],
        "chips": [
            {
                "label": "Preview API",
                "value": "required",
                "copy": "/api/custom/ibkr/broker-mode/switch/preview",
            },
            {
                "label": "Confirm Text",
                "value": "SWITCH TARGET",
                "copy": "必须精确输入 SWITCH LIVE 或 SWITCH PAPER",
            },
            {
                "label": "Hard Blockers",
                "value": "positions / orders / 2FA",
                "copy": "任一 blocker 存在时返回 409，不写配置",
            },
            {
                "label": "Restart Plan",
                "value": "Gateway first",
                "copy": "stop runtime -> restart gateway -> runtime/compute/scheduler/api",
            },
        ],
        "highlights": [
            {
                "id": "guard",
                "label": "切换前置",
                "value": "preview first",
                "note": "执行前会再跑一次 preview，避免状态在确认后变更",
                "tone": "accent",
            },
            {
                "id": "accounts",
                "label": "账户隔离",
                "value": "paper/live keys",
                "note": "live 需要显式 IBKR_LIVE_USERNAME；paper 可回退 IBKR_USERNAME",
                "tone": "neutral",
            },
            {
                "id": "2fa",
                "label": "2FA",
                "value": "active blocks",
                "note": "已有 Gateway 验证流程未完成时禁止切换",
                "tone": "warn",
            },
        ],
        "details": [
            {
                "id": "blockers",
                "title": "Preview Blockers",
                "summary": "任何一项命中都阻断切换",
                "tone": "warn",
                "lines": [
                    "目标账户 ID 缺失: live=IBKR_ACCOUNT_ID, paper=IBKR_PAPER_ACCOUNT_ID。",
                    "目标 Gateway 登录名或密码缺失；live 不接受 legacy IBKR_USERNAME 作为显式 live 登录名。",
                    "IBC config.ini 不存在、不可读或不可写。",
                    "任一运行 .env 文件不存在或不可读，不能保证所有服务同步切换。",
                    "当前账户快照不可用、仍有持仓、IBKR 挂单或 PB active/stale/shadow 订单组。",
                    "2FA / Gateway 验证流程处于 requested/pending/waiting/responded/submitted/running 或 recovery 等待状态。",
                ],
            },
            {
                "id": "write",
                "title": "写配置规则",
                "summary": "先备份，再原子替换",
                "tone": "neutral",
                "lines": [
                    "确认文本必须等于 preview 返回的 confirm_text。",
                    "切换过程使用进程内锁，已有切换进行中时返回 broker_mode_switch_in_progress。",
                    ".env 写入 IBKR_BROKER_MODE 与 IBKR_GATEWAY_MODE；若提供目标凭据，也同步 IBKR_USERNAME / IBKR_PASSWORD。",
                    "原模式的 legacy 登录信息会先补到 IBKR_LIVE_* 或 IBKR_PAPER_*，避免切回时丢凭据。",
                    "IBC config.ini 写入 IbLoginId / IbPassword / TradingMode / OverrideTwsApiPort。",
                    "如果 IBC 写入失败，不会继续重启服务。",
                ],
            },
            {
                "id": "restart",
                "title": "重启顺序",
                "summary": "Gateway 恢复后重新完成 2FA",
                "tone": "accent",
                "lines": [
                    "1. stop ibkr-runtime，避免 runtime 在旧 Gateway 模式下继续操作。",
                    "2. restart ibkr-gateway，使 IBC 读取目标登录配置。",
                    "3. restart ibkr-runtime。",
                    "4. restart ibkr-compute。",
                    "5. restart ibkr-scheduler。",
                    "6. 延迟 schedule restart ibkr-api，让当前请求先返回。",
                    "ibkr-gateway 或 ibkr-runtime 重启失败属于 hard failure。",
                ],
            },
        ],
        "sections": [
            {
                "title": "操作验收",
                "copy": "成功切换并不等于已恢复交易，后续必须观察 Gateway / Runtime / 2FA 状态。",
                "lines": [
                    "成功响应为 202 restart_requested；next_step 会提示等待目标模式恢复并完成 2FA。",
                    "失败响应会保留 blockers / env_updates / ibc_config_update / restart_results，便于人工恢复。",
                    "所有切换事件会写入 system_events，来源为 broker_mode_switch。",
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
            "order_flow": _order_flow_panel(environment),
            "orders": _orders_panel(environment),
            "broker_mode_switch": _broker_mode_switch_panel(environment),
            "quality": _quality_panel(environment),
            "backtest_validation": _backtest_validation_panel(environment),
            "source_refs": _source_refs(),
            "logic_coverage": _build_logic_coverage(section_ids),
        }
    )

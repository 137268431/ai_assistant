from __future__ import annotations

from typing import Any, Callable

NormalizeEnvironment = Callable[[Any, str], str]
TimeStrings = Callable[[], dict[str, str]]
BuildTodayTargetsResponse = Callable[..., tuple[dict[str, Any], int]]
BuildSystemSummaryPayload = Callable[..., dict[str, Any]]
BuildSystemMonitorPayload = Callable[[str], dict[str, Any]]
FeishuSendInteractive = Callable[[dict[str, Any], str, str], dict[str, Any]]
WriteSystemEventRecord = Callable[..., dict[str, Any]]
GetStatePayload = Callable[[str, str], dict[str, Any]]
UpsertState = Callable[[str, str, dict[str, Any], str], dict[str, Any]]
ConfigValue = Callable[[str, str, str], str]
ConsoleBaseUrl = Callable[[], str]
SignalChatId = Callable[[str], str]
StartupChatId = Callable[[str], str]
LoadMarketSnapshots = Callable[[str, list[str], str, int], list[dict[str, Any]]]


def build_system_scan_summary_response(
    *,
    payload: dict[str, Any] | None,
    normalize_environment: NormalizeEnvironment,
    time_strings: TimeStrings,
    build_today_targets_response: BuildTodayTargetsResponse,
    build_system_summary_payload: BuildSystemSummaryPayload,
    build_system_monitor_payload: BuildSystemMonitorPayload,
    feishu_send_interactive: FeishuSendInteractive,
    write_system_event_record: WriteSystemEventRecord,
    get_state_payload: GetStatePayload,
    upsert_state: UpsertState,
    config_value: ConfigValue,
    console_base_url: ConsoleBaseUrl,
    signal_chat_id: SignalChatId,
    startup_chat_id: StartupChatId,
    load_market_snapshots: LoadMarketSnapshots | None = None,
) -> tuple[dict[str, Any], int]:
    from ibkr_api.system.jobs.open_report import build_system_open_report_response

    # Keep the historical route name, but the scheduled card is now the shared 09:30 open report.
    report, status_code = build_system_open_report_response(
        payload=payload,
        normalize_environment=normalize_environment,
        time_strings=time_strings,
        build_today_targets_response=build_today_targets_response,
        build_system_summary_payload=build_system_summary_payload,
        build_system_monitor_payload=build_system_monitor_payload,
        feishu_send_interactive=feishu_send_interactive,
        write_system_event_record=write_system_event_record,
        get_state_payload=get_state_payload,
        upsert_state=upsert_state,
        config_value=config_value,
        console_base_url=console_base_url,
        startup_chat_id=startup_chat_id,
        load_market_snapshots=load_market_snapshots,
    )
    report["job_id"] = "system_scan_summary"
    return report, status_code


__all__ = ["build_system_scan_summary_response"]

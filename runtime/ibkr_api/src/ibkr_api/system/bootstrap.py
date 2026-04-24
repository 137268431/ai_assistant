from __future__ import annotations

from functools import partial
from typing import Any

from ibkr_api.integrations.feishu import feishu_send_interactive, feishu_suppressed, feishu_token, feishu_update_interactive
from ibkr_api.startup.progress import (
    build_startup_card,
    build_startup_cycle_id,
    build_startup_label,
    deliver_startup_progress_card,
    normalize_startup_state,
    startup_chat_id,
)
from ibkr_api.system.events import (
    build_system_event_card,
    deliver_system_event_notification,
    should_notify_system_event,
    system_event_chat_id,
    write_system_event_record,
)
from ibkr_api.system.runtime_events import build_emit_system_event, build_request_two_factor_approval


def build_system_bootstrap(
    *,
    pb: Any,
    requests_module: Any,
    feishu_token_cache: dict[str, Any],
    default_feishu_app_id: str,
    default_feishu_app_secret: str,
    default_feishu_2fa_chat_id: str,
    default_feishu_alert_chat_id: str,
    default_feishu_system_chat_id: str,
    default_feishu_startup_chat_id: str,
    normalize_environment,
    config_value,
    is_enabled_text,
    add_environment_to_detail,
    time_strings,
    system_page_url,
    label_title_with_environment,
    environment_tag,
    runtime_page_url,
    console_base_url,
) -> dict[str, Any]:
    send_interactive = partial(
        feishu_send_interactive,
        normalize_environment=normalize_environment,
        token_loader=partial(
            feishu_token,
            app_id=default_feishu_app_id,
            app_secret=default_feishu_app_secret,
            requests_module=requests_module,
            cache=feishu_token_cache,
        ),
        requests_module=requests_module,
    )
    update_interactive = partial(
        feishu_update_interactive,
        normalize_environment=normalize_environment,
        token_loader=partial(
            feishu_token,
            app_id=default_feishu_app_id,
            app_secret=default_feishu_app_secret,
            requests_module=requests_module,
            cache=feishu_token_cache,
        ),
        requests_module=requests_module,
    )
    build_system_event_card_fn = partial(
        build_system_event_card,
        normalize_environment=normalize_environment,
        add_environment_to_detail=add_environment_to_detail,
        time_strings=time_strings,
        system_page_url=system_page_url,
        label_title_with_environment=label_title_with_environment,
    )
    should_notify_system_event_fn = partial(
        should_notify_system_event,
        normalize_environment=normalize_environment,
        config_value=config_value,
        is_enabled_text=is_enabled_text,
    )
    system_event_chat_id_fn = partial(
        system_event_chat_id,
        normalize_environment=normalize_environment,
        config_value=config_value,
        default_2fa_chat_id=default_feishu_2fa_chat_id,
        default_alert_chat_id=default_feishu_alert_chat_id,
        default_system_chat_id=default_feishu_system_chat_id,
    )
    deliver_system_event_notification_fn = partial(
        deliver_system_event_notification,
        normalize_environment=normalize_environment,
        should_notify_system_event_fn=should_notify_system_event_fn,
        build_system_event_card_fn=build_system_event_card_fn,
        system_event_chat_id_fn=system_event_chat_id_fn,
        send_interactive=send_interactive,
        update_interactive=update_interactive,
    )
    write_system_event_record_fn = partial(
        write_system_event_record,
        pb=pb,
        normalize_environment=normalize_environment,
        time_strings=time_strings,
        label_title_with_environment=label_title_with_environment,
        add_environment_to_detail=add_environment_to_detail,
    )
    emit_system_event = build_emit_system_event(
        deliver_system_event_notification=deliver_system_event_notification_fn,
        write_system_event_record=write_system_event_record_fn,
    )
    startup_chat_id_fn = partial(
        startup_chat_id,
        config_value=config_value,
        default_chat_id=default_feishu_startup_chat_id,
    )
    build_startup_label_fn = partial(
        build_startup_label,
        normalize_environment=normalize_environment,
        time_strings=time_strings,
    )
    build_startup_cycle_id_fn = partial(
        build_startup_cycle_id,
        normalize_environment=normalize_environment,
    )
    normalize_startup_state_fn = partial(
        normalize_startup_state,
        normalize_environment=normalize_environment,
        startup_chat_id_fn=startup_chat_id_fn,
    )
    build_startup_card_fn = partial(
        build_startup_card,
        normalize_environment=normalize_environment,
        normalize_startup_state_fn=normalize_startup_state_fn,
        environment_tag=environment_tag,
        label_title_with_environment=label_title_with_environment,
        runtime_page_url=runtime_page_url,
        system_page_url=system_page_url,
        console_base_url=console_base_url,
    )
    deliver_startup_progress_card_fn = partial(
        deliver_startup_progress_card,
        normalize_environment=normalize_environment,
        normalize_startup_state_fn=normalize_startup_state_fn,
        build_startup_card_fn=build_startup_card_fn,
        send_interactive=send_interactive,
        update_interactive=update_interactive,
        startup_chat_id_fn=startup_chat_id_fn,
    )
    return {
        "_feishu_suppressed": partial(feishu_suppressed, normalize_environment=normalize_environment),
        "_feishu_send_interactive": send_interactive,
        "_feishu_update_interactive": update_interactive,
        "_deliver_system_event_notification": deliver_system_event_notification_fn,
        "_write_system_event_record": write_system_event_record_fn,
        "_emit_system_event": emit_system_event,
        "_request_two_factor_approval": build_request_two_factor_approval(pb=pb),
        "_startup_chat_id": startup_chat_id_fn,
        "_build_startup_label": build_startup_label_fn,
        "_build_startup_cycle_id": build_startup_cycle_id_fn,
        "_normalize_startup_state": normalize_startup_state_fn,
        "_deliver_startup_progress_card": deliver_startup_progress_card_fn,
    }


__all__ = ["build_system_bootstrap"]

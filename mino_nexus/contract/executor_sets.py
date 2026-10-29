"""执行器集合：本地执行器与子能力展开表，catalog 与资源侧共用。"""
from __future__ import annotations

LOCAL_EXECUTORS = {
    'assert_visual': 'vlm', 'persona_subtask': 'ai_persona',
    'human_input_text': 'hitl',
    # Flow-block program hook: observes the login form locally in prep/do.
    # It is not the check-only assert_visual capability (on any platform).
    'confirm_login_state': 'internal',
    'note': 'internal',
    'sample': 'internal',
}


CHILDREN = {
    'input_text': ('tap_element',),
    'confirm_login_state': ('read_web_auth', 'hierarchy', 'screenshot'),
    'get_otp': ('plugin.gmail.fetch_otp',),
    'fsm_navigate': ('tap_element', 'press_key'),
    'operation.check_box': ('tap_element', 'hierarchy', 'screenshot'),
    'accept_legal_consent': ('tap_element', 'press_key'), 'dismiss_ime': ('press_key',),
    'request_sms_code': ('tap_element', 'press_key'),
    'wait_screen_ready': ('probe_device_state', 'get_foreground_app'),
}

"""IM 入站归一化。平台能力需与适配器实测能力相交。"""
from __future__ import annotations
import hashlib
import json
from typing import Any

EVENTS = {'message', 'button', 'edit', 'retract', 'attachment'}


def normalize(raw: dict[str, Any]) -> dict[str, Any]:
    out = dict(raw)
    out['event_type'] = str(raw.get('event_type') or 'message')
    if out['event_type'] not in EVENTS:
        raise ValueError('不支持的消息事件')
    for key in ('channel', 'instance_id', 'tenant', 'chat_id', 'sender_id', 'msg_id', 'thread_id', 'reply_to', 'target_message_id'):
        out[key] = str(raw.get(key) or '')[:256]
    if not all(out[key] for key in ('channel', 'instance_id', 'chat_id', 'sender_id', 'msg_id')):
        raise ValueError('消息缺少可信路由标识')
    out['chat_type'] = 'group' if raw.get('chat_type') == 'group' else 'private'
    out['text'] = str(raw.get('text') or '')[:12000]
    out['attachments'] = [dict(row) for row in raw.get('attachments') or [] if isinstance(row, dict)][:4]
    out['capabilities'] = capabilities(raw.get('capabilities'))
    return out


def lane_for(user_id: str, origin: dict[str, Any]) -> str:
    parts = [user_id] + [str(origin.get(key) or '') for key in
                       ('channel', 'instance_id', 'tenant', 'chat_id', 'sender_id', 'thread_id')]
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()


def capabilities(declared: Any = None) -> dict[str, Any]:
    declared = declared if isinstance(declared, dict) else {}
    native = declared.get('version') == 2 and declared.get('cards') is True
    return {'markdown': False, 'cards': native, 'buttons': False, 'streaming': native and declared.get('streaming') is True,
            'threads': native and declared.get('threads') is True, 'max_characters': max(256, min(int(declared.get('max_characters') or 1800), 12000)),
            'length_unit': 'utf16' if declared.get('length_unit') == 'utf16' else 'characters',
            'attachment_types': [value for value in declared.get('attachment_types') or [] if value in ('image', 'audio')],
            'version': 1}

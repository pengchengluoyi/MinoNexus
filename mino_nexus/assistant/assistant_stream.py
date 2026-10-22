"""模型内容的草稿流；工具调用完整解析后才允许执行。"""
from __future__ import annotations
import time
from mino_nexus.assistant.assistant_envelope import capabilities
from mino_nexus.assistant import assistant_conversation as conversation

class DraftStream:
    def __init__(self, inbound: dict, origin: dict, generation: int, group: dict | None):
        self.inbound, self.origin, self.generation = inbound, origin, generation
        self.enabled = origin.get('channel') == 'feishu' and capabilities(inbound.get('capabilities')).get('streaming') and (group is None or group.get('disclose_results'))
        self.active = False
        self.sequence = 0
        self.last_at = 0.0
        self.failed = False
        self.key = 'draft-' + str(inbound.get('delivery_key') or inbound['msg_id'])

    def _send(self, text: str, *, complete: bool = False, card: dict | None = None) -> dict:
        from mino_nexus.resources.node_plugin_call import call_plugin
        from mino_nexus.assistant.store import require_im_binding
        conversation.require_generation(self.origin, self.generation)
        require_im_binding(conversation.require_user_id(), self.origin['channel'], self.origin['node_id'], self.origin)
        if self.origin.get('chat_type') == 'group':
            policy = conversation.group_policy(self.origin)
            if not policy.get('enabled') or not policy.get('disclose_results'):
                self.enabled = False
                return {}
        self.sequence += 1
        result = call_plugin(kind='im', plugin_id='langbot', node_id=self.origin['node_id'], capability_id='plugin.im.stream',
                             params={**self.origin, 'reply_ctx': self.inbound.get('reply_ctx') or {}, 'stream_id': self.key,
                                     'sequence': self.sequence, 'text': text, 'complete': complete, 'card': card or {}}, timeout=25)
        self.active = True
        self.last_at = time.time()
        return result

    def accept(self, text: str) -> None:
        if not self.enabled or self.failed or len(text) < 40 or time.time() - self.last_at < 1.2:
            return
        try:
            self._send(text[:1800])
        except PermissionError:
            raise
        except Exception:
            self.failed = True

    def finish(self, text: str, card: dict | None) -> dict | None:
        if not self.active or self.failed or not self.enabled:
            return None
        try:
            return self._send(text, complete=True, card=card)
        except PermissionError:
            raise
        except Exception:
            self.failed = True
            return None

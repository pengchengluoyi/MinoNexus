"""三类业务卡片与完整文本降级；先结果、再问题和解决办法。"""
from __future__ import annotations
import re
import unicodedata
from typing import Any
from mino_nexus.ai.prompt_repo import get_card_template

TOKEN = re.compile(r'\{([^{}]+)\}')


def _fill(text: str, bag: dict) -> str:
    def replace(match):
        value: Any = bag
        for part in match.group(1).split('.'):
            if not isinstance(value, dict) or part not in value:
                return match.group(0)
            value = value[part]
        return str(value) if value is not None else ''
    return TOKEN.sub(replace, str(text or ''))


def build_card(*, template_id: str, title: str, summary: str, data: Any = None, intent: str = '',
               steps: list | None = None, status: str = 'done', actions: list | None = None,
               card_type: str = 'notification', problem: str = '', suggestions: list[str] | None = None) -> dict:
    if card_type not in ('clarification', 'notification', 'dialogue'):
        raise ValueError('未知卡片类型')
    spec = get_card_template(template_id) or {}
    bag = {'title': title, 'summary': summary, 'intent': intent}
    if isinstance(data, (str, int)):
        bag['task_id'] = str(data)
    elif isinstance(data, dict):
        bag.update(data)
        bag['task_id'] = data.get('task_id') or data.get('run_id') or data.get('id') or ''
    # 执行受理和等待输入使用事实状态，不能由模板覆盖成“已开始/已完成”。
    header = title if template_id == 'answer' or status in ('need_input', 'submitted', 'blocked') else _fill(spec.get('title') or title, bag)
    tone = 'warn' if status in ('need_input', 'blocked', 'uncertain') else 'danger' if status == 'failed' else 'info' if status == 'submitted' or card_type == 'dialogue' else str(spec.get('tone') or 'info')
    blocks = [{'type': 'summary', 'text': _fill(spec.get('summary') or '{summary}', bag) or summary}]
    if problem:
        blocks.append({'type': 'problem', 'text': problem})
    if suggestions:
        blocks.append({'type': 'next_steps', 'items': suggestions})
    items = []
    for pair in spec.get('kv') or []:
        if isinstance(pair, list) and len(pair) == 2:
            value = _fill(str(pair[1]), bag)
            if value and not TOKEN.search(value):
                items.append([_fill(str(pair[0]), bag), value])
    if items:
        blocks.append({'type': 'kv', 'items': items})
    if intent:
        blocks.append({'type': 'intent', 'text': intent})
    if steps:
        blocks.append({'type': 'trace', 'steps': steps})
    return {'version': 1, 'template': template_id, 'card_type': card_type, 'status': status,
            'header': {'title': header, 'tone': tone}, 'blocks': blocks, 'actions': actions or []}


def render_wechat_text(card: dict) -> str:
    return render_text(card)


def render_text(card: dict, *, markdown: bool = False) -> str:
    title = str((card.get('header') or {}).get('title') or '')
    sections = [f'**{title}**' if markdown else title] if title else []
    for block in card.get('blocks') or []:
        kind = block.get('type')
        if kind in ('summary', 'problem', 'intent') and block.get('text'):
            label = {'problem': '问题', 'intent': '处理目标'}.get(kind)
            sections.append((label + '：\n' if label else '') + str(block['text']))
        elif kind == 'next_steps' and block.get('items'):
            sections.append('接下来：\n' + '\n'.join(f'{index}. {value}' for index, value in enumerate(block.get('items') or [], 1)))
        elif kind == 'choices' and block.get('items'):
            sections.append('可选项：\n' + '\n'.join(f'{index}. {item["label"]}' for index, item in enumerate(block.get('items') or [], 1)))
        elif kind == 'kv':
            sections.append('\n'.join(f'{key}：{value}' for key, value in block.get('items') or []))
        elif kind == 'case_results':
            for item in block.get('items') or []:
                heading = f'{item.get("number", "")}. {item.get("name", "未命名")}：{item.get("status_label", "待确认")}'.lstrip('. ')
                sections.append(('**' + heading + '**' if markdown else heading) +
                                ('\n' + str(item['summary']) if item.get('summary') else '') +
                                ('\n建议：' + str(item['advice']) if item.get('advice') else ''))
        elif kind == 'trace':
            sections.append('处理过程：\n' + '\n'.join(f'{index}. {step.get("label") or step.get("tool")}（' +
                {'ok': '完成', 'ask': '等待补充', 'fail': '未完成', 'error': '未完成'}.get(step.get('status'), '未知') + '）'
                for index, step in enumerate(block.get('steps') or [], 1)))
    for action in card.get('actions') or []:
        url = str(action.get('url') or '')
        if re.match(r'^https?://', url):
            sections.append(str(action.get('label') or '查看详情') + '：\n' + url)
    text = '\n\n'.join(section for section in sections if section)
    if not markdown:
        text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)
        text = re.sub(r'`([^`]+)`', r'\1', text)
        text = re.sub(r'^#{1,6}\s+', '', text, flags=re.M)
    return text


def _graphemes(text: str) -> list[str]:
    groups: list[str] = []
    regional_count = 0
    for char in text:
        regional = 0x1F1E6 <= ord(char) <= 0x1F1FF
        follows = bool(groups) and (unicodedata.combining(char) or char in ('\ufe0f', '\ufe0e', '\u200d') or
                  groups[-1].endswith('\u200d') or 0x1F3FB <= ord(char) <= 0x1F3FF or (regional and regional_count % 2 == 1))
        if follows:
            groups[-1] += char
        else:
            groups.append(char)
        regional_count = regional_count + 1 if regional else 0
    return groups


def split_text(text: str, limit: int = 1800, unit: str = 'characters') -> list[str]:
    measure = lambda value: len(value.encode('utf-16-le')) // 2 if unit == 'utf16' else len(value)
    if limit < 64:
        raise ValueError('渠道文本长度限制过小')
    if measure(text) <= limit:
        return [text] if text else []
    budget, chunks, current, size = limit - 32, [], [], 0
    for group in _graphemes(text):
        width = measure(group)
        if width > budget:
            raise ValueError('单个字符组合超过渠道消息长度限制')
        if size + width > budget:
            chunks.append(''.join(current))
            current, size = [], 0
        current.append(group)
        size += width
    if current:
        chunks.append(''.join(current))
    return [f'（{index}/{len(chunks)}）\n{chunk}' for index, chunk in enumerate(chunks, 1)]

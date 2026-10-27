"""入口文本归一化：只做确定性的清洗，不调用模型。

用例原文里的写法差异很大：全角/半角标点、中英文引号、`1.` 编号、
尾部括注、`点一下/轻触/点击`、`进入/跳转/打开`。这里把它们收成同一种
形状，并把“依据哪条规则抽出来的”一起返回，后续存储和排查都能看到
`rule_id`，而不是只看到一条孤零零的入口。
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any

_TAP_VERBS = ("点击", "轻触", "点一下", "点按", "单击", "触碰", "点开", "点击一下")
_ENTER_VERBS = ("进入", "跳转", "打开", "前往", "切换到", "切到", "去到", "回到", "返回")
_QUOTES = "“”‘’「」『』\"'`"
_LEADING_NUM = re.compile(r"^\s*(?:步骤\s*)?\d+\s*[.、)）:：]\s*")
_TRAILING_PAREN = re.compile(r"[（(][^（）()]{0,40}[）)]\s*$")
_PUNCT = str.maketrans({
    "，": ",", "。": ".", "；": ";", "：": ":", "、": ",",
    "！": "!", "？": "?", "（": "(", "）": ")", "【": "[", "】": "]",
    "《": "<", "》": ">", "　": " ",
})
_QUOTE_MAP = str.maketrans({ch: '"' for ch in _QUOTES})


def norm_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = text.translate(_QUOTE_MAP).translate(_PUNCT)
    text = re.sub(r"[\u200b-\u200f\u2060\ufeff]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def norm_key(value: Any) -> str:
    return re.sub(r"[\s\"'`.,;:!?()\[\]<>/\\|_-]+", "", norm_text(value)).lower()


def strip_leading_number(value: Any) -> str:
    text = norm_text(value)
    return _LEADING_NUM.sub("", text).strip()


def strip_trailing_paren(value: Any) -> str:
    text = norm_text(value)
    previous = ""
    while text != previous:
        previous = text
        text = _TRAILING_PAREN.sub("", text).strip()
    return text


def strip_quotes(value: Any) -> str:
    return norm_text(value).strip(" \"'`[]()<>")


def _verb_split(text: str, verbs: tuple[str, ...]) -> tuple[str, str] | None:
    """取句子里最早出现的动词；同位置取更长词，避免“打开”压过“点击”。"""
    best: tuple[int, int, str] | None = None
    for verb in verbs:
        idx = text.find(verb)
        if idx < 0:
            continue
        cand = (idx, -len(verb), verb)
        if best is None or cand < best:
            best = cand
    if best is None:
        return None
    idx, _, verb = best
    return verb, text[idx + len(verb):]


def _clean_target(value: str) -> str:
    target = strip_quotes(value)
    # 用例里的目标页常带括号说明或“即可/并打开…”后缀；页名只取前半段。
    target = re.split(r"[（(]", target, maxsplit=1)[0]
    target = re.split(r"(?:并|后|然后|即可|，|,|。|\.|；|;|：|:|！|!|？|\?)", target, maxsplit=1)[0]
    target = re.sub(r"^(?:到|至|了|该|这个|那个|对应的)", "", target).strip()
    target = re.sub(r"(?:按钮|选项|入口|链接|图标|标签页?|tab|Tab|TAB)$", "", target).strip()
    if norm_key(target) in {"", "app", "应用", "页面", "界面"}:
        return ""
    return target[:32].strip()


def _clean_trigger(value: str) -> str:
    # 入口文案是控件原文，不能按页名规则去掉尾字。“做成真的”不能变成“做成真”。
    text = strip_quotes(value)
    text = re.split(r"[（(]", text, maxsplit=1)[0]
    text = re.split(r"(?:按钮|选项|入口|链接|图标|标签页?|tab|Tab|TAB)", text, maxsplit=1)[0].strip()
    text = re.sub(r"^(?:底部|顶部|页面底部|页面顶部|屏幕底部|屏幕顶部|左上角|右上角|左下角|右下角)\s*", "", text)
    text = re.sub(r"(?:一次|两次|一下|后|之后)$", "", text).strip()
    return text.strip(" \"'`")


def _take_object(value: str) -> str:
    """取动词后的对象：优先取成对引号里的原文，否则取到动作/标点为止。"""
    text = str(value or "").lstrip()
    if text.startswith('"'):
        end = text.find('"', 1)
        if end > 0:
            return text[1:end].strip()
    stop = len(text)
    for sep in (
        "进入", "跳转", "打开", "前往", "切换到", "切到", "去到",
        "按钮", "选项", "入口", "链接", "图标", "标签",
        ",", ".", ";", "然后", "并", "一次", "两次", "一下",
    ):
        pos = text.find(sep)
        if 0 <= pos < stop:
            stop = pos
    return text[:stop].strip()


def _looks_like_page(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    if len(text) > 40:
        return False
    generic = {"app", "应用", "软件", "程序", "页面", "界面", "模块", "功能", "系统"}
    if norm_key(text) in generic:
        return False
    return True


def parse_step(raw: Any) -> dict[str, Any]:
    """把一条用例步骤拆成动作/对象/目标页，并返回规则来源。

    返回字段：
    - `text`：归一化后的整句；
    - `trigger`：可点击的入口文案（没有就是空串）；
    - `target`：要进入的页面名（没有就是空串）；
    - `action`：tap/enter/tap_enter/other；
    - `rule_id` / `confidence` / `warnings` / `source_quote`。
    """
    source = str(raw or "").strip()
    text = strip_trailing_paren(strip_leading_number(source))
    warnings: list[str] = []
    trigger = ""
    target = ""
    action = "other"
    rule_id = "case.step.none.v1"
    confidence = 0.0

    # 去掉“在 X 页”这种前置定位，保留 from_hint，避免把“在首页”当成目标。
    from_hint = ""
    m_from = re.match(
        r"^(?:在|从)\s*([^,，]{1,24}?(?:页面|页|界面))(?:\s*(?:上|里|中))?\s*(?=点击|轻触|点一下|点按|单击|输入|等待|$)",
        text,
    )
    if m_from and any(v in text for v in _TAP_VERBS):
        candidate = strip_quotes(m_from.group(1))
        if candidate and not re.search(r"(?:点击|轻触|点一下|输入|等待)", candidate):
            from_hint = candidate

    # “点击 X 进入 Y” / “点击 X，跳转到 Y”
    tap = _verb_split(text, _TAP_VERBS)
    enter = _verb_split(text, _ENTER_VERBS)
    if tap:
        action = "tap"
        after_tap = tap[1].lstrip()
        trigger = _take_object(after_tap)
        trigger = _clean_trigger(trigger)
        if enter and (enter[0] in ("进入", "跳转", "打开", "前往", "切换到", "切到", "去到")):
            # 有“点击 X 进入 Y”时，目标取 enter 之后的部分。
            target = _clean_target(enter[1])
            if target:
                action = "tap_enter"
                rule_id = "case.step.tap_to_enter.v1"
                confidence = 0.84
        if trigger and not target:
            rule_id = "case.step.tap_only.v1"
            confidence = 0.46
        if trigger and re.search(r"(?:进入|跳转|打开|前往)[^,。;]{0,12}(?:页|页面|界面)", text):
            # “点击 X 进入商品详情页”：enter 动词被拆在点击对象里时也能取到目标。
            m_page = re.search(r"(?:进入|跳转到?|打开|前往)\s*([^,。;]{1,24}?(?:页|页面|界面))", text)
            if m_page:
                target = _clean_target(m_page.group(1))
                action = "tap_enter"
                rule_id = "case.step.tap_to_enter.v1"
                confidence = 0.8
    elif enter and enter[0] in ("进入", "跳转", "打开", "前往", "切换到", "切到", "去到"):
        action = "enter"
        target = _clean_target(enter[1])
        if target:
            rule_id = "case.step.enter_target.v1"
            confidence = 0.62

    # 目标页后缀清理：“进入商品详情页”保留“商品详情页”，后续解析时再去后缀。
    target = _clean_target(target)
    if target and not _looks_like_page(target):
        warnings.append("target_too_generic")
        target = ""
    if trigger and trigger == target:
        trigger = ""
    if not trigger and not target:
        rule_id = "case.step.none.v1"
        confidence = 0.0

    return {
        "raw": source,
        "text": text,
        "from_hint": from_hint,
        "trigger": trigger,
        "target": target,
        "action": action,
        "rule_id": rule_id,
        "confidence": confidence,
        "warnings": warnings,
        "source_quote": source,
    }


def labels_for_page(value: Any) -> list[str]:
    """页面名的常见别名：去掉“页面/页/界面”后缀，保留原词。"""
    text = strip_quotes(strip_trailing_paren(value))
    text = re.sub(r"^(?:进入|跳转到?|打开|前往|切换到?|去往?)", "", text).strip()
    out: list[str] = []
    for item in (text, re.sub(r"(?:页面|页|界面)$", "", text).strip(), re.sub(r"(?:tab|标签页)$", "", text, flags=re.I).strip()):
        if item and item not in out:
            out.append(item)
    return out


def parse_numbered_items(values: Any) -> list[tuple[int, str]]:
    """把 `["1. ...", "2. ..."]` 或带编号的整段文本拆成 (序号, 正文)。"""
    items: list[tuple[int, str]] = []
    if isinstance(values, str):
        values = [values]
    for raw in values or []:
        if isinstance(raw, dict):
            raw = raw.get("text") or raw.get("raw") or ""
        for line in str(raw or "").splitlines():
            line = line.strip()
            if not line:
                continue
            m = re.match(r"^\s*(?:步骤\s*)?(\d+)\s*[.、)）:：]\s*(.*)$", line)
            if m:
                items.append((int(m.group(1)), m.group(2).strip()))
            else:
                items.append((len(items) + 1, line))
    return items

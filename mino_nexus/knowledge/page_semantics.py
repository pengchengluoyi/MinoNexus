"""页面/控件语义边界。

用例原文不是页面目录，也不是控件目录。这里只做确定性的中文短语归类：
- 页面目标：短名词（商品详情页、帖子详情页、海报页）；
- 页面状态：同一页面上的业务状态（新人折扣、退款成功后）；
- 动作：进入/点击/返回/生成/分享/放弃支付；
- 断言：查看/观察/检查/停留；
- 控件：明确可点的元素文案（发起支付、首条帖子、快门）。

不调用模型，不写死某个 App 的文案。调用方拿到 role 后再决定是否建页面、
是否建控件、是否只记入口缺口。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

_PAGE_SUFFIXES = (
    "详情页面", "详情页", "列表页面", "列表页", "结果页面", "结果页",
    "首页", "主页", "页面", "页", "界面", "弹窗", "对话框", "浮层",
)
_PAGE_SUFFIX_RE = re.compile(
    r"(详情页面|详情页|列表页面|列表页|结果页面|结果页|首页|主页|页面|页|界面|弹窗|对话框|浮层)$"
)
_NAV_VERBS = (
    "跳转到", "跳转至", "切换到", "切到", "进入到", "回到", "返回",
    "进入", "打开", "前往", "去到", "落地到", "定位到", "跳转",
)
_CHECK_VERBS = (
    "查看", "检查", "观察", "确认", "验证", "断言", "判断", "核对",
    "确保", "看到", "显示", "展示", "停留", "等待",
)
_ACTION_VERBS = (
    "点击", "轻触", "点一下", "点按", "单击", "触碰", "点开", "输入",
    "选择", "触发", "生成", "分享", "保存", "支付", "授权", "放弃",
    "按下", "拍摄", "拍照", "上传", "下载", "滚动", "滑动", "刷新",
    "返回", "回到", "进入", "打开", "跳转", "前往", "切换",
)
_DATA_PREFIXES = (
    ("最新一条", {"position": "latest", "index": 1}),
    ("最新", {"position": "latest", "index": 1}),
    ("首条", {"position": "first", "index": 1}),
    ("第一条", {"position": "first", "index": 1}),
    ("第二条", {"position": "second", "index": 2}),
    ("第三条", {"position": "third", "index": 3}),
    ("指定一条", {"position": "specified"}),
    ("指定", {"position": "specified"}),
)
_STATE_PREFIXES = (
    "已配置", "未配置", "已开启", "未开启", "已领取", "未领取",
    "已登录", "未登录", "已支付", "未支付", "退款成功后", "退款后",
    "支付成功后", "支付失败后", "成功后", "失败后", "新人", "非新人",
)
_GENERIC = {"页面", "界面", "页", "app", "应用", "软件", "程序", "模块", "功能", "系统"}
_NON_ELEMENT_SUFFIX = (
    "模块", "区域", "部分", "内容", "信息", "状态", "结果", "页面",
    "弹窗", "对话框", "浮层",
)
_FLOW_PUNCT = re.compile(r"[，,；;。]|(?:并|或)")
_TRIM_EDGES = " \t\r\n\"'“”‘’「」『』`[]()（）【】<>《》"


@dataclass(frozen=True)
class ClauseSemantics:
    raw: str
    role: str  # page / nav / action / assertion / flow / unknown
    page_label: str = ""
    variant: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    rule_id: str = "clause.none.v1"
    warnings: tuple[str, ...] = ()

    @property
    def is_page_target(self) -> bool:
        return bool(self.page_label) and self.role in ("page", "nav", "action")


def normalize(value: Any) -> str:
    text = str(value or "").strip().strip(_TRIM_EDGES)
    text = re.sub(r"^\s*(?:步骤\s*)?\d+\s*[.、)）:：]\s*", "", text)
    text = re.sub(r"\s+", " ", text)
    return text


def _key(value: Any) -> str:
    text = normalize(value)
    text = text.replace("详情页面", "详情页").replace("页面", "页")
    return re.sub(r"[\s\"'`.,;:!?()\[\]<>/\\|_-]+", "", text).lower()


def _has_page_suffix(text: str) -> bool:
    return bool(_PAGE_SUFFIX_RE.search(str(text or "").strip()))


def _strip_edges(text: str) -> str:
    value = normalize(text)
    value = re.sub(r"^(?:在|从|到|至|了|该|这个|那个|当前|本|对应的)\s*", "", value)
    value = re.sub(r"(?:后|之后|以后|时|前|之后|中|上|里|里面)$", "", value).strip()
    value = re.sub(r"^(?:在|从|到|至|了|该|这个|那个|当前|本|对应的)\s*", "", value)
    return value.strip()


def _strip_action_prefix(text: str) -> str:
    """去掉“生成分享至微信生成”这类动作前缀，只留页面名词。"""
    value = str(text or "")
    for verb in sorted(_ACTION_VERBS + _NAV_VERBS, key=len, reverse=True):
        if verb in value:
            value = value.rsplit(verb, 1)[-1]
    value = re.sub(r"^(?:自动|直接|正常|成功|授权后|正常|并|然后)+", "", value).strip()
    return value


def _page_suffix(text: str) -> tuple[str, str]:
    m = _PAGE_SUFFIX_RE.search(str(text or "").strip())
    if not m:
        return "", ""
    suffix = m.group(1)
    canonical = {
        "详情页面": "详情页", "列表页面": "列表页", "结果页面": "结果页",
        "页面": "页", "主页": "页",
    }.get(suffix, suffix)
    return suffix, canonical


def _canonical_suffix(label: str) -> str:
    text = str(label or "").strip()
    for old, new in (
        ("详情页面", "详情页"), ("列表页面", "列表页"), ("结果页面", "结果页"),
        ("页面", "页"), ("主页", "页"),
    ):
        if text.endswith(old):
            return text[: -len(old)] + new
    return text


def _known_page_labels(labels: Iterable[str]) -> list[str]:
    out: list[str] = []
    for raw in labels or []:
        text = normalize(raw)
        if not (2 <= len(text) <= 18):
            continue
        if text in _GENERIC or text.lower() in {"edittext", "imageview", "textview"}:
            continue
        if re.search(r"[，,；;。:：]", text):
            continue
        if text.startswith(_CHECK_VERBS) or text.startswith(_ACTION_VERBS):
            continue
        if any(text.endswith(suffix) for suffix in _NON_ELEMENT_SUFFIX):
            # 页面标题允许“页面”，但不能把模块/状态当页面别名。
            if not _has_page_suffix(text):
                continue
        out.append(_canonical_suffix(text))
    return list(dict.fromkeys(out))


def _match_known(text: str, known: list[str]) -> str:
    if not text or not known:
        return ""
    key = _key(text)
    hits = [label for label in known if _key(label) and _key(label) in key]
    if not hits:
        return ""
    hits.sort(key=lambda item: (len(_key(item)), len(item)), reverse=True)
    # 只接受唯一最长匹配；同样长的两个候选说明有歧义。
    best = hits[0]
    if len(hits) > 1 and len(_key(hits[1])) == len(_key(best)):
        return ""
    return best


def _page_mention(text: str, known: list[str]) -> str:
    matched = _match_known(text, known)
    if matched:
        return matched
    value = normalize(text)
    # 只从页面名词后缀往前取，不带“查看/观察/进入”等动词。
    for m in re.finditer(
        r"([\u4e00-\u9fa5A-Za-z0-9_\-]{1,12}(?:详情页面|详情页|列表页面|列表页|结果页面|结果页|首页|主页|页面|页|界面|弹窗|对话框|浮层))",
        value,
    ):
        candidate = m.group(1)
        candidate = _strip_action_prefix(candidate)
        candidate = _strip_edges(candidate)
        if candidate and candidate not in _GENERIC:
            return _canonical_suffix(candidate)
    return ""


def _qualifier(text: str, page_label: str) -> str:
    raw = normalize(text)
    key = _key(page_label)
    if not key:
        return ""
    # 从原文里去掉页面名，剩下的前缀就是状态/动作线索。
    for label in (page_label, page_label.replace("页", "页面"), page_label.replace("页", "界面")):
        if label and label in raw:
            prefix = raw.split(label, 1)[0]
            break
    else:
        prefix = ""
    prefix = re.sub(r"^(?:进入|打开|跳转至|跳转到|前往|回到|返回|点击|选择|触发|生成|分享至|分享|保存|支付|授权|放弃|在|从|到|至|了|该|这个|那个|当前|本)+", "", prefix).strip()
    prefix = re.sub(r"(?:后|之后|以后|时|前)$", "", prefix).strip()
    return prefix.strip(" 的，,；;。:：")


def _variant_from_qualifier(qualifier: str) -> str:
    text = str(qualifier or "").strip()
    if not text:
        return ""
    for prefix in _STATE_PREFIXES:
        if text.startswith(prefix):
            rest = text[len(prefix):].strip(" 的")
            if prefix in ("退款成功后", "退款后"):
                return "退款成功后"
            if prefix in ("支付成功后", "成功后"):
                return "支付成功后"
            if prefix in ("支付失败后", "失败后"):
                return "支付失败后"
            if rest and len(rest) <= 16:
                return rest
            return prefix
    return ""


def _data_from_text(text: str) -> tuple[dict[str, Any], str]:
    raw = normalize(text)
    for prefix, data in _DATA_PREFIXES:
        if prefix in raw:
            return dict(data), prefix
    return {}, ""


def _starts_with_any(text: str, values: Iterable[str]) -> bool:
    return any(str(text or "").startswith(value) for value in values)


def _role(text: str, page_label: str) -> str:
    raw = normalize(text)
    if not raw:
        return "unknown"
    if _starts_with_any(raw, _CHECK_VERBS) or re.search(r"(?:状态|展示|显示|提示)$", raw):
        return "assertion"
    # 导航优先于“支付/授权/点击”等动作前缀：
    # “支付成功后返回商品详情页”“授权后正常进入相机页面”仍然是导航。
    if page_label and any(verb in raw for verb in ("进入", "跳转到", "跳转至", "回到", "返回", "打开", "前往")):
        return "nav"
    if _starts_with_any(raw, _ACTION_VERBS):
        return "action" if page_label or _FLOW_PUNCT.search(raw) is None else "flow"
    # “在相机页面按下快门拍照”：页面名词只是定位，动作在页面名词之后。
    if page_label and any(verb in raw for verb in _ACTION_VERBS if verb not in _NAV_VERBS):
        return "action"
    if page_label:
        return "page"
    if _FLOW_PUNCT.search(raw):
        return "flow"
    return "unknown"


def analyze_clause(raw: Any, *, known_labels: Iterable[str] = ()) -> ClauseSemantics:
    text = normalize(raw)
    if not text:
        return ClauseSemantics(raw="", role="unknown")
    known = _known_page_labels(known_labels)
    matched = _match_known(text, known)
    label = matched or ""
    suffix, canonical_suffix = _page_suffix(text)
    if not label and suffix:
        body = text[: -len(suffix)]
        body = _strip_edges(body)
        body = _strip_action_prefix(body)
        if "的" in body:
            body = body.rsplit("的", 1)[-1]
        body = re.sub(r"^(?:该|这个|那个|当前|本|对应的|首条|第一条|第二条|最新)+", "", body).strip()
        if body and len(body) <= 14 and not _starts_with_any(body, _CHECK_VERBS):
            label = _canonical_suffix(body + suffix)
    if not label:
        label = _page_mention(text, known)
    data, data_prefix = _data_from_text(text)
    qualifier = _qualifier(text, label) if label else ""
    if data_prefix:
        qualifier = re.sub(re.escape(data_prefix), "", qualifier).strip(" 的")
    role = _role(text, label)
    variant = _variant_from_qualifier(qualifier) if qualifier and role in ("page", "nav") else ""
    if role == "nav" and not label:
        role = "action"
    if role == "page" and not label:
        role = "unknown"
    warnings: list[str] = []
    if len(text) > 40:
        warnings.append("clause_too_long")
    if label and len(label) > 18:
        warnings.append("page_label_too_long")
    confidence = 0.0
    if matched:
        confidence = 0.9
    elif label and suffix:
        confidence = 0.78
    elif label:
        confidence = 0.6
    elif role in ("action", "assertion", "flow"):
        confidence = 0.55
    rule = "clause.known_page.v1" if matched else "clause.page_suffix.v1" if label and suffix else "clause.page_mention.v1" if label else "clause.non_page.v1"
    return ClauseSemantics(
        raw=text, role=role, page_label=label, variant=variant, data=data,
        confidence=confidence, rule_id=rule, warnings=tuple(warnings),
    )


def is_page_label(value: Any) -> bool:
    text = normalize(value)
    return bool(text) and len(text) <= 18 and _has_page_suffix(text) and _key(text) not in _GENERIC


def canonical_page_label(value: Any, *, known_labels: Iterable[str] = ()) -> str:
    return analyze_clause(value, known_labels=known_labels).page_label


def control_semantic_role(value: Any, *, explicit: bool = False) -> str:
    """控件文案归类：element / action / assertion / page / unknown。

    explicit=True 表示来自解析器的 target.name，不是整句兜底；仍然拒绝
    页面名和动作/断言，避免把“进入相机页面”当成元素。
    """
    text = normalize(value)
    if not text:
        return "unknown"
    sem = analyze_clause(text)
    if sem.role in ("nav", "flow", "action"):
        return "action"
    if sem.role == "assertion":
        return "assertion"
    if sem.role == "page":
        return "page" if is_page_label(text) else "unknown"
    if any(text.endswith(suffix) for suffix in _NON_ELEMENT_SUFFIX):
        return "unknown"
    if len(text) > 32 or re.search(r"[，,；;。]", text):
        return "action"
    if _starts_with_any(text, _CHECK_VERBS):
        return "assertion"
    if _starts_with_any(text, _ACTION_VERBS):
        return "action"
    if not explicit and len(text) > 18:
        return "unknown"
    return "element"


def is_element_label(value: Any, *, explicit: bool = False) -> bool:
    return control_semantic_role(value, explicit=explicit) == "element"


def page_key(value: Any) -> str:
    """页面分组键：状态变体归到同一页面。"""
    text = _canonical_suffix(normalize(value))
    return _key(text) or text

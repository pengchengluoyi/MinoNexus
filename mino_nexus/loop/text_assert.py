"""多语言文案的有界 DOM 证据和逐项校验；不读取设备，不整树发送。"""
from __future__ import annotations

from typing import Any

from mino_nexus.ai.schemas import AssertResult
from mino_nexus.knowledge.checkpoint_text_policy import expected_text, language_matches, text_policy

_MAX_NODES = 32
_MAX_CHARS = 6000
_MAX_NODE_CHARS = 800
_MAX_EVIDENCE_CHARS = _MAX_CHARS + _MAX_NODES - 1
_SEMANTIC_KEYS = ("subject", "negation", "quantities", "conditions")


def text_evidence_schema() -> dict[str, Any]:
    return {
        "text_match": {"type": "string", "enum": ["exact", "intent"]},
        "observable": {"type": "boolean"},
        "evidence_scope": {"type": "string", "enum": ["visible_full", "unknown"]},
        "observed_text": {"type": "string", "maxLength": _MAX_EVIDENCE_CHARS},
        "observed_language": {"type": "string"},
        "node_refs": {"type": "array", "items": {"type": "string"}, "uniqueItems": True, "maxItems": _MAX_NODES},
        "semantic_checks": {
            "type": "object", "properties": {key: {"type": "boolean"} for key in _SEMANTIC_KEYS},
            "required": list(_SEMANTIC_KEYS), "additionalProperties": False,
        },
    }


class TextEvidenceIssue:
    def __init__(self, reason: str, *, unavailable: bool = False):
        self.reason = reason
        self.unavailable = unavailable


def validate_text_evidence(
    row: dict[str, Any], checkpoint: dict[str, Any], *, observation: dict[str, Any] | None = None,
) -> TextEvidenceIssue | None:
    """返回不可核验原因；可核验的文案/语言不符保留为正常 fail。"""
    policy = text_policy(checkpoint)
    if not expected_text(checkpoint):
        return TextEvidenceIssue("缺少要比较的完整预期文案或意图")
    if row.get("text_match") != policy:
        return TextEvidenceIssue('文案匹配策略与请求不一致')
    if type(row.get("observable")) is not bool:
        return TextEvidenceIssue('缺少文案可观察性')
    if row["observable"] is not True:
        return TextEvidenceIssue('目标文案不可观察', unavailable=True)
    actual = row.get("observed_text")
    language = row.get("observed_language")
    if not isinstance(actual, str) or not actual.strip() or len(actual) > _MAX_EVIDENCE_CHARS:
        return TextEvidenceIssue('缺少完整且有界的实际文案', unavailable=True)
    if not isinstance(language, str) or not language_matches("", language):
        return TextEvidenceIssue('实际文案语言未知', unavailable=True)
    if row.get("evidence_scope") != "visible_full":
        return TextEvidenceIssue('文案观察不完整', unavailable=True)
    if observation is not None:
        facts = {fact["ref"]: fact for fact in observation.get("texts", [])}
        refs = row.get("node_refs")
        if (not isinstance(refs, list) or not refs or len(refs) > _MAX_NODES
                or any(not isinstance(ref, str) or ref not in facts for ref in refs)
                or len(set(refs)) != len(refs)):
            return TextEvidenceIssue('文案引用缺失、重复或超出本检查点的观察范围')
        # 多选项可以跨节点。逐个完整原文按引用顺序换行，不能漏掉限定词、
        # 编造译文或反复引用一个节点凑数量。单节点保持原合同。
        observed = "\n".join(str(facts[ref]["text"]) for ref in refs)
        if actual != observed:
            return TextEvidenceIssue('实际文案不是引用节点完整原文的逐行摘录')
        if row.get("passed") is False and observation.get("incomplete"):
            return TextEvidenceIssue('DOM 候选不完整，不能用未命中证明文案不符', unavailable=True)
    if observation is None and row.get("node_refs") != []:
        return TextEvidenceIssue("截图结果不能编造 DOM 节点引用")
    checks = row.get("semantic_checks")
    if not isinstance(checks, dict) or any(type(checks.get(key)) is not bool for key in _SEMANTIC_KEYS):
        return TextEvidenceIssue('缺少主体、否定、数量或条件的逐项核对')
    if row.get("passed") is True and policy == "intent" and not all(checks[key] for key in _SEMANTIC_KEYS):
        return TextEvidenceIssue('语义约束未全部保持，不能判为意图一致')
    rules = checkpoint.get("rules") if isinstance(checkpoint.get("rules"), dict) else {}
    if policy == "exact":
        want = expected_text(checkpoint)
        if not want:
            return TextEvidenceIssue('指定文案检查缺少预期文字')
        equal = actual.strip() == want if rules.get("match") == "equals" else want in actual
        if not equal:
            row["passed"] = False
            row["evidence"] = "指定文案与实际可见文案不匹配；不能以翻译或语义相近放行"
    if not language_matches(str(rules.get("expected_language") or ""), language):
        row["passed"] = False
        row["evidence"] = "实际界面语言不符合明确指定的语言要求"
    return None


def _bounds(node: dict[str, Any]) -> tuple[float, float, float, float] | None:
    raw = node.get("bounds")
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        return None
    try:
        left, top, right, bottom = (float(value) for value in raw)
    except (TypeError, ValueError):
        return None
    return (left, top, right, bottom) if right > left and bottom > top else None


def dom_text_observation(checkpoint: dict[str, Any], ctx: Any) -> dict[str, Any]:
    from mino_nexus.core.secret_redaction import sanitize
    from mino_nexus.navigation.geometry.hierarchy_slots import node_flag
    nodes = [row for row in (getattr(ctx, "nav_hierarchy_nodes", None) or []) if isinstance(row, dict)]
    contract = dict(getattr(ctx, "observation_contract", None) or {})
    element = checkpoint.get("element") if isinstance(checkpoint.get("element"), dict) else {}
    role = str(element.get("role") or "")
    area = None
    if role == "dialog":
        dialogs = [node for node in nodes if str(node.get("role") or "").lower() in {"dialog", "alertdialog"}
                   and ("visible" not in node or node_flag(node, "visible")) and _bounds(node)]
        if len(dialogs) != 1:
            return {"texts": [], "incomplete": True, "reason": "无法唯一确定弹窗范围"}
        area = _bounds(dialogs[0])
    texts: list[dict[str, Any]] = []
    incomplete = not contract or bool(contract.get("truncated")) or bool(contract.get("missing"))
    chars = 0
    for index, node in enumerate(nodes):
        if (("visible" in node and not node_flag(node, "visible")) or node_flag(node, "sensitive")
                or node_flag(node, "password") or node_flag(node, "editable")):
            continue
        kind = str(node.get("role") or node.get("class") or "").lower()
        if any(token in kind for token in ("input", "edittext", "textbox", "textarea", "password")):
            continue
        bounds = _bounds(node)
        if not bounds:
            continue
        if area and not (bounds[0] >= area[0] and bounds[1] >= area[1] and bounds[2] <= area[2] and bounds[3] <= area[3]):
            continue
        if role not in {"", "page", "dialog", "text"} and role not in kind:
            continue
        # 文案只取实际 text，不能把 accessible description 当作可见文本。
        text = str(node.get("text") or "").strip()
        if not text or sanitize(text) != text:
            continue
        if len(text) > _MAX_NODE_CHARS or len(texts) >= _MAX_NODES or chars + len(text) > _MAX_CHARS:
            incomplete = True
            continue
        texts.append({"ref": f"n{index}", "text": text, "role": kind[:60]})
        chars += len(text)
    return {"texts": texts, "incomplete": incomplete, "chars": chars,
            "snapshot_id": str(contract.get("snapshot_id") or "")[:120]}


def run_dom_text_checks(checkpoints: list[dict[str, Any]], ctx: Any, *, provider_id: str = "") -> AssertResult:
    from mino_nexus.loop.vision_assert import vision_assert_checkpoints
    observations = {row["point_id"]: dom_text_observation(row, ctx) for row in checkpoints}
    # 批次共享一个总预算；不同检查点不能反复发送整页文本。
    shared: dict[str, str] = {}
    total_chars = 0
    for observation in observations.values():
        bounded = []
        for fact in observation["texts"]:
            ref, value = fact["ref"], fact["text"]
            if ref not in shared:
                if len(shared) >= _MAX_NODES or total_chars + len(value) > _MAX_CHARS:
                    observation["incomplete"] = True
                    continue
                shared[ref] = value
                total_chars += len(value)
            bounded.append(fact)
        observation["texts"] = bounded
    available = [row for row in checkpoints if observations[row["point_id"]]["texts"]]
    missing = [row for row in checkpoints if not observations[row["point_id"]]["texts"]]
    if any(row.get("required") is not False for row in missing):
        return AssertResult(passed=False, confidence=0.0, evidence="dom",
                            ai_reasoning="目标范围缺少可用的可见文案，需要截图兜底", error_kind="dom_check_unavailable")
    if available:
        result = vision_assert_checkpoints(
            expectation="", image_base64="", checkpoints=available,
            provider_id=provider_id or None,
            text_observations={row["point_id"]: observations[row["point_id"]] for row in available}, run_context=ctx,
        )
    else:
        result = AssertResult(passed=True, confidence=1.0, evidence="dom", ai_reasoning="无必选文案检查点")
    result.checkpoint_results.extend({"point_id": row["point_id"], "passed": False,
        "evidence": "选填目标缺少可用文案，未核验", "error_kind": "dom_check_unavailable",
        "text_match": text_policy(row), "evidence_scope": "unknown"} for row in missing)
    return result


def checkpoint_report(row: dict[str, Any]) -> dict[str, Any]:
    """session 报告只存白名单证据；沿用统一秘密脱敏。"""
    from mino_nexus.core.secret_redaction import sanitize
    keys = ("point_id", "passed", "evidence", "evidence_scope", "hidden_content_verified", "error_kind",
            "text_match", "observable", "observed_text", "observed_language", "node_refs", "semantic_checks", "observation_ref", "observation_source")
    result = {key: row[key] for key in keys if key in row}
    for key in ("evidence", "observed_text"):
        if key in result:
            result[key] = str(result[key])[:_MAX_EVIDENCE_CHARS if key == "observed_text" else _MAX_NODE_CHARS]
    return sanitize(result)

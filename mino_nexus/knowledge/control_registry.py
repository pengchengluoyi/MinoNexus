"""控件身份。执行渠道和设备渠道决定读哪一格，空了只补这一格。"""
from __future__ import annotations

import re
import uuid

from mino_nexus.core.ids import prefixed
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

from mino_nexus.platform.runtime_mode import _RUNTIME_MODE  # noqa: E402
_COMPILE_INSERTS: ContextVar[list | None] = ContextVar("control_compile_inserts", default=None)


@contextmanager
def capture_compile_controls():
    """Record exact inserted rows so a compile does not invalidate itself.

    This is local to one compiler worker. Changes to existing controls, or
    subsequent edits to an inserted control, still invalidate its dependency.
    """
    rows = []
    token = _COMPILE_INSERTS.set(rows)
    try:
        yield rows
    finally:
        _COMPILE_INSERTS.reset(token)


from mino_nexus.contract.errors import RuntimeReadOnlyError
from mino_nexus.knowledge.page_semantics import analyze_clause, control_semantic_role
from mino_nexus.platform.runtime_mode import runtime_readonly  # noqa: F401


class ControlRegistryReadOnlyError(RuntimeReadOnlyError):
    def __init__(self, reason: str):
        self.reason = reason

    def __str__(self) -> str:
        return f"控件配置只读：{self.reason}"


@contextmanager
def registry_runtime(execution_mode: str = "agent") -> Iterator[None]:
    from mino_nexus.core.protocol import execution_mode as require_execution_mode
    mode = require_execution_mode(execution_mode)
    if runtime_readonly():
        mode = "v5"
    token = _RUNTIME_MODE.set(mode)
    try:
        yield
    finally:
        _RUNTIME_MODE.reset(token)




def _assert_runtime_writable() -> None:
    if runtime_readonly():
        raise ControlRegistryReadOnlyError("v5_runtime")


def _published_typed(row: Any) -> bool:
    return row.publish_state == "published" and bool(row.locator_spec)

_ELEMENT_TYPES = {"button", "input", "checkbox", "dialog", "text", "image", "switch", "page"}
_SCOPES = {"page", "iframe", "dialog", "new_window", "webview", "shadow"}
_ROLE_TYPE = {
    "button": "button",
    "input": "input",
    "checkbox": "checkbox",
    "switch": "switch",
    "image": "image",
    "dialog": "dialog",
    "page": "page",
    "element": "button",
}


def _empty_locator() -> dict[str, Any]:
    return {
        "playwright": "",
        "uiautomator": "",
        "ax": "",
        "uia": "",
        "vlm_text": "",
        "source": {"playwright": "", "uiautomator": "", "ax": "", "uia": "", "vlm_text": ""},
    }


def _desc_key(text: str) -> str:
    return re.sub(r"\s+", "", str(text or "").strip().lower())


def _public(row: Any) -> dict[str, Any]:
    locator = row.locator_value if isinstance(row.locator_value, dict) else {}
    merged = _empty_locator()
    merged.update(locator)
    source = merged.get("source") if isinstance(merged.get("source"), dict) else {}
    merged["source"] = {
        "playwright": str(source.get("playwright") or ""),
        "uiautomator": str(source.get("uiautomator") or ""),
        "ax": str(source.get("ax") or ""),
        "uia": str(source.get("uia") or ""),
        "vlm_text": str(source.get("vlm_text") or ""),
    }
    return {
        "project_id": row.project_id,
        "target_desc": str(merged.get("vlm_text") or row.desc_key or ""),
        "control_id": row.control_id,
        "element_type": row.element_type or "button",
        "scope": row.scope or "page",
        "locator_value": merged,
        "pending_merge": bool(row.pending_merge),
        "revision": int(row.revision or 1),
        "page_state_id": str(merged.get("nav_state_id") or ""),
        "locator_spec": dict(row.locator_spec or {}),
        "sensitive": bool(row.sensitive),
        "publish_state": row.publish_state or "draft",
        "published_by": row.published_by or "",
        "published_at": row.published_at or "",
        "updated_by": row.updated_by or "",
        "updated_at": row.updated_at or "",
        # 只读派生值：历史控件表里混入过动作/断言句，Workbench 用它把
        # 真正的元素和用例句子分开，不改旧记录。
        "semantic_role": control_semantic_role(str(merged.get("vlm_text") or row.desc_key or "")),
    }


def ensure_control(
    project_id: str,
    *,
    target_desc: str,
    element_type: str = "button",
    scope: str = "page",
    vlm_text: str = "",
    page_id: str = "",
    container_id: str = "",
    platform: str = "",
) -> str:
    """有明确页面/容器身份时按其作用域复用；历史无身份的控件保留旧键。"""
    _assert_runtime_writable()
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.ui_control import UiControl

    pid = str(project_id or "").strip() or "_project"
    desc = str(target_desc or vlm_text or "").strip()
    key = _desc_key(desc)
    if not key:
        key = uuid.uuid4().hex[:8]
    kind = _ROLE_TYPE.get(str(element_type or "").strip(), str(element_type or "button"))
    if kind not in _ELEMENT_TYPES:
        kind = "button"
    where = str(scope or "page").strip()
    if where not in _SCOPES:
        where = "page"
    page = str(page_id or "").strip()
    container = str(container_id or "").strip()
    channel = str(platform or "").strip().lower()
    if page or container:
        key = f"{channel}:{page}:{container}:{where}:{kind}:{key}"
    with session_scope() as db:
        row = (
            db.query(UiControl)
            .filter(UiControl.project_id == pid, UiControl.desc_key == key)
            .first()
        )
        if row is None:
            locator = _empty_locator()
            text = str(vlm_text or desc)[:200]
            locator["vlm_text"] = text
            locator["source"]["vlm_text"] = "case"
            row = UiControl(
                control_id=prefixed("ctl", 8),
                project_id=pid,
                desc_key=key,
                element_type=kind,
                scope=where,
                locator_value=locator,
                pending_merge=0,
            )
            db.add(row)
            db.flush()
            captured = _COMPILE_INSERTS.get()
            if captured is not None:
                captured.append({column.name: getattr(row, column.name) for column in UiControl.__table__.columns})
        elif not _published_typed(row) and vlm_text and not str((row.locator_value or {}).get("vlm_text") or "").strip():
            locator = _empty_locator()
            locator.update(row.locator_value or {})
            locator["vlm_text"] = str(vlm_text)[:200]
            source = dict(locator.get("source") or {})
            source["vlm_text"] = "case"
            locator["source"] = source
            changed = (
                db.query(UiControl)
                .filter(
                    UiControl.pk == row.pk,
                    UiControl.revision == row.revision,
                    UiControl.publish_state != "published",
                )
                .update(
                    {UiControl.locator_value: locator, UiControl.revision: UiControl.revision + 1},
                    synchronize_session=False,
                )
            )
            if not changed:
                raise ControlRegistryReadOnlyError("configuration_changed")
        return str(row.control_id)


def _find_control_row(db: Any, pid: str, cid: str) -> Any:
    """All execution modes require exact project ownership."""
    from mino_nexus.models.ui_control import UiControl

    rows = db.query(UiControl).filter(UiControl.project_id == pid, UiControl.control_id == cid).all()
    if len(rows) > 1:
        raise ValueError("control_identity_conflict")
    return rows[0] if rows else None


def get_control(project_id: str, control_id: str) -> dict[str, Any] | None:
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.ui_control import UiControl

    pid = str(project_id or "").strip() or "_project"
    cid = str(control_id or "").strip()
    if not cid:
        return None
    with session_scope() as db:
        row = _find_control_row(db, pid, cid)
        return _public(row) if row else None


def _control_label_token(row: Any) -> str:
    locator = row.locator_value if isinstance(row.locator_value, dict) else {}
    return _desc_key(str((locator or {}).get("vlm_text") or row.desc_key or ""))


def find_control_by_label(project_id: str, label: str) -> dict[str, Any] | None:
    """Unique control whose visible copy equals this label. resource_id is not required."""
    token = _desc_key(label)
    if len(token) < 2:
        return None
    pid = str(project_id or "").strip()
    if not pid:
        return None
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.ui_control import UiControl

    with session_scope() as db:
        hits = [row for row in db.query(UiControl).filter_by(project_id=pid).all() if _control_label_token(row) == token]
        if len(hits) != 1:
            return None
        return _public(hits[0])


def find_control_in_text(project_id: str, text: str) -> dict[str, Any] | None:
    """Unique longest control copy that appears in this clause. Ties are not a match."""
    blob = _desc_key(text)
    if len(blob) < 2:
        return None
    pid = str(project_id or "").strip()
    if not pid:
        return None
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.ui_control import UiControl

    skip = {"页面", "edittext", "button", "input", "checkbox", "switch", "dialog", "image", "text"}
    with session_scope() as db:
        ranked: list[tuple[int, Any]] = []
        for row in db.query(UiControl).filter_by(project_id=pid).all():
            token = _control_label_token(row)
            if len(token) < 2 or token in skip or token not in blob:
                continue
            ranked.append((len(token), row))
        if not ranked:
            return None
        best = max(length for length, _row in ranked)
        top = [row for length, row in ranked if length == best]
        if len(top) != 1:
            return None
        return _public(top[0])


def locator_cell(scheme: str, platform: str) -> str:
    from mino_nexus.core.platform_contract import target_platform
    plat = target_platform(platform)
    if scheme == "visual": return "vlm_text"
    if scheme not in {"dom","dom_preferred"}: raise ValueError("locator_scheme_invalid")
    return {"android": "uiautomator", "ios": "xcuitest", "web": "playwright", "mac": "ax", "win": "uia"}[plat]


def read_locator(control: dict[str, Any] | None, *, scheme: str, platform: str) -> str:
    cell = locator_cell(scheme, platform)
    locator = (control or {}).get("locator_value") if isinstance((control or {}).get("locator_value"), dict) else {}
    return str((locator or {}).get(cell) or "")


def fill_locator(
    project_id: str,
    control_id: str,
    *,
    scheme: str,
    platform: str,
    value: str,
    source: str,
) -> str:
    """只补当前渠道那一格。看图不接受坐标。"""
    _assert_runtime_writable()
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.ui_control import UiControl

    cell = locator_cell(scheme, platform)
    text = str(value or "").strip()
    if not text:
        return cell
    if cell == "vlm_text" and _looks_like_coord(text):
        return cell
    pid = str(project_id or "").strip() or "_project"
    with session_scope() as db:
        row = _find_control_row(db, pid, str(control_id or ""))
        if row is None:
            return cell
        if _published_typed(row):
            raise ControlRegistryReadOnlyError("published_typed_control")
        locator = _empty_locator()
        locator.update(row.locator_value or {})
        if str(locator.get(cell) or "").strip():
            return cell
        locator[cell] = text[:300]
        src = dict(locator.get("source") or {})
        src[cell] = str(source or "case")[:32]
        locator["source"] = src
        changed = (
            db.query(UiControl)
            .filter(
                UiControl.pk == row.pk,
                UiControl.revision == row.revision,
                UiControl.publish_state != "published",
            )
            .update(
                {UiControl.locator_value: locator, UiControl.revision: UiControl.revision + 1},
                synchronize_session=False,
            )
        )
        if not changed:
            raise ControlRegistryReadOnlyError("configuration_changed")
    return cell


def fill_locator_spec_draft(project_id: str, control_id: str, spec: dict[str, Any]) -> None:
    """Write typed locator_spec cells that are still empty. Never publishes."""
    _assert_runtime_writable()
    if not isinstance(spec, dict) or not spec:
        return
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.ui_control import UiControl

    pid = str(project_id or "").strip() or "_project"
    with session_scope() as db:
        row = _find_control_row(db, pid, str(control_id or ""))
        if row is None or _published_typed(row):
            return
        current = dict(row.locator_spec or {}) if isinstance(row.locator_spec, dict) else {}
        changed = False
        for cell, cfg in spec.items():
            if not str(cell or "").strip() or not isinstance(cfg, dict):
                continue
            if isinstance(current.get(cell), dict) and current.get(cell):
                continue
            current[cell] = cfg
            changed = True
        if not changed:
            return
        updated = (
            db.query(UiControl)
            .filter(
                UiControl.pk == row.pk,
                UiControl.revision == row.revision,
                UiControl.publish_state != "published",
            )
            .update(
                {UiControl.locator_spec: current, UiControl.revision: UiControl.revision + 1},
                synchronize_session=False,
            )
        )
        if not updated:
            raise ControlRegistryReadOnlyError("configuration_changed")


_UNIMPLEMENTED = {"select", "upload", "hover"}


def action_type_of(step: dict[str, Any]) -> str:
    cap = str(step.get("hook_cap") or "")
    ref = str(step.get("key_ref") or "")
    clause = str(step.get("source_clause") or step.get("title") or "")
    if "上传" in clause:
        return "upload"
    if "悬停" in clause or "鼠标移" in clause:
        return "hover"
    if "下拉选择" in clause:
        return "select"
    if cap == "input_text" or ref == "operation.input_text":
        return "clear" if "清空" in clause and "输入" not in clause else "input"
    if ref == "operation.check_box":
        return "check"
    if cap == "swipe_direction" or ref == "operation.swipe":
        return "swipe"
    if cap in ("wait_ms", "wait_screen_ready") or ref == "operation.wait_ready":
        return "wait"
    if cap == "reload_page" or ref == "operation.reload_page":
        return "wait"
    if "scroll" in cap or "scroll" in ref:
        return "scroll"
    return "click"


def assert_of(step: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    payload = step.get("assert") if isinstance(step.get("assert"), dict) else {}
    rules = payload.get("rules") if isinstance(payload.get("rules"), dict) else {}
    style = str(rules.get("style") or "")
    if style == "disabled":
        return "enabled", {"rule": "disabled"}
    if style in ("enabled",):
        return "enabled", {"rule": "enabled"}
    if rules.get("max_length"):
        return "value", {"rule": "max_length", "max_length": rules.get("max_length")}
    if rules.get("accepts_special"):
        return "value", {"rule": "equals"}
    text = str(rules.get("title") or rules.get("body") or rules.get("text") or "").strip()
    if text:
        return "text", {"rule": "contains", "text": text[:200]}
    if rules.get("which"):
        return "exists", {"rule": "exists", "text": str(rules.get("which"))[:200]}
    if rules.get("exclusive"):
        return "count", {"rule": "equals", "count": 1}
    if str(rules.get("relation") or ""):
        return "visible", {"rule": "visible"}
    return "visible", {"rule": "visible"}


def stamp_compiled_plans(case: dict[str, Any], do_plan: dict[str, Any], check_plan: dict[str, Any]) -> list[dict[str, Any]]:
    """给操作和预期补上 control_id 与步骤字段。未实现的动作返回阻塞说明。

    控件身份只来自明确的元素目标。导航步骤、检查步骤和只有整句描述的
    动作不会创建控件；它们分别属于入口/断言，不能污染控件表。
    """
    project_id = str(case.get("project_id") or "")
    if not project_id or project_id == "_project":
        raise ValueError("control_project_missing: 编译控件必须携带真实 project_id")
    blocked: list[dict[str, Any]] = []
    first_step = ""
    for index, step in enumerate(do_plan.get("steps") or []):
        if not isinstance(step, dict):
            continue
        step_id = f"st_{int(do_plan.get('case_step') or 1)}_{index + 1}"
        step["step_id"] = step_id
        step["step_type"] = "action"
        action = action_type_of(step)
        step["action_type"] = action
        params = step.get("params") if isinstance(step.get("params"), dict) else {}
        target = params.get("target") if isinstance(params.get("target"), dict) else {}
        kind = str(step.get("kind") or "")
        explicit = str(target.get("name") or "").strip()
        desc = explicit or str(step.get("source_clause") or step.get("title") or "").strip()
        step["target_desc"] = desc[:200]
        step["target_role"] = control_semantic_role(desc, explicit=bool(explicit)) if desc else "unknown"
        step["control_id"] = ""
        if kind == "nav":
            nav = str(step.get("nav_target") or desc)
            step["target_desc"] = nav[:200]
            step["target_page"] = analyze_clause(nav).page_label
            step["target_role"] = "nav"
        elif kind not in ("flow_block", "remark") and explicit:
            if step["target_role"] == "element":
                step["control_id"] = ensure_control(
                    project_id,
                    target_desc=explicit,
                    element_type=str(target.get("role") or "button"),
                    scope="dialog" if "弹窗" in explicit else "page",
                    vlm_text=explicit,
                    page_id=str(target.get("page_id") or step.get("page_id") or ""),
                    container_id=str(target.get("container_id") or step.get("container_id") or ""),
                    platform=str(case.get("platform") or ""),
                )
        for slot in step.get("slots") or []:
            if not isinstance(slot, dict):
                continue
            slot_desc = str(slot.get("text") or "").strip()
            slot_target = slot.get("target") if isinstance(slot.get("target"), dict) else {}
            slot["control_id"] = ""
            if slot_desc and control_semantic_role(slot_desc, explicit=True) == "element":
                slot["control_id"] = ensure_control(
                    project_id,
                    target_desc=slot_desc,
                    element_type=str(slot_target.get("role") or "button"),
                    scope="page",
                    vlm_text=slot_desc,
                    page_id=str(slot_target.get("page_id") or step.get("page_id") or ""),
                    container_id=str(slot_target.get("container_id") or step.get("container_id") or ""),
                    platform=str(case.get("platform") or ""),
                )
        extra = dict(params.get("rules") or {}) if isinstance(params.get("rules"), dict) else {}
        if params.get("wait_ms"):
            extra["wait_ms"] = params.get("wait_ms")
        step["step_params"] = extra
        if not first_step:
            first_step = step_id
        if action in _UNIMPLEMENTED:
            blocked.append(
                {
                    "phase": "do",
                    "case_step": int(do_plan.get("case_step") or 1),
                    "reason": "unimplemented",
                    "source_clause": desc[:240],
                    "user_message": f"步骤 {int(do_plan.get('case_step') or 1)} 操作「{desc[:80]}」无法执行，当前版本未实现。",
                }
            )
    for index, step in enumerate(check_plan.get("checkpoints") or []):
        if not isinstance(step, dict):
            continue
        kind, assert_params = assert_of(step)
        payload = step.get("assert") if isinstance(step.get("assert"), dict) else {}
        rules = payload.get("rules") if isinstance(payload.get("rules"), dict) else {}
        element = payload.get("element") if isinstance(payload.get("element"), dict) else {}
        desc = str(element.get("name") or rules.get("which") or rules.get("label") or rules.get("subject")
                   or step.get("source_clause") or step.get("title") or "").strip()
        step["assert_id"] = f"as_{int(check_plan.get('case_step') or 1)}_{index + 1}"
        step["step_id"] = first_step
        step["assert_type"] = kind
        step["target_desc"] = desc[:200]
        # 预期是断言，不是控件。只有以后明确指向已注册元素时才允许引用
        # control_id；这里不创建、也没有整句兜底。
        step["control_id"] = ""
        step["target_role"] = "assertion" if desc else "unknown"
        step["assert_params"] = assert_params
    return blocked

def locator_stop_reason(
    *,
    project_id: str,
    control_id: str,
    scheme: str,
    platform: str,
    dom_locator: str = "",
    target_desc: str = "",
) -> str:
    """选中的格补不上时，这一步不执行。看图只补描述。"""
    if not str(control_id or "").strip():
        return ""
    cell = locator_cell(scheme, platform)
    found = get_control(project_id, control_id)
    if found is None:
        return f"未执行：控件 {control_id} 在项目 {project_id or '_project'} 下不存在（含 _project），需重新导入/编译用例。"
    current = read_locator(found, scheme=scheme, platform=platform)
    if current:
        return ""
    incoming = str(target_desc or "").strip() if cell == "vlm_text" else str(dom_locator or "").strip()
    if incoming and not _looks_like_coord(incoming):
        try:
            fill_locator(
                project_id,
                control_id,
                scheme=scheme,
                platform=platform,
                value=incoming,
                source="case" if cell == "vlm_text" else "dom_hit",
            )
        except ControlRegistryReadOnlyError:
            return "未执行：控件配置只读，当前渠道的定位尚未发布。"
        if read_locator(get_control(project_id, control_id), scheme=scheme, platform=platform):
            return ""
    return "未执行：当前渠道的定位还没有写上。"


def note_execution_locator(
    writer: Any,
    *,
    project_id: str,
    control_id: str,
    scheme: str,
    platform: str,
    dom_locator: str = "",
    target_desc: str = "",
) -> None:
    """记这次用了哪一格。空格只补当前渠道，看图不写坐标。"""
    if writer is None or not str(control_id or "").strip():
        return
    cell = locator_cell(scheme, platform)
    current = read_locator(get_control(project_id, control_id), scheme=scheme, platform=platform)
    filled = False
    write_blocked = ""
    if not current:
        incoming = str(target_desc or "").strip() if cell == "vlm_text" else str(dom_locator or "").strip()
        if incoming and not (cell == "vlm_text" and _looks_like_coord(incoming)):
            try:
                fill_locator(
                    project_id,
                    control_id,
                    scheme=scheme,
                    platform=platform,
                    value=incoming,
                    source="case" if cell == "vlm_text" else "dom_hit",
                )
                filled = bool(read_locator(get_control(project_id, control_id), scheme=scheme, platform=platform))
            except ControlRegistryReadOnlyError as exc:
                write_blocked = exc.reason
    writer.append(
        "control/locator",
        {
            "control_id": str(control_id),
            "cell": cell,
            "filled": filled,
            "scheme": "visual" if str(scheme or "").strip().lower() not in ("dom", "dom_preferred") else "dom",
            "platform": str(platform or ""),
            **({"write_blocked": write_blocked} if write_blocked else {}),
        },
    )


def _looks_like_coord(text: str) -> bool:
    return bool(re.fullmatch(r"\d{1,4}\s*[,，]\s*\d{1,4}", str(text or "").strip()))

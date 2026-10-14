"""控件身份。执行渠道和设备渠道决定读哪一格，空了只补这一格。"""
from __future__ import annotations

import re
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

_RUNTIME_MODE: ContextVar[str] = ContextVar("control_registry_runtime_mode", default="")


class ControlRegistryReadOnlyError(RuntimeError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"控件配置只读：{reason}")


@contextmanager
def registry_runtime(execution_mode: str = "legacy") -> Iterator[None]:
    mode = str(execution_mode or "legacy").strip().lower()
    if mode not in {"legacy", "v5"}:
        raise ValueError(f"未知控件运行模式：{mode}")
    if runtime_readonly():
        mode = "v5"
    token = _RUNTIME_MODE.set(mode)
    try:
        yield
    finally:
        _RUNTIME_MODE.reset(token)


def runtime_readonly() -> bool:
    return _RUNTIME_MODE.get() == "v5"


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
        "vlm_text": "",
        "source": {"playwright": "", "uiautomator": "", "vlm_text": ""},
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
        "vlm_text": str(source.get("vlm_text") or ""),
    }
    return {
        "project_id": row.project_id,
        "target_desc": str(merged.get("vlm_text") or row.desc_key or ""),
        "control_id": row.control_id,
        "legacy_control_id": str(merged.get("legacy_control_id") or ""),
        "element_type": row.element_type or "button",
        "scope": row.scope or "page",
        "locator_value": merged,
        "pending_merge": bool(row.pending_merge),
        "revision": int(row.revision or 1),
        "locator_spec": dict(row.locator_spec or {}),
        "sensitive": bool(row.sensitive),
        "publish_state": row.publish_state or "draft",
        "published_by": row.published_by or "",
        "published_at": row.published_at or "",
        "updated_by": row.updated_by or "",
        "updated_at": row.updated_at or "",
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
    legacy_key = key
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
            if page or container:
                # 明确记录旧身份来源；新作用域只继承描述，不继承可能串页面的定位器。
                legacy = (
                    db.query(UiControl)
                    .filter(UiControl.project_id == pid, UiControl.desc_key == legacy_key)
                    .first()
                )
                if legacy is not None:
                    locator["legacy_control_id"] = str(legacy.control_id)
            row = UiControl(
                control_id=f"ctl_{uuid.uuid4().hex[:8]}",
                project_id=pid,
                desc_key=key,
                element_type=kind,
                scope=where,
                locator_value=locator,
                pending_merge=0,
            )
            db.add(row)
            db.flush()
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
    """Legacy compatibility only; V5 never borrows another project's identity."""
    from mino_nexus.models.ui_control import UiControl

    rows = db.query(UiControl).filter(UiControl.project_id == pid, UiControl.control_id == cid).all()
    if not rows and pid != "_project" and not runtime_readonly():
        rows = db.query(UiControl).filter(UiControl.project_id == "_project", UiControl.control_id == cid).all()
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


def locator_cell(scheme: str, platform: str) -> str:
    """看图只用描述。DOM 按设备取 Playwright 或 UI Automation。"""
    visual = str(scheme or "").strip().lower() != "dom"
    if visual:
        return "vlm_text"
    plat = str(platform or "").strip().lower()
    if plat in ("android", "ios"):
        return "uiautomator"
    return "playwright"


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
    """给操作和预期补上 control_id 与步骤字段。未实现的动作返回阻塞说明。"""
    project_id = str(case.get("project_id") or case.get("app_id") or "")
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
        desc = str(target.get("name") or step.get("source_clause") or step.get("title") or "").strip()
        step["target_desc"] = desc[:200]
        step["control_id"] = ensure_control(
            project_id,
            target_desc=desc,
            element_type=str(target.get("role") or "button"),
            scope="dialog" if "弹窗" in desc else "page",
            vlm_text=desc,
            page_id=str(target.get("page_id") or step.get("page_id") or ""),
            container_id=str(target.get("container_id") or step.get("container_id") or ""),
            platform=str(case.get("platform") or ""),
        )
        for slot in step.get("slots") or []:
            if not isinstance(slot, dict):
                continue
            slot_desc = str(slot.get("text") or "").strip()
            slot_target = slot.get("target") if isinstance(slot.get("target"), dict) else {}
            slot["control_id"] = ensure_control(
                project_id,
                target_desc=slot_desc or str(slot.get("id") or ""),
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
        desc = str(rules.get("which") or rules.get("label") or rules.get("subject") or step.get("title") or "").strip()
        step["assert_id"] = f"as_{int(check_plan.get('case_step') or 1)}_{index + 1}"
        step["step_id"] = first_step
        step["assert_type"] = kind
        step["target_desc"] = desc[:200]
        step["control_id"] = ensure_control(
            project_id,
            target_desc=desc or step["assert_id"],
            element_type="dialog" if kind in ("exists", "visible") and "弹窗" in desc else "text",
            scope="dialog" if "弹窗" in desc else "page",
            vlm_text=desc,
            page_id=str(rules.get("page_id") or step.get("page_id") or ""),
            container_id=str(rules.get("container_id") or step.get("container_id") or ""),
            platform=str(case.get("platform") or ""),
        )
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
            "scheme": "visual" if str(scheme or "").strip().lower() != "dom" else "dom",
            "platform": str(platform or ""),
            **({"write_blocked": write_blocked} if write_blocked else {}),
        },
    )


def _looks_like_coord(text: str) -> bool:
    return bool(re.fullmatch(r"\d{1,4}\s*[,，]\s*\d{1,4}", str(text or "").strip()))

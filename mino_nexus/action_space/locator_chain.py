"""用例执行的定位链：按平台策略解析当前 hierarchy，只接受唯一命中。

模型规划不在这里。0 命中或消歧后仍不唯一，返回 miss/ambiguous，由调用方去兜底。
miss 不是用例失败。android / web / mac / win 都先走结构链；树空或 0 命中才 miss，由调用方看图兜底。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from mino_nexus.navigation.geometry.hierarchy_slots import int_list, rid_short

POINT_CAPS = frozenset({
    "tap_element",
    "long_press_element",
    "multi_tap",
    "swipe_element_to_element",
    "input_text",
})


def locator_chain_enabled(ctx: Any) -> bool:
    raw = getattr(ctx, "locator_chain", None) if ctx is not None else None
    if raw is None and ctx is not None:
        case = getattr(ctx, "case", None)
        if isinstance(case, dict):
            meta = case.get("meta") if isinstance(case.get("meta"), dict) else {}
            raw = meta.get("locator_chain")
    text = str(raw or "on").strip().lower()
    return text not in {"0", "off", "false", "no"}


def needs_locator(capability_id: str) -> bool:
    return str(capability_id or "").strip() in POINT_CAPS


def platform_of(ctx: Any = None, row: dict[str, Any] | None = None) -> str:
    for raw in (
        (row or {}).get("platform") if isinstance(row, dict) else "",
        getattr(ctx, "platform", "") if ctx is not None else "",
        ((getattr(ctx, "case", None) or {}) if ctx is not None else {}).get("platform") if ctx is not None else "",
    ):
        text = str(raw or "").strip()
        if text:
            return text
    return ""


@dataclass
class LocatorStep:
    key: str
    value: str
    source: str = ""
    role: str = ""


@dataclass
class LocatorChain:
    capability_id: str = "tap_element"
    steps: list[LocatorStep] = field(default_factory=list)
    snapshot_id: str = ""
    spatial_hint: str = ""
    parent_resource_id: str = ""
    platform: str = ""
    page_state_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        rows = [
            {k: v for k, v in (("key", s.key), ("value", s.value), ("source", s.source), ("role", s.role)) if v}
            for s in self.steps
            if s.key and s.value
        ]
        out: dict[str, Any] = {"steps": rows}
        if self.snapshot_id:
            out["snapshot_id"] = self.snapshot_id
        if self.spatial_hint:
            out["spatial_hint"] = self.spatial_hint
        if self.parent_resource_id:
            out["parent_resource_id"] = self.parent_resource_id
        if self.platform:
            out["platform"] = self.platform
        if self.page_state_id:
            out["page_state_id"] = self.page_state_id
        return out


@dataclass
class ChainResult:
    status: str
    used_key: str = ""
    used_value: str = ""
    source: str = ""
    node: dict[str, Any] | None = None
    hit_count: int = 0
    reason: str = ""
    platform: str = ""

    def to_event(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "used_key": self.used_key,
            "used_value": self.used_value[:80],
            "source": self.source,
            "hit_count": self.hit_count,
            "reason": self.reason,
            "platform": self.platform,
            "execution_started": False,
        }


def _norm(text: str) -> str:
    return " ".join(str(text or "").strip().split()).casefold()


def _add(
    steps: list[LocatorStep],
    seen: set[tuple[str, str]],
    *,
    key: str,
    value: str,
    source: str,
    role: str = "",
    allowed: tuple[str, ...] | None = None,
) -> None:
    text = str(value or "").strip()
    keys = allowed or ()
    if not text or (keys and key not in keys):
        return
    mark = (key, _norm(text) or text)
    if mark in seen:
        return
    seen.add(mark)
    steps.append(LocatorStep(key=key, value=text, source=source, role=role))


def add_locator_value(steps: list[LocatorStep], seen: set[tuple[str, str]], value: str, *,
                      source: str, platform: str = "") -> None:
    from mino_nexus.action_space.locator_platforms import for_platform

    strat = for_platform(platform)
    for step in strat.parse_value(str(value or ""), source=source):
        _add(steps, seen, key=step.key, value=step.value, source=step.source, role=step.role,
             allowed=strat.key_order)


def _spatial_of(node: dict[str, Any], nodes: list[dict[str, Any]]) -> str:
    sw = sh = 0
    for item in nodes:
        b = int_list(item.get("bounds"), 4)
        if len(b) == 4:
            sw = max(sw, b[2])
            sh = max(sh, b[3])
    b = int_list(node.get("bounds"), 4)
    if len(b) != 4 or sw <= 0 or sh <= 0:
        return ""
    cx = (b[0] + b[2]) / 2.0
    cy = (b[1] + b[3]) / 2.0
    left = cx < sw * 0.38
    right = cx > sw * 0.62
    top = cy < sh * 0.33
    bottom = cy > sh * 0.67
    if top and right:
        return "top_right"
    if top and left:
        return "top_left"
    if bottom and right:
        return "bottom_right"
    if bottom and left:
        return "bottom_left"
    if top:
        return "top"
    if bottom:
        return "bottom"
    if right:
        return "right"
    if left:
        return "left"
    return "center"


def _parent_ok(node: dict[str, Any], parent_resource_id: str) -> bool:
    want = rid_short(parent_resource_id)
    if not want:
        return True
    for key in ("parent_resource_id", "parent_id", "rid_parent"):
        if rid_short(str(node.get(key) or "")) == want:
            return True
    path = str(node.get("path") or node.get("parent_path") or "")
    return want in path if path else False


def _disambiguate(
    hits: list[dict[str, Any]],
    chain: LocatorChain,
    pool: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if len(hits) <= 1:
        return hits
    if chain.parent_resource_id:
        narrowed = [n for n in hits if _parent_ok(n, chain.parent_resource_id)]
        if len(narrowed) == 1:
            return narrowed
        if narrowed:
            hits = narrowed
    hint = str(chain.spatial_hint or "").strip().lower()
    if hint and len(hits) > 1:
        ranked = [n for n in hits if _spatial_of(n, pool) == hint]
        if len(ranked) == 1:
            return ranked
        if ranked:
            hits = ranked
    return hits


def resolve(chain: LocatorChain, nodes: list[dict[str, Any]] | None) -> ChainResult:
    from mino_nexus.action_space.locator_platforms import for_platform

    strat = for_platform(chain.platform)
    plat = chain.platform or strat.name
    if not strat.structural:
        return ChainResult(status="skip", reason="visual_platform", platform=plat)
    pool = strat.usable(nodes, chain.capability_id)
    if not pool:
        return ChainResult(status="miss", reason="structure_empty", platform=plat)
    if not chain.steps:
        return ChainResult(status="miss", reason="chain_empty", hit_count=0, platform=plat)

    grouped: dict[str, list[LocatorStep]] = {key: [] for key in strat.key_order}
    for step in chain.steps:
        if step.key in grouped and str(step.value or "").strip():
            grouped[step.key].append(step)

    for key in strat.key_order:
        alts = grouped.get(key) or []
        if not alts:
            continue
        ambiguous = False
        unique: tuple[LocatorStep, dict[str, Any]] | None = None
        last_count = 0
        for step in alts:
            hits = [n for n in pool if strat.match_node(n, step, chain.snapshot_id)]
            hits = _disambiguate(hits, chain, pool)
            last_count = max(last_count, len(hits))
            if len(hits) == 1:
                unique = (step, hits[0])
                break
            if len(hits) > 1:
                ambiguous = True
                break
        if unique is not None:
            step, node = unique
            return ChainResult(
                status="hit",
                used_key=step.key,
                used_value=step.value,
                source=step.source,
                node=node,
                hit_count=1,
                platform=plat,
            )
        if ambiguous:
            return ChainResult(
                status="ambiguous",
                used_key=key,
                hit_count=last_count,
                reason="not_unique",
                platform=plat,
            )
    return ChainResult(status="miss", reason="no_step_hit", platform=plat)


def build_chain(
    *,
    capability_id: str,
    params: dict[str, Any] | None = None,
    compiled: dict[str, Any] | None = None,
    fingerprints: dict[str, Any] | None = None,
    match_phrases: list[str] | None = None,
    snapshot_id: str = "",
    platform: str = "",
    control: dict[str, Any] | None = None,
) -> LocatorChain:
    from mino_nexus.action_space.locator_platforms import for_platform

    cap = str(capability_id or "tap_element").strip() or "tap_element"
    params = params if isinstance(params, dict) else {}
    compiled = compiled if isinstance(compiled, dict) else {}
    plat = str(platform or compiled.get("platform") or "").strip()
    strat = for_platform(plat)
    target = params.get("target") if isinstance(params.get("target"), dict) else {}
    chain = LocatorChain(
        capability_id=cap,
        snapshot_id=str(snapshot_id or params.get("snapshot_id") or compiled.get("snapshot_id") or "").strip(),
        spatial_hint=str(compiled.get("spatial_hint") or params.get("spatial_hint") or "").strip().lower(),
        parent_resource_id=rid_short(
            str(compiled.get("parent_resource_id") or target.get("parent_resource_id") or "")
        ),
        platform=plat,
        page_state_id=str(compiled.get("page_state_id") or params.get("page_state_id") or "").strip(),
    )
    return strat.ingest(
        chain,
        params=params,
        compiled=compiled,
        fingerprints=fingerprints if isinstance(fingerprints, dict) else None,
        match_phrases=[str(p).strip() for p in (match_phrases or []) if str(p or "").strip()],
        control=control if isinstance(control, dict) else None,
    )


_LABEL_STEP_KEYS = frozenset({
    "text", "content_desc", "label", "title", "name", "selector_text",
})


def match_phrases_of(row: dict[str, Any] | None) -> list[str]:
    raw = (row or {}).get("match") if isinstance(row, dict) else None
    if not isinstance(raw, list):
        return []
    return [str(item).strip() for item in raw if str(item or "").strip()]


def _label_candidates(row: dict[str, Any] | None) -> list[str]:
    focus = row if isinstance(row, dict) else {}
    out: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        text = str(value or "").strip()
        mark = _norm(text)
        if not text or len(mark) < 2 or mark in seen:
            return
        seen.add(mark)
        out.append(text)

    for phrase in match_phrases_of(focus):
        add(phrase)
    compiled = focus.get("locator_chain") if isinstance(focus.get("locator_chain"), dict) else {}
    for step in compiled.get("steps") or []:
        if not isinstance(step, dict):
            continue
        if str(step.get("key") or "") in _LABEL_STEP_KEYS:
            add(str(step.get("value") or ""))
    params = focus.get("params") if isinstance(focus.get("params"), dict) else {}
    target = params.get("target") if isinstance(params.get("target"), dict) else {}
    element = params.get("element") if isinstance(params.get("element"), dict) else {}
    for blob in (target, element, params):
        if not isinstance(blob, dict):
            continue
        for key in ("text", "selector_text", "content_desc", "name", "label", "title"):
            add(str(blob.get(key) or ""))
    return out


def chain_dict_from_focus(
    row: dict[str, Any] | None,
    *,
    capability_id: str = "",
    platform: str = "",
    control: dict[str, Any] | None = None,
) -> dict[str, Any]:
    focus = row if isinstance(row, dict) else {}
    cap = str(capability_id or focus.get("hook_cap") or focus.get("device_cap") or "tap_element")
    params = focus.get("params") if isinstance(focus.get("params"), dict) else {}
    compiled = focus.get("locator_chain") if isinstance(focus.get("locator_chain"), dict) else {}
    plat = str(platform or focus.get("platform") or compiled.get("platform") or "")
    chain = build_chain(
        capability_id=cap,
        params=params,
        compiled=compiled,
        match_phrases=match_phrases_of(focus),
        platform=plat,
        control=control,
    )
    return chain.to_dict() if chain.steps else {}


def attach_to_row(
    row: dict[str, Any] | None,
    *,
    platform: str = "",
    project_id: str = "",
    app_id: str = "",
    ctx: Any = None,
) -> dict[str, Any]:
    """Compile a chain onto a milestone/plan row. Empty chain is left unset."""
    if not isinstance(row, dict):
        return {}
    plat = str(platform or platform_of(ctx, row) or "")
    if plat:
        row.setdefault("platform", plat)
    cap = str(row.get("hook_cap") or row.get("device_cap") or row.get("cap") or "")
    if cap and not needs_locator(cap) and str(row.get("kind") or "") not in {"visual_tap", "visual_action", "nav"}:
        return row
    control = _control_of(row, ctx, project_id=project_id)
    nav = _nav_execute_locator(str(app_id or getattr(ctx, "app_id", "") or ""), str(row.get("nav_target") or ""))
    if nav:
        params = dict(row.get("params") or {}) if isinstance(row.get("params"), dict) else {}
        target = dict(params.get("target") or {}) if isinstance(params.get("target"), dict) else {}
        for key in (
            "resource_id", "content_desc", "selector_text", "text", "test_id",
            "identifier", "automation_id", "title", "name", "accessibility_id",
        ):
            if nav.get(key) and not target.get(key):
                target[key] = nav[key]
        if target:
            params["target"] = target
            row["params"] = params
    blob = chain_dict_from_focus(row, platform=plat, control=control)
    if blob.get("steps"):
        row["locator_chain"] = blob
    return row


def stamp_runtime(ctx: Any, writer: Any = None) -> None:
    enabled = locator_chain_enabled(ctx)
    plat = platform_of(ctx)
    from mino_nexus.action_space.locator_platforms import for_platform

    strat = for_platform(plat)
    payload = {
        "enabled": bool(enabled),
        "platform": plat,
        "structural": bool(strat.structural),
    }
    if ctx is not None and getattr(ctx, "locator_chain", None) is None:
        try:
            ctx.locator_chain = "on" if enabled else "off"
        except AttributeError:
            # 第三方/测试 stub 可能没有该 slot；开关仍可由 case/meta 读取。
            pass
    if writer is None:
        return
    try:
        from mino_nexus.records.eventlog.atomic import append_batch

        append_batch(writer.session_id, [], meta_patch={"runtime_context": {"locator_chain": payload}})
        writer.append("locator_chain/config", {**payload, "execution_started": False})
    except Exception:
        writer.append("locator_chain/config", {**payload, "execution_started": False})


def _control_of(row: dict[str, Any], ctx: Any, *, project_id: str = "") -> dict[str, Any] | None:
    cid = str(row.get("control_id") or "").strip()
    params = row.get("params") if isinstance(row.get("params"), dict) else {}
    cid = cid or str(params.get("control_id") or "").strip()
    pid = str(project_id or getattr(ctx, "project_id", "") or "").strip()
    if not pid:
        return None
    try:
        from mino_nexus.knowledge.control_registry import (
            find_control_by_label,
            find_control_in_text,
            get_control,
        )
    except Exception:
        return None
    if cid:
        try:
            found = get_control(pid, cid)
        except (ValueError, KeyError, TypeError):
            found = None
        if found:
            return found
    hits: list[dict[str, Any]] = []
    seen: set[str] = set()
    for label in _label_candidates(row):
        try:
            hit = find_control_by_label(pid, label)
        except (ValueError, KeyError, TypeError):
            hit = None
        if not hit:
            continue
        hid = str(hit.get("control_id") or "")
        if not hid or hid in seen:
            continue
        seen.add(hid)
        hits.append(hit)
    if len(hits) == 1:
        row["control_id"] = str(hits[0].get("control_id") or "")
        return hits[0]
    clause = ""
    for key in ("source_clause", "handoff_question", "source_line", "title"):
        clause = str(row.get(key) or "").strip()
        if clause:
            break
    if not clause:
        return None
    try:
        hit = find_control_in_text(pid, clause)
    except (ValueError, KeyError, TypeError):
        hit = None
    if hit:
        row["control_id"] = str(hit.get("control_id") or "")
        return hit
    return None


def _nav_execute_locator(app_id: str, nav_target: str) -> dict[str, str]:
    app = str(app_id or "").strip()
    target = str(nav_target or "").strip()
    if not app or not target:
        return {}
    try:
        from mino_nexus.core.database import session_scope
        from mino_nexus.models.nav_fsm import NavFsm, NavFsmEdge, NavFsmState
    except Exception:
        return {}
    with session_scope() as db:
        fsm = db.query(NavFsm).filter_by(app_id=app, version="v1").one_or_none()
        if fsm is None:
            return {}
        states = db.query(NavFsmState).filter_by(fsm_id=fsm.id).all()
        want = {target}
        for state in states:
            meta = state.meta if isinstance(state.meta, dict) else {}
            names = [state.state_id, meta.get("display_name"), *(meta.get("aliases") or [])]
            if any(str(n or "").strip() == target for n in names):
                want.add(str(state.state_id or ""))
        edges = (
            db.query(NavFsmEdge)
            .filter(NavFsmEdge.fsm_id == fsm.id, NavFsmEdge.to_state.in_(sorted(want)))
            .all()
        )
        for edge in edges:
            exe = edge.execute if isinstance(edge.execute, dict) else {}
            out = {
                key: str(exe.get(key) or "").strip()
                for key in (
                    "resource_id", "content_desc", "selector_text", "text", "test_id",
                    "accessibility_id", "identifier", "automation_id", "title", "name",
                )
                if str(exe.get(key) or "").strip()
            }
            if out.get("accessibility_id"):
                out.setdefault("identifier", out["accessibility_id"])
                out.setdefault("automation_id", out["accessibility_id"])
                out.setdefault("resource_id", out["accessibility_id"])
            if out:
                return out
    return {}

"""把控件挂到它所属的导航页。

页面可以来自骨架指纹，也可以来自 DOM 层级、用例里的先后跳转、
结构上的先后（输入页前面的那一屏），以及文档里同时写到的页面和控件。
不靠空的 web page_id，也不把某个 App 的文案写死在代码里。
"""
from __future__ import annotations

import hashlib
import re
import time
from collections import Counter, defaultdict

from mino_nexus.knowledge.page_semantics import analyze_clause, page_key

_SKIP = {"页面", "edittext"}
_RANK = {"manual": 6, "observed": 5, "dom": 4, "case_flow": 3, "structure": 2, "document": 1}
_QUOTED = re.compile(r"[“\"「]([^”\"」]{2,16})[”\"」]")


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", str(text or "")).strip("“”\"'。，,.：: ")


def _safe(raw: str) -> str:
    seg = str(raw or "").strip().replace("\\", "/").split("/")[-1]
    seg = "".join(ch for ch in seg if ch.isalnum() or ch in "-_.")
    return "_" if seg in ("", ".", "..") else seg


def _usable(text: str) -> str:
    token = _norm(text)
    if len(token) < 2 or token.casefold() in _SKIP or token.isdigit():
        return ""
    return token


def _case_state_id(name: str) -> str:
    return "page.case." + hashlib.sha1(_norm(name).encode()).hexdigest()[:10]


def _majority(counts: Counter[str]) -> str:
    if not counts:
        return ""
    (state_id, count), *rest = counts.most_common()
    second = rest[0][1] if rest else 0
    return state_id if count > second else ""


def bind_project_controls(project_id: str) -> int:
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.nav_fsm import NavFsm
    from mino_nexus.models.project import App

    with session_scope() as db:
        app_ids = [row.id for row in db.query(App).filter_by(project_id=project_id)]
        graphs = db.query(NavFsm).filter(NavFsm.project_id == project_id, NavFsm.app_id.in_(app_ids or [""])).all()
        pairs = [(row.app_id, row.id) for row in graphs if row.app_id]
    updated = 0
    for app_id, fsm_id in pairs:
        updated += bind_app_controls(project_id, app_id, fsm_id=fsm_id)
    return updated


def bind_app_controls(project_id: str, app_id: str, *, fsm_id: int | None = None) -> int:
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.case import ProjectCase
    from mino_nexus.models.doc_library import DocChunk, DocSource
    from mino_nexus.models.nav_case_page_ref import NavCasePageRef
    from mino_nexus.models.nav_fsm import NavFsm, NavFsmEdge, NavFsmState
    from mino_nexus.models.session_event import SessionEvent, SessionMeta
    from mino_nexus.models.ui_control import UiControl

    with session_scope() as db:
        fsm = db.get(NavFsm, fsm_id) if fsm_id else db.query(NavFsm).filter_by(
            app_id=app_id, project_id=project_id, version="v1").one_or_none()
        if fsm is None:
            return 0
        states = db.query(NavFsmState).filter_by(fsm_id=fsm.id).all()
        label_to_state: dict[str, str] = {}
        for row in states:
            meta = row.meta or {}
            if str(meta.get("semantic_status") or "") == "rejected":
                continue
            name = str(meta.get("display_name") or "").strip()
            sem = analyze_clause(name)
            # 旧的 case_flow 长句页面不能继续当页面别名；只映射规范短名。
            canonical = sem.page_label if sem.page_label and (
                str(meta.get("page_source") or "") != "case_flow"
                or (sem.role == "page" and not sem.variant and not sem.data and not sem.warnings)
            ) else ""
            if canonical:
                token = _usable(canonical)
                if token and token not in label_to_state:
                    label_to_state[token] = row.state_id
            if not canonical and name and len(name) <= 18 and not re.search(r"[，,；;。]", name) \
                    and not any(name.startswith(verb) for verb in ("进入", "点击", "查看", "观察", "检查", "生成", "放弃", "支付", "授权")):
                token = _usable(name)
                if token and token not in label_to_state:
                    label_to_state[token] = row.state_id
            for raw in (meta.get("aliases") or []):
                if str(meta.get("page_source") or "") == "case_flow" and not canonical:
                    continue
                alias = analyze_clause(str(raw or ""))
                token = _usable(alias.page_label or str(raw or ""))
                if token and token not in label_to_state:
                    label_to_state[token] = row.state_id
        turn_page: dict[tuple[str, int], str] = {}
        order: dict[str, list[str]] = defaultdict(list)
        for ref in ((fsm.meta or {}).get("atlas_turn_refs") or []):
            if not isinstance(ref, dict):
                continue
            state_id = str(ref.get("state_id") or "")
            if not state_id:
                continue
            key = (_safe(ref.get("session_id")), int(ref.get("turn_id") or 0))
            turn_page[key] = state_id
            seq = order[key[0]]
            if not seq or seq[-1] != state_id:
                seq.append(state_id)

        dom_lines, input_pages = _dom_index(app_id, turn_page)
        cases = db.query(ProjectCase).filter_by(project_id=project_id).all()
        case_bindings, case_pages, case_edges, case_refs, case_gaps = _case_flow(cases, label_to_state)
        for name, (state_id, sem) in case_pages.items():
            if any(row.state_id == state_id for row in states):
                continue
            db.add(NavFsmState(
                fsm_id=fsm.id,
                state_id=state_id,
                kind="page",
                identify={},
                guards={},
                meta={
                    "display_name": name,
                    "aliases": [name],
                    "page_source": "case_flow",
                    "page_key": page_key(name),
                    "variant": sem.variant,
                    "semantic_status": "provisional",
                    "semantic_rule": sem.rule_id,
                },
            ))
            label_to_state.setdefault(_usable(name), state_id)
        db.query(NavFsmEdge).filter(NavFsmEdge.fsm_id == fsm.id, NavFsmEdge.edge_id.like("case.%")).delete(synchronize_session=False)
        known_edges = {(row.from_state, row.to_state, str((row.execute or {}).get("text") or "")) for row in db.query(NavFsmEdge).filter_by(fsm_id=fsm.id)}
        for src, dst, text in case_edges:
            mark = (src, dst, text)
            if not src or not dst or src == dst or mark in known_edges:
                continue
            digest = hashlib.sha1(f"{src}|{dst}|{text}".encode()).hexdigest()[:10]
            db.add(NavFsmEdge(
                fsm_id=fsm.id,
                edge_id=f"case.{digest}",
                kind="nav",
                from_state=src,
                to_state=dst,
                guard={},
                execute={"steps": ["tap_element"], "selector_text": text, "text": text},
                effect_assert={},
                on_fail={},
                scroll_into_view={},
            ))
            known_edges.add(mark)

        now = int(time.time())
        for ref in case_refs:
            if not ref.get("page_state_id"):
                continue
            row = db.query(NavCasePageRef).filter_by(app_id=app_id, ref_id=ref["ref_id"]).one_or_none()
            if row is None:
                row = NavCasePageRef(
                    ref_id=ref["ref_id"],
                    app_id=app_id,
                    project_id=project_id,
                    case_id=ref["case_id"],
                    case_name=ref.get("case_name") or "",
                    step_index=ref["step_index"],
                    step_id=ref.get("step_id") or "",
                    page_state_id=ref["page_state_id"],
                    page_label=ref.get("page_label") or "",
                    control_id=ref.get("control_id") or "",
                    relation=ref["relation"],
                    quote=ref.get("quote") or "",
                    variant=ref.get("variant") or "",
                    data=ref.get("data") or {},
                    created_at=now,
                    updated_at=now,
                )
                db.add(row)
            else:
                row.case_name = ref.get("case_name") or row.case_name
                row.page_label = ref.get("page_label") or row.page_label
                row.quote = ref.get("quote") or row.quote
                row.variant = ref.get("variant") or ""
                row.data = ref.get("data") or {}
                row.updated_at = now

        observed: dict[str, str] = {}
        votes: dict[str, Counter[str]] = defaultdict(Counter)
        events = (
            db.query(SessionEvent.session_id, SessionEvent.turn, SessionEvent.payload)
            .join(SessionMeta, SessionMeta.session_id == SessionEvent.session_id)
            .filter(SessionMeta.app_id == app_id, SessionEvent.type == "control/locator")
            .all()
        )
        for session_id, turn, payload in events:
            body = payload if isinstance(payload, dict) else {}
            control_id = str(body.get("control_id") or "")
            state_id = turn_page.get((_safe(session_id), int(turn or 0)))
            if control_id and state_id:
                votes[control_id][state_id] += 1
        for control_id, counts in votes.items():
            state_id = _majority(counts)
            if state_id:
                observed[control_id] = state_id

        controls = db.query(UiControl).filter_by(project_id=project_id).all()
        text_of = {
            row.control_id: str((row.locator_value or {}).get("vlm_text") or row.desc_key or "")
            for row in controls
        }
        pairs = list(text_of.items())
        dom_bindings = _dom_bindings_for_controls(dom_lines, pairs)
        pred = _predecessors(order, input_pages)
        structure = _structure_bindings(cases, {**observed, **dom_bindings, **case_bindings}, pred, input_pages)
        documents = _document_bindings_for_controls(
            db.query(DocChunk.heading, DocChunk.text)
            .join(DocSource, DocSource.id == DocChunk.source_id)
            .filter(DocSource.app_id == app_id)
            .limit(400)
            .all(),
            pairs,
            label_to_state,
        )
        cited = _cited_pages(text_of, label_to_state)

        updated = 0
        for row in controls:
            locator = dict(row.locator_value or {})
            current = str(locator.get("nav_state_id") or "")
            current_source = str(locator.get("nav_state_source") or "")
            state_id, source = _best(
                row.control_id,
                observed=observed,
                dom=dom_bindings,
                case_flow=case_bindings,
                cited=cited,
                structure=structure,
                documents=documents,
            )
            if not state_id:
                continue
            if current and _RANK.get(current_source, 0) >= _RANK.get(source, 0):
                continue
            if state_id == current and current_source == source:
                continue
            locator["nav_state_id"] = state_id
            locator["nav_state_source"] = source
            row.locator_value = locator
            row.revision = int(row.revision or 1) + 1
            updated += 1
        for gap in case_gaps:
            gap["app_id"] = app_id
            gap["project_id"] = project_id
        _record_case_gaps(db, case_gaps)
        return updated


def _record_case_gaps(db, gaps: list[dict]) -> None:
    """在同一个事务里记缺口，避免外层未提交时另开 SQLite 写连接。"""
    if not gaps:
        return
    from mino_nexus.models.nav_page_entry import NavPageGap

    now = int(time.time())
    for gap in gaps:
        raw = "|".join([
            str(gap.get("kind") or "target_only"), str(gap.get("target_label") or ""),
            str(gap.get("target_state_id") or ""), str(gap.get("from_state_id") or ""),
            str(gap.get("platform") or ""),
        ])
        gap_id = "gap." + hashlib.sha1(raw.encode()).hexdigest()[:20]
        row = db.query(NavPageGap).filter_by(app_id=gap.get("app_id") or "", gap_id=gap_id).one_or_none()
        if row is None:
            row = NavPageGap(
                gap_id=gap_id, app_id=gap.get("app_id") or "", project_id=gap.get("project_id") or "",
                target_label=gap.get("target_label") or "", kind=gap.get("kind") or "target_only",
                detail=str(gap.get("detail") or "")[:2000], occurrence_count=1,
                first_seen_at=now, last_seen_at=now,
                sample_refs=[gap["sample_ref"]] if gap.get("sample_ref") else [],
            )
            db.add(row)
        else:
            row.occurrence_count = int(row.occurrence_count or 0) + 1
            row.last_seen_at = now
            row.status = "open"
            if gap.get("detail"):
                row.detail = str(gap["detail"])[:2000]
            refs = list(row.sample_refs or [])
            if gap.get("sample_ref"):
                refs.append(gap["sample_ref"])
            row.sample_refs = refs[-5:]


def _best(control_id: str, **groups: dict[str, str]) -> tuple[str, str]:
    for source in ("observed", "dom", "case_flow", "cited", "structure", "documents"):
        state_id = groups.get(source, {}).get(control_id) or ""
        if state_id:
            mapped = "case_flow" if source == "cited" else ("document" if source == "documents" else source)
            return state_id, mapped
    return "", ""


def _case_flow(cases, label_to_state: dict[str, str]):
    """从编译后的用例步骤提取页面/控件引用和入口边。

    用例是证据，不是页面目录：只有规范短页面名才允许建立 provisional 页面；
    动作、断言、流程句只进入引用/缺口，不生成页面。
    """
    bindings: dict[str, str] = {}
    pages: dict[str, tuple[str, object]] = {}
    edges: list[tuple[str, str, str]] = []
    refs: list[dict] = []
    gaps: list[dict] = []

    def resolve(raw: str):
        sem = analyze_clause(raw, known_labels=list(label_to_state))
        label = sem.page_label
        token = _usable(label) if label else ""
        if token and token in label_to_state:
            return label_to_state[token], sem
        # 兼容“帖子详情页”这类已知页：页面名词被拆在“进入详情”里时，
        # 用已知页面的词干做一次唯一匹配，不凭空造页面。
        if not label and "详情" in str(raw or ""):
            hits = [name for name in label_to_state if name.endswith("详情页") and name[:-3] and name[:-3] in str(raw or "")]
            if len(hits) == 1:
                label = hits[0]
                sem = analyze_clause(label)
                token = _usable(label)
                if token in label_to_state:
                    return label_to_state[token], sem
        if not label:
            return "", sem
        if sem.role not in ("page", "nav", "action") or sem.confidence < 0.6:
            return "", sem
        if not label or len(label) > 14 or sem.warnings:
            return "", sem
        state_id = _case_state_id(token)
        pages[label] = (state_id, sem)
        label_to_state[token] = state_id
        return state_id, sem

    def add_ref(ref: dict) -> None:
        # 引用必须挂到真实页面；动作/流程句不生成页面引用。
        if not ref.get("page_state_id"):
            return
        raw = "|".join([
            str(ref.get("case_id") or ""), str(ref.get("step_index") or 0),
            str(ref.get("relation") or ""), str(ref.get("page_state_id") or ""),
            str(ref.get("control_id") or ""), str(ref.get("quote") or ""),
        ])
        ref["ref_id"] = "ref." + hashlib.sha1(raw.encode()).hexdigest()[:20]
        refs.append(ref)

    for case in cases:
        bundles = ((case.extra or {}).get("meta") or {}).get("step_program_keys") or {}
        keys = sorted(bundles, key=lambda item: int(item) if str(item).isdigit() else 0)
        current = ""
        current_label = ""
        pending: list[tuple[str, str]] = []
        for key in keys:
            step_index = int(key) if str(key).isdigit() else 0
            steps = ((bundles[key].get("do_program_plan") or {}).get("steps") or [])
            checks = ((bundles[key].get("check_program_plan") or {}).get("checkpoints") or [])
            for step in steps:
                if not isinstance(step, dict):
                    continue
                quote = str(step.get("source_clause") or step.get("title") or "")[:500]
                if step.get("kind") == "nav":
                    raw = str(step.get("nav_target") or step.get("title") or "")
                    target, sem = resolve(raw)
                    trigger = pending[-1][1] if pending else ""
                    trigger_control = pending[-1][0] if pending else ""
                    for control_id, _text in pending:
                        if current and control_id not in bindings:
                            bindings[control_id] = current
                    if current and target and trigger and target != current:
                        edges.append((current, target, trigger))
                    add_ref({
                        "case_id": str(getattr(case, "case_id", "") or ""),
                        "case_name": str(getattr(case, "name", "") or ""),
                        "step_index": step_index, "relation": "enters",
                        "page_state_id": target, "page_label": sem.page_label or raw,
                        "control_id": trigger_control, "quote": quote,
                        "variant": sem.variant, "data": sem.data,
                    })
                    if not target and sem.page_label:
                        gaps.append({
                            "app_id": "",
                            "target_label": sem.page_label,
                            "kind": "target_only",
                            "detail": f"用例写了页面目标「{sem.page_label}」，但没有可确认的页面身份；先补采集/别名，不盲点。",
                            "sample_ref": {"case_id": str(getattr(case, "case_id", "") or ""), "step": step_index, "quote": quote},
                        })
                    pending = []
                    if target:
                        current = target
                        current_label = sem.page_label
                    continue
                control_id = str(step.get("control_id") or "")
                text = _usable(str(step.get("target_desc") or step.get("title") or ""))
                if control_id:
                    pending.append((control_id, text))
                    if current and control_id not in bindings:
                        bindings[control_id] = current
                    add_ref({
                        "case_id": str(getattr(case, "case_id", "") or ""),
                        "case_name": str(getattr(case, "name", "") or ""),
                        "step_index": step_index, "relation": "operates",
                        "page_state_id": current, "page_label": current_label,
                        "control_id": control_id, "quote": quote,
                    })
            for step in checks:
                if not isinstance(step, dict) or not current:
                    continue
                add_ref({
                    "case_id": str(getattr(case, "case_id", "") or ""),
                    "case_name": str(getattr(case, "name", "") or ""),
                    "step_index": step_index, "relation": "asserts",
                    "page_state_id": current, "page_label": current_label,
                    "control_id": "", "quote": str(step.get("source_clause") or step.get("title") or "")[:500],
                })
        for control_id, _text in pending:
            if current and control_id not in bindings:
                bindings[control_id] = current
    return bindings, pages, edges, refs, gaps

def _dom_index(app_id: str, turn_page: dict[tuple[str, int], str]) -> tuple[dict[str, set[str]], set[str]]:
    from mino_nexus.core.paths import nav_capture_dir
    import json

    lines: dict[str, set[str]] = defaultdict(set)
    editable: Counter[str] = Counter()
    root = nav_capture_dir(app_id)
    if not root.exists():
        return {}, set()
    for (session_id, turn_id), state_id in turn_page.items():
        path = root / session_id / f"turn_{turn_id:04d}.nodes.json"
        if not path.exists():
            continue
        try:
            nodes = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        editors = 0
        for node in nodes if isinstance(nodes, list) else []:
            if not isinstance(node, dict):
                continue
            if node.get("editable") or str(node.get("class") or "").endswith("EditText"):
                editors += 1
            for raw in (node.get("text"), node.get("content_desc")):
                for line in str(raw or "").splitlines():
                    token = _usable(line)
                    if token:
                        lines[state_id].add(token)
        if editors >= 2:
            editable[state_id] += 1
    return lines, {state_id for state_id, count in editable.items() if count}


def _dom_bindings_for_controls(lines: dict[str, set[str]], controls: list[tuple[str, str]]) -> dict[str, str]:
    found: dict[str, str] = {}
    for control_id, raw in controls:
        quotes = [_usable(item) for item in _QUOTED.findall(raw)]
        tokens = [item for item in quotes if item] or ([_usable(raw)] if len(_norm(raw)) <= 24 else [])
        scores: Counter[str] = Counter()
        for token in tokens:
            if not token:
                continue
            for state_id, page_lines in lines.items():
                if any(token == line or (len(token) >= 4 and token in line) for line in page_lines):
                    scores[state_id] += 1
        state_id = _majority(scores)
        if state_id:
            found[control_id] = state_id
    return found


def _predecessors(order: dict[str, list[str]], input_pages: set[str]) -> dict[str, str]:
    votes: dict[str, Counter[str]] = defaultdict(Counter)
    for seq in order.values():
        previous = ""
        for state_id in seq:
            if state_id in input_pages and previous and previous not in input_pages:
                votes[state_id][previous] += 1
            previous = state_id
    return {state_id: _majority(counts) for state_id, counts in votes.items() if _majority(counts)}


def _structure_bindings(cases, bound: dict[str, str], pred: dict[str, str], input_pages: set[str]) -> dict[str, str]:
    """输入页前面的那一屏。只把紧挨在它之前、还没归页的控件放过去。"""
    found: dict[str, str] = {}
    for case in cases:
        bundles = ((case.extra or {}).get("meta") or {}).get("step_program_keys") or {}
        keys = sorted(bundles, key=lambda item: int(item) if str(item).isdigit() else 0)
        previous = ""
        for key in keys:
            for step in ((bundles[key].get("do_program_plan") or {}).get("steps") or []):
                if not isinstance(step, dict) or step.get("kind") == "nav":
                    continue
                control_id = str(step.get("control_id") or "")
                if not control_id:
                    continue
                page = bound.get(control_id) or ""
                if previous and page in input_pages and previous not in bound and previous not in found:
                    earlier = pred.get(page) or ""
                    if earlier:
                        found[previous] = earlier
                previous = control_id
    return found


def _cited_pages(text_of: dict[str, str], label_to_state: dict[str, str]) -> dict[str, str]:
    found: dict[str, str] = {}
    labels = [(label, state_id) for label, state_id in label_to_state.items() if len(label) >= 2]
    for control_id, raw in text_of.items():
        text = _norm(raw)
        hits = [(len(label), state_id) for label, state_id in labels if label in text]
        if not hits:
            continue
        hits.sort(reverse=True)
        if len(hits) == 1 or hits[0][0] > hits[1][0]:
            found[control_id] = hits[0][1]
    return found


def _document_bindings_for_controls(chunks, controls: list[tuple[str, str]], label_to_state: dict[str, str]) -> dict[str, str]:
    bodies = [_norm((heading or "") + (text or "")) for heading, text in chunks]
    if not bodies:
        return {}
    found: dict[str, str] = {}
    labels = [(label, state_id) for label, state_id in label_to_state.items() if 2 <= len(label) <= 24]
    for control_id, raw in controls:
        token = _usable(raw)
        if not token or len(token) > 16:
            continue
        scores: Counter[str] = Counter()
        for body in bodies:
            if token not in body:
                continue
            named = {state_id for label, state_id in labels if label in body}
            if len(named) == 1:
                scores[next(iter(named))] += 1
        state_id = _majority(scores)
        if state_id:
            found[control_id] = state_id
    return found

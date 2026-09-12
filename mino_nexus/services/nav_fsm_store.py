"""NavFSM 配置读写。真源是 `mino.db` 的 nav_fsm / nav_fsm_states / nav_fsm_edges。

设计稿 docs/NAVIGATION_ATLAS.md §0.2、§10.5、§11.4。

三条硬规则：
  1. **`__CALIBRATE__` 不得进 runtime** —— load 与 PUT 双向门禁（§10.5）；
  2. **`project_id` 必须与 `apps.project_id` 一致** —— 否则是配错了 app，直接拒；
  3. **校准账号必须与跑批租号一致** —— 不一致说明 hierarchy 是另一个账号采的，
     `follow` 态、resource-id 都可能不同，必须重新 walkthrough（§11.4）。

拒绝一律返回 `(None, 原因)`，**不抛异常** —— 没配 NavFSM 是正常状态（绝大多数 app 都没配），
不该让跑批挂掉。只有写入侧（save）才抛，让 PUT 回 422。
"""
from __future__ import annotations

import time
from typing import Any, Optional

from mino_nexus.models.nav_fsm import NavFsm, NavFsmEdge, NavFsmState

CALIBRATE_MARK = "__CALIBRATE__"
DEFAULT_VERSION = "v1"
DRAFT_VERSION = "draft"


class NavFsmInvalid(ValueError):
    """配置不合法。PUT 侧转 422，seed 侧直接中止。"""


# ---------------- 校验 ----------------


def validate_nav_fsm(obj: Any, path: str = "") -> None:
    """递归查残留占位符（§10.5）。seed 草稿可以有，进 runtime 的不许有。"""
    if isinstance(obj, str):
        if CALIBRATE_MARK in obj:
            raise NavFsmInvalid(f"nav_fsm 未校准：{path or '<root>'} 仍是 {CALIBRATE_MARK}")
        return
    if isinstance(obj, dict):
        for key, val in obj.items():
            validate_nav_fsm(key, f"{path}.{key}" if path else str(key))
            validate_nav_fsm(val, f"{path}.{key}" if path else str(key))
        return
    if isinstance(obj, (list, tuple)):
        for i, val in enumerate(obj):
            validate_nav_fsm(val, f"{path}[{i}]")


def _nav_edges_only(edges: list[Any]) -> list[dict[str, Any]]:
    """路线图只持久化 nav 边；recover 走 recovery 扩展包，不进 edges 表。"""
    out: list[dict[str, Any]] = []
    for ed in edges or []:
        if not isinstance(ed, dict):
            continue
        if str(ed.get("kind") or "nav") != "nav":
            continue
        out.append(ed)
    return out


def validate_doc(doc: dict[str, Any], *, allow_calibrate: bool = False) -> None:
    """写入前的结构校验：必填字段 + 边引用的 state 必须存在（§0.2.3「校验」行）。"""
    if not isinstance(doc, dict):
        raise NavFsmInvalid("nav_fsm 载荷必须是对象")
    if not str(doc.get("app_id") or "").strip():
        raise NavFsmInvalid("app_id 必填")
    if not str(doc.get("project_id") or "").strip():
        raise NavFsmInvalid("project_id 必填")

    states = doc.get("states") or []
    edges = _nav_edges_only(doc.get("edges") or [])
    ids: set[str] = set()
    for i, st in enumerate(states):
        sid = str((st or {}).get("id") or (st or {}).get("state_id") or "").strip()
        if not sid:
            raise NavFsmInvalid(f"states[{i}] 缺 id")
        if sid in ids:
            raise NavFsmInvalid(f"state_id 重复：{sid}")
        ids.add(sid)

    seen_edges: set[str] = set()
    for i, ed in enumerate(edges):
        eid = str((ed or {}).get("id") or (ed or {}).get("edge_id") or "").strip()
        if not eid:
            raise NavFsmInvalid(f"edges[{i}] 缺 id")
        if eid in seen_edges:
            raise NavFsmInvalid(f"edge_id 重复：{eid}")
        seen_edges.add(eid)
        kind = str((ed or {}).get("kind") or "nav").strip()
        for side in ("from", "to"):
            ref = str((ed or {}).get(side) or (ed or {}).get(f"{side}_state") or "").strip()
            # recover 边可以从任意状态出发，允许 from 为空 / 通配
            if not ref and side == "from" and kind == "recover":
                continue
            if not ref:
                raise NavFsmInvalid(f"边 {eid} 缺 {side}")
            if ref != "*" and ref not in ids:
                raise NavFsmInvalid(f"边 {eid} 的 {side}={ref} 不在 states 里")

    if not allow_calibrate:
        validate_nav_fsm(doc)


# ---------------- 形状转换 ----------------


def _state_public(row: NavFsmState) -> dict[str, Any]:
    out = {
        "id": row.state_id or "",
        "kind": row.kind or "page",
        "identify": dict(row.identify or {}),
        "guards": dict(row.guards or {}),
        "wiki_ref": row.wiki_ref or "",
    }
    if int(getattr(row, "entry", 0) or 0):
        out["entry"] = True
    role = str(getattr(row, "role", "") or "").strip()
    if role:
        out["role"] = role
    return out


def _edge_public(row: NavFsmEdge) -> dict[str, Any]:
    return {
        "id": row.edge_id or "",
        "kind": row.kind or "nav",
        "from": row.from_state or "",
        "to": row.to_state or "",
        "guard": dict(row.guard or {}),
        "execute": dict(row.execute or {}),
        "effect_assert": dict(row.effect_assert or {}),
        "on_fail": dict(row.on_fail or {}),
        "scroll_into_view": dict(row.scroll_into_view or {}),
    }


def _doc_public(fsm: NavFsm, states: list[NavFsmState], edges: list[NavFsmEdge]) -> dict[str, Any]:
    return {
        "app_id": fsm.app_id or "",
        "project_id": fsm.project_id or "",
        "version": fsm.version or DEFAULT_VERSION,
        "meta": dict(fsm.meta or {}),
        "test_data": dict(fsm.test_data or {}),
        "updated_by": fsm.updated_by or "",
        "updated_at": int(fsm.updated_at or 0),
        "states": [_state_public(s) for s in states],
        "edges": [_edge_public(e) for e in edges],
    }


# ---------------- 读 ----------------


def read_raw(app_id: str, *, version: str = DEFAULT_VERSION) -> Optional[dict[str, Any]]:
    """原样读一行，**不做任何校验**。Studio 要能打开还带 `__CALIBRATE__` 的草稿去补录，
    所以编辑口不能用 `load()` —— 那是 runtime 门禁，草稿本来就该被它拒。"""
    aid = str(app_id or "").strip()
    if not aid:
        return None
    from mino_nexus.core.database import SessionLocal, ensure_db

    ensure_db()
    db = SessionLocal()
    try:
        fsm = (
            db.query(NavFsm)
            .filter(NavFsm.app_id == aid, NavFsm.version == (version or DEFAULT_VERSION))
            .one_or_none()
        )
        if fsm is None:
            return None
        states = db.query(NavFsmState).filter(NavFsmState.fsm_id == fsm.id).all()
        edges = db.query(NavFsmEdge).filter(NavFsmEdge.fsm_id == fsm.id).all()
        return _doc_public(fsm, states, edges)
    finally:
        db.close()


def load_with_reason(
    app_id: str,
    *,
    version: str = DEFAULT_VERSION,
    expected_account_id: str = "",
) -> tuple[Optional[dict[str, Any]], str]:
    """读一份可用配置。返回 `(doc, reason)`；`doc is None` 时 reason 说明为什么不可用。"""
    aid = str(app_id or "").strip()
    if not aid:
        return None, "app_id 为空"

    from mino_nexus.core.database import SessionLocal, ensure_db

    ensure_db()
    db = SessionLocal()
    try:
        fsm = (
            db.query(NavFsm)
            .filter(NavFsm.app_id == aid, NavFsm.version == (version or DEFAULT_VERSION))
            .one_or_none()
        )
        if fsm is None:
            return None, f"app_id={aid} 没有 nav_fsm 配置"
        states = db.query(NavFsmState).filter(NavFsmState.fsm_id == fsm.id).all()
        edges = db.query(NavFsmEdge).filter(NavFsmEdge.fsm_id == fsm.id).all()
        doc = _doc_public(fsm, states, edges)
    finally:
        db.close()

    reason = _scope_reason(doc, expected_account_id=expected_account_id)
    if reason:
        return None, reason
    try:
        validate_nav_fsm(doc)
    except NavFsmInvalid as exc:
        return None, str(exc)
    return doc, ""


def load(
    app_id: str,
    *,
    version: str = DEFAULT_VERSION,
    expected_account_id: str = "",
) -> Optional[dict[str, Any]]:
    doc, _ = load_with_reason(app_id, version=version, expected_account_id=expected_account_id)
    return doc


def _scope_reason(doc: dict[str, Any], *, expected_account_id: str = "") -> str:
    """§11.4 三元绑定：project_id 对得上 apps、account_id 对得上跑批租号。"""
    aid = str(doc.get("app_id") or "")
    want_project = str(doc.get("project_id") or "").strip()
    try:
        from mino_nexus.services import project_store as ps

        app = ps.find_app(aid)
    except Exception:  # noqa: BLE001 — 查不到项目表不该让跑批崩
        app = None
    if app is not None:
        actual = str(app.get("project_id") or "").strip()
        if want_project and actual and want_project != actual:
            return (
                f"nav_fsm.project_id={want_project} 与 apps.project_id={actual} 不一致，"
                "配置挂错了 app"
            )

    want_account = str(expected_account_id or "").strip()
    if not want_account:
        return ""
    calib = doc.get("meta") or {}
    calib = calib.get("hierarchy_calibration") if isinstance(calib, dict) else {}
    got = str((calib or {}).get("account_id") or "").strip()
    if got and got != want_account:
        return (
            f"校准账号 {got} 与本次租号 {want_account} 不一致："
            "hierarchy 是另一个账号采的，须重新 walkthrough 并重录配置（§11.4）"
        )
    return ""


def list_apps() -> list[dict[str, Any]]:
    """Console/Studio 列表用。只出摘要，不出 states/edges 正文。"""
    from mino_nexus.core.database import SessionLocal, ensure_db

    ensure_db()
    db = SessionLocal()
    try:
        rows = db.query(NavFsm).order_by(NavFsm.app_id, NavFsm.version).all()
        return [
            {
                "app_id": r.app_id or "",
                "project_id": r.project_id or "",
                "version": r.version or DEFAULT_VERSION,
                "updated_by": r.updated_by or "",
                "updated_at": int(r.updated_at or 0),
                "state_count": db.query(NavFsmState).filter(NavFsmState.fsm_id == r.id).count(),
                "edge_count": db.query(NavFsmEdge).filter(NavFsmEdge.fsm_id == r.id).count(),
            }
            for r in rows
        ]
    finally:
        db.close()


# ---------------- 写 ----------------


def save(
    app_id: str,
    body: dict[str, Any],
    *,
    updated_by: str = "",
    allow_calibrate: bool = False,
) -> dict[str, Any]:
    """整份替换（states / edges 全量重写）。校验不过直接抛，由调用方转 422。"""
    aid = str(app_id or "").strip()
    if not aid:
        raise NavFsmInvalid("app_id 为空")

    doc = dict(body or {})
    doc["app_id"] = aid
    if not str(doc.get("project_id") or "").strip():
        doc["project_id"] = _project_of(aid)
    version = str(doc.get("version") or DEFAULT_VERSION).strip() or DEFAULT_VERSION
    doc["version"] = version

    validate_doc(doc, allow_calibrate=allow_calibrate or version == DRAFT_VERSION)
    reason = _scope_reason(doc)
    if reason:
        raise NavFsmInvalid(reason)

    from mino_nexus.core.database import SessionLocal, ensure_db

    ensure_db()
    db = SessionLocal()
    try:
        fsm = (
            db.query(NavFsm)
            .filter(NavFsm.app_id == aid, NavFsm.version == version)
            .one_or_none()
        )
        if fsm is None:
            fsm = NavFsm(app_id=aid, version=version)
            db.add(fsm)
            db.flush()
        fsm.project_id = str(doc.get("project_id") or "")
        fsm.meta = dict(doc.get("meta") or {})
        fsm.test_data = dict(doc.get("test_data") or {})
        fsm.updated_by = str(updated_by or doc.get("updated_by") or "")
        fsm.updated_at = int(time.time())

        db.query(NavFsmState).filter(NavFsmState.fsm_id == fsm.id).delete()
        db.query(NavFsmEdge).filter(NavFsmEdge.fsm_id == fsm.id).delete()
        for st in doc.get("states") or []:
            db.add(
                NavFsmState(
                    fsm_id=fsm.id,
                    state_id=str(st.get("id") or st.get("state_id") or ""),
                    kind=str(st.get("kind") or "page"),
                    identify=dict(st.get("identify") or {}),
                    guards=dict(st.get("guards") or {}),
                    wiki_ref=str(st.get("wiki_ref") or ""),
                    entry=1 if st.get("entry") else 0,
                    role=str(st.get("role") or ""),
                )
            )
        for ed in _nav_edges_only(doc.get("edges") or []):
            db.add(
                NavFsmEdge(
                    fsm_id=fsm.id,
                    edge_id=str(ed.get("id") or ed.get("edge_id") or ""),
                    kind=str(ed.get("kind") or "nav"),
                    from_state=str(ed.get("from") or ed.get("from_state") or ""),
                    to_state=str(ed.get("to") or ed.get("to_state") or ""),
                    guard=dict(ed.get("guard") or {}),
                    execute=dict(ed.get("execute") or {}),
                    effect_assert=dict(ed.get("effect_assert") or {}),
                    on_fail=dict(ed.get("on_fail") or {}),
                    scroll_into_view=dict(ed.get("scroll_into_view") or {}),
                )
            )
        db.commit()
        states = db.query(NavFsmState).filter(NavFsmState.fsm_id == fsm.id).all()
        edges = db.query(NavFsmEdge).filter(NavFsmEdge.fsm_id == fsm.id).all()
        return _doc_public(fsm, states, edges)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def delete(app_id: str, *, version: str = DEFAULT_VERSION) -> bool:
    from mino_nexus.core.database import SessionLocal, ensure_db

    ensure_db()
    db = SessionLocal()
    try:
        fsm = (
            db.query(NavFsm)
            .filter(NavFsm.app_id == str(app_id or "").strip(), NavFsm.version == version)
            .one_or_none()
        )
        if fsm is None:
            return False
        db.query(NavFsmState).filter(NavFsmState.fsm_id == fsm.id).delete()
        db.query(NavFsmEdge).filter(NavFsmEdge.fsm_id == fsm.id).delete()
        db.delete(fsm)
        db.commit()
        return True
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def save_draft(app_id: str, body: dict[str, Any], *, updated_by: str = "") -> dict[str, Any]:
    """草稿进 `nav_fsm` 表（version=draft），允许 `__CALIBRATE__`。"""
    doc = dict(body or {})
    doc["version"] = DRAFT_VERSION
    return save(app_id, doc, updated_by=updated_by, allow_calibrate=True)


def read_draft(app_id: str) -> Optional[dict[str, Any]]:
    return read_raw(app_id, version=DRAFT_VERSION)


def promote_draft(app_id: str, *, updated_by: str = "") -> dict[str, Any]:
    """draft 行 → v1 正式发布行。"""
    draft = read_draft(app_id)
    if draft is None:
        raise NavFsmInvalid(f"app_id={app_id} 没有待发布的草稿（version={DRAFT_VERSION}）")
    return promote_prepared(app_id, draft, updated_by=updated_by)


def promote_prepared(app_id: str, body: dict[str, Any], *, updated_by: str = "") -> dict[str, Any]:
    """把已通过校验的内存文档直接写入 v1，避免 promote 时读到陈旧 draft。"""
    prepared = {k: v for k, v in dict(body or {}).items() if not str(k).startswith("_")}
    prepared["version"] = DEFAULT_VERSION
    saved = save(app_id, prepared, updated_by=updated_by)
    try:
        delete(app_id, version=DRAFT_VERSION)
    except Exception:  # noqa: BLE001 — 正式版已写入，草稿删失败不阻断
        pass
    return saved


def _project_of(app_id: str) -> str:
    try:
        from mino_nexus.services import project_store as ps

        app = ps.find_app(app_id)
        return str((app or {}).get("project_id") or "")
    except Exception:  # noqa: BLE001
        return ""

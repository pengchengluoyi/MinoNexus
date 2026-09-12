"""v2.5 从被动采集样本生成 NavFSM 字段候选（人工审核后合并进 draft）。"""
from __future__ import annotations

import hashlib
import re
from typing import Any

from mino_nexus.services import nav_calibration_store as calib
from mino_nexus.services import nav_capture_store as capture
from mino_nexus.services import nav_candidates_store as cand_store
from mino_nexus.services.nav_fsm_humanize import humanize_pending_list
from mino_nexus.services.nav_fsm_store import CALIBRATE_MARK

_MIN_EVIDENCE = 1
# 候选不做「概率置信」自动接受；只按证据次数排序，人工或显式批量接受。
_AUTO_ACCEPT_MIN = 2.0
_NOISE_RESOURCES = frozenset(
    {
        "action_bar_root",
        "battery",
        "status_bar",
        "status_bar_container",
        "navigationBarBackground",
        "android:id/content",
        "content",
        "clock",
        "container",
        "cutout_space_view",
        "decor_content_parent",
        "focused_notifi_start",
        "focused_parent",
        "hollow_battery_image",
        "battery_charge_out_image",
        "battery_icon_container",
    }
)
_NOISE_RESOURCE_PREFIXES = ("battery", "status_bar", "notification", "system_ui")


_TEMPLATE_SCREEN_IDS = frozenset({"page.home", "page.list", "page.detail", "dialog.confirm"})
_SKIP_LANDMARK_RE = re.compile(r"协议|隐私条款|隐私政策|已仔细阅读|同意《")
_LOGIN_LANDMARK_RE = re.compile(r"登录|手机号|验证码|密码|注册|短信")
_TIME_LANDMARK_RE = re.compile(r"^\d{1,2}:\d{2}$")
_DIGIT_LANDMARK_RE = re.compile(r"^\d{1,4}$")
_DIALOG_HINTS = frozenset({"确认", "取消", "提示", "确定", "知道了", "关闭"})
_MIN_CLUSTER_TURNS = 1
_MIN_EDGE_EVIDENCE = 1


def _is_noise_resource(rid: str) -> bool:
    raw = str(rid or "").strip()
    if not raw:
        return True
    base = raw.split("/")[-1].replace("\\", "/").split("/")[-1]
    if base in _NOISE_RESOURCES or base.startswith("android:id/"):
        return True
    return any(base.startswith(p) for p in _NOISE_RESOURCE_PREFIXES)


def _is_noise_candidate(row: dict[str, Any]) -> bool:
    kind = str(row.get("kind") or "")
    if kind not in ("resource_id", "feedback_resource"):
        return False
    val = str(row.get("proposed_value") or "").strip()
    if not val:
        return True
    raw = val.replace("\\", "")
    return _is_noise_resource(raw)


def filter_noise_candidates(batch: dict[str, Any]) -> dict[str, Any]:
    out = dict(batch or {})
    rows = [r for r in (out.get("candidates") or []) if not _is_noise_candidate(r)]
    out["candidates"] = rows
    out["pending_count"] = sum(1 for r in rows if str(r.get("status") or "") == "pending")
    return out


def _cid(prefix: str, raw: str) -> str:
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]
    return f"{prefix}.{digest}"


def compile_from_captures(app_id: str, *, min_evidence: int = _MIN_EVIDENCE) -> dict[str, Any]:
    samples = list(capture.iter_samples(app_id, limit_sessions=40, limit_turns=400))
    landmarks = capture.extract_landmark_stats(samples)
    resources = capture.extract_resource_stats(samples)

    candidates: list[dict[str, Any]] = []
    draft = calib.read_draft(app_id)

    for text, count in landmarks[:80]:
        if count < min_evidence:
            continue
        candidates.append(
            {
                "candidate_id": _cid("landmark", text),
                "kind": "text_landmark",
                "path": "states[].identify.required[].any",
                "proposed_value": text,
                "evidence_count": count,
                "status": "accepted",
            }
        )

    for rid, count in resources[:60]:
        if count < min_evidence or _is_noise_resource(rid):
            continue
        candidates.append(
            {
                "candidate_id": _cid("resource", rid),
                "kind": "resource_id",
                "path": "states[].guards[].detect.match_any[].resource_id_regex",
                "proposed_value": re.escape(rid),
                "evidence_count": count,
                "status": "accepted",
            }
        )

    candidates = _dedupe_for_human_review(candidates)

    batch = cand_store.append_candidates(
        app_id,
        candidates,
        source="capture_compile",
    )
    batch["sample_turns"] = len(samples)
    batch = filter_noise_candidates(batch)
    for row in batch.get("candidates") or []:
        if str(row.get("status") or "") == "pending":
            row["status"] = "accepted"
    batch["pending_count"] = 0
    cand_store.save_batch(app_id, batch)
    return batch


def _dedupe_for_human_review(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """去掉占位符填充类候选（发布时自动处理），同类同值只留一条。"""
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        kind = str(row.get("kind") or "")
        if kind == "fill_calibrate":
            continue
        val = str(row.get("proposed_value") or "").strip()
        if not val:
            continue
        key = (kind, val)
        if key in seen:
            continue
        seen.add(key)
        row["review_tier"] = "human"
        out.append(row)
    return out


def compile_from_feedback(
    app_id: str,
    *,
    session_id: str,
    turn_id: int,
    kind: str,
    note: str = "",
) -> dict[str, Any]:
    """失败点反馈 → 定点候选（只写候选包，不进正式库）。"""
    turn = capture.read_turn(app_id, session_id, int(turn_id))
    if not turn:
        raise ValueError("找不到该 turn 的采集样本")

    landmarks = capture.extract_landmark_stats([turn])
    resources = capture.extract_resource_stats([turn])
    rows: list[dict[str, Any]] = []
    k = str(kind or "").strip().lower()

    if k in ("localize_wrong", "edge_fail", "missing_edge"):
        for text, count in landmarks[:8]:
            rows.append(
                {
                    "candidate_id": _cid("fb.landmark", f"{session_id}:{turn_id}:{text}"),
                    "kind": "feedback_landmark",
                    "path": "states[].identify.required",
                    "proposed_value": text,
                    "confidence": 0.5,
                    "evidence_count": count,
                    "status": "pending",
                    "feedback": {"session_id": session_id, "turn_id": turn_id, "kind": k, "note": note},
                }
            )
    elif k in ("guard_false_positive", "guard_false_negative"):
        for rid, count in resources[:6]:
            rows.append(
                {
                    "candidate_id": _cid("fb.rid", f"{session_id}:{turn_id}:{rid}"),
                    "kind": "feedback_resource",
                    "path": "states[].guards[].detect",
                    "proposed_value": re.escape(rid),
                    "confidence": 0.48,
                    "evidence_count": count,
                    "status": "pending",
                    "feedback": {"session_id": session_id, "turn_id": turn_id, "kind": k, "note": note},
                }
            )

    if not rows and note:
        rows.append(
            {
                "candidate_id": _cid("fb.note", f"{session_id}:{turn_id}:{note}"),
                "kind": "feedback_note",
                "path": "",
                "proposed_value": note,
                "confidence": 0.3,
                "evidence_count": 1,
                "status": "pending",
                "feedback": {"session_id": session_id, "turn_id": turn_id, "kind": k, "note": note},
            }
        )

    batch = cand_store.append_candidates(app_id, rows, source=f"feedback:{k}")
    batch["feedback_turn"] = {"session_id": session_id, "turn_id": turn_id, "kind": k}
    cand_store.save_batch(app_id, batch)
    return batch


def apply_accepted_to_draft(app_id: str) -> dict[str, Any]:
    """把已 accept 的候选合并进 draft（仍可能含未审字段；不进正式库）。"""
    accepted = cand_store.accepted_candidates(app_id)
    if not accepted:
        raise ValueError("没有已接受的候选")

    draft = calib.read_draft(app_id)
    if not draft:
        from mino_nexus.services import nav_fsm_template as tpl
        from mino_nexus.services import project_store as ps

        app = ps.find_app(app_id)
        draft = tpl.build_template(app_id, project_id=str((app or {}).get("project_id") or ""))

    applied = 0
    for row in accepted:
        path = str(row.get("path") or "")
        val = row.get("proposed_value")
        kind = str(row.get("kind") or "")
        if kind == "fill_calibrate" and path and val is not None and _set_at_path(draft, path, val):
            applied += 1
        elif kind in ("text_landmark", "feedback_landmark") and val:
            _append_landmark_to_first_state(draft, str(val))
            applied += 1

    saved = calib.save_draft(app_id, draft)
    from mino_nexus.services import nav_fsm_template as tpl

    return {"draft": saved, "pending": tpl.pending_marks(saved), "applied": applied}


def _set_at_path(doc: dict[str, Any], path: str, value: Any) -> bool:
    """按 `states[0].identify...` 路径写入；目标含 `__CALIBRATE__` 才替换。"""
    cur: Any = doc
    tokens = _path_tokens(path)
    if not tokens:
        return False
    for key, idx in tokens[:-1]:
        if key is not None:
            if not isinstance(cur, dict) or key not in cur:
                return False
            cur = cur[key]
        else:
            if not isinstance(cur, list) or idx is None or idx >= len(cur):
                return False
            cur = cur[idx]
    last_key, last_idx = tokens[-1]
    if last_key is not None:
        if not isinstance(cur, dict) or last_key not in cur:
            return False
        old = cur[last_key]
        if isinstance(old, str) and CALIBRATE_MARK in old:
            cur[last_key] = value
            return True
        if isinstance(old, list):
            cur[last_key] = [value if x == CALIBRATE_MARK else x for x in old]
            return True
        return False
    if not isinstance(cur, list) or last_idx is None or last_idx >= len(cur):
        return False
    old = cur[last_idx]
    if isinstance(old, str) and CALIBRATE_MARK in old:
        cur[last_idx] = value
        return True
    return False


def _path_tokens(path: str) -> list[tuple[str | None, int | None]]:
    tokens: list[tuple[str | None, int | None]] = []
    for part in str(path or "").split("."):
        if not part:
            continue
        while part:
            if "[" in part:
                head, rest = part.split("[", 1)
                if head:
                    tokens.append((head, None))
                idx_s, rest2 = rest.split("]", 1)
                tokens.append((None, int(idx_s)))
                part = rest2.lstrip(".")
            else:
                tokens.append((part, None))
                part = ""
    return tokens


def _append_landmark_to_first_state(doc: dict[str, Any], text: str) -> None:
    states = doc.get("states") or []
    if not states:
        return
    st = states[0]
    identify = st.get("identify") or {}
    required = list(identify.get("required") or [])
    if not required:
        required = [{"signal": "text_landmarks", "any": [], "none_of": []}]
    block = required[0]
    any_list = list(block.get("any") or [])
    if text not in any_list and CALIBRATE_MARK in any_list:
        any_list = [text if x == CALIBRATE_MARK else x for x in any_list]
    elif text not in any_list:
        any_list.append(text)
    block["any"] = any_list
    identify["required"] = required
    st["identify"] = identify
    states[0] = st
    doc["states"] = states


def _primary_landmark(state: dict[str, Any]) -> str:
    identify = state.get("identify") or {}
    required = identify.get("required")
    blocks = required if isinstance(required, list) else ([required] if required else [])
    for block in blocks:
        if not isinstance(block, dict):
            continue
        for text in block.get("any") or []:
            val = str(text or "").strip()
            if val and val != CALIBRATE_MARK:
                return val
    return str(state.get("id") or "?")


def _is_noise_landmark(text: str) -> bool:
    val = str(text or "").strip()
    if len(val) < 2 or len(val) > 48:
        return True
    if _SKIP_LANDMARK_RE.search(val):
        return True
    if _TIME_LANDMARK_RE.match(val):
        return True
    if _DIGIT_LANDMARK_RE.match(val):
        return True
    return False


def _all_landmarks(sample: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for node in sample.get("nodes") or []:
        text = str(node.get("text") or "").strip()
        if _is_noise_landmark(text):
            continue
        if text not in out:
            out.append(text)
        if len(out) >= 8:
            break
    return out


def _chrome_texts(samples: list[dict[str, Any]], *, min_ratio: float = 0.55) -> set[str]:
    """在多屏反复出现的底栏/顶栏文案，不能用来区分页面。"""
    counts: dict[str, int] = {}
    total = max(1, len(samples))
    for sample in samples:
        seen: set[str] = set()
        for node in sample.get("nodes") or []:
            text = str(node.get("text") or "").strip()
            if _is_noise_landmark(text):
                continue
            seen.add(text)
        for text in seen:
            counts[text] = counts.get(text, 0) + 1
    threshold = max(3, int(total * min_ratio))
    chrome: set[str] = {t for t, c in counts.items() if c >= threshold}
    for text, count in counts.items():
        if text.startswith("#") and count >= max(2, threshold - 1):
            chrome.add(text)
    return chrome


def _discriminative_landmarks(sample: dict[str, Any], chrome: set[str]) -> list[str]:
    all_texts = _all_landmarks(sample)
    if not all_texts:
        return []
    primary = next((t for t in all_texts if t not in chrome), all_texts[0])
    out = [primary]
    for text in all_texts:
        if text == primary or text in chrome:
            continue
        out.append(text)
        if len(out) >= 6:
            break
    return out


def _is_login_screen(landmarks: list[str]) -> bool:
    return any(_LOGIN_LANDMARK_RE.search(t) for t in landmarks)


def _screen_kind(primary: str, landmarks: list[str], *, is_login: bool = False) -> str:
    if is_login:
        return "page"
    if any(h in primary for h in _DIALOG_HINTS):
        return "dialog"
    return "page"


def _cluster_non_chrome(landmarks: list[str], chrome: set[str]) -> list[str]:
    return [t for t in landmarks if t not in chrome]


def _is_weak_main_cluster(data: dict[str, Any], chrome: set[str]) -> bool:
    """底栏 Tab / 全局 chrome 不能单独成页。"""
    primary = str(data.get("primary") or "")
    non_chrome = _cluster_non_chrome(list(data.get("landmarks") or []), chrome)
    if primary in chrome:
        return len(non_chrome) < 2
    return len(non_chrome) < 1


def _safe_state_id(primary: str, used: set[str]) -> str:
    slug = re.sub(r"\s+", "_", primary.strip())[:20]
    slug = re.sub(r"[^\w\u4e00-\u9fff.-]", "", slug) or "screen"
    sid = f"page.{slug}"
    n = 2
    while sid in used:
        sid = f"page.{slug}_{n}"
        n += 1
    used.add(sid)
    return sid


def build_fsm_from_captures(app_id: str, *, project_id: str = "") -> dict[str, Any] | None:
    """从采集轨迹归纳「真实页面」；不把每次滑动都连成导航边，登录页单独隔离。"""
    from mino_nexus.services import nav_fsm_template as tpl

    from mino_nexus.services.nav_target_scope import resolve_app_target_scope

    ordered, cap_meta = capture.iter_cumulative_turns(app_id, limit_sessions=40, limit_turns=800)
    scope = resolve_app_target_scope(app_id, platform="", target_package="")
    synth_turns = capture.synthesis_turns(ordered, scope=scope)
    if len(synth_turns) < 2:
        return None

    from mino_nexus.services.nav_synthesis import build_from_tab_bar

    tab_doc = build_from_tab_bar(
        app_id,
        ordered,
        project_id=project_id,
        session_id=str(cap_meta.get("latest_session_id") or ""),
        capture_meta=cap_meta,
    )
    if tab_doc:
        return tab_doc

    chrome = _chrome_texts(synth_turns)
    turn_rows: list[dict[str, Any] | None] = []
    for sample in synth_turns:
        landmarks = _discriminative_landmarks(sample, chrome)
        if not landmarks:
            turn_rows.append(None)
            continue
        primary = landmarks[0]
        if _is_noise_landmark(primary):
            turn_rows.append(None)
            continue
        turn_rows.append(
            {
                "primary": primary,
                "landmarks": landmarks,
                "is_login": _is_login_screen(landmarks),
            }
        )

    clusters: dict[str, dict[str, Any]] = {}
    for i, row in enumerate(turn_rows):
        if not row:
            continue
        primary = str(row["primary"])
        bucket = clusters.setdefault(
            primary,
            {
                "primary": primary,
                "landmarks": list(row["landmarks"]),
                "is_login": bool(row["is_login"]),
                "first": i,
                "count": 0,
            },
        )
        bucket["count"] += 1
        for t in row["landmarks"]:
            if t not in bucket["landmarks"]:
                bucket["landmarks"].append(t)

    valid: dict[str, dict[str, Any]] = {}
    for k, v in clusters.items():
        if v["is_login"]:
            valid[k] = v
            continue
        if v["count"] < _MIN_CLUSTER_TURNS:
            continue
        if _is_weak_main_cluster(v, chrome):
            continue
        valid[k] = v
    mains = {k: v for k, v in valid.items() if not v["is_login"]}
    logins = {k: v for k, v in valid.items() if v["is_login"]}
    if not mains and len(logins) < 2:
        return None
    if not mains:
        mains = logins
        logins = {}

    def _home_score(item: tuple[str, dict[str, Any]]) -> tuple[int, int, int]:
        primary, data = item
        non_chrome = len(_cluster_non_chrome(list(data.get("landmarks") or []), chrome))
        return (non_chrome, int(data["count"]), -int(data["first"]))

    home_primary = max(mains.items(), key=_home_score)[0]
    main_order = sorted(
        [p for p in mains if p != home_primary],
        key=lambda p: mains[p]["first"],
    )

    screens: list[dict[str, Any]] = []
    used_ids: set[str] = set()
    primary_to_id: dict[str, str] = {}

    def _add_screen(data: dict[str, Any]) -> None:
        primary = str(data["primary"])
        sid = _safe_state_id(primary, used_ids)
        landmarks = [t for t in data["landmarks"] if not _is_noise_landmark(t) and t not in chrome][:5]
        if primary not in landmarks:
            landmarks = [primary, *landmarks]
        if len(landmarks) < 2:
            landmarks = landmarks + [t for t in _cluster_non_chrome(list(data["landmarks"] or []), chrome) if t not in landmarks][:3]
        screens.append(
            {
                "id": sid,
                "kind": _screen_kind(primary, landmarks, is_login=bool(data["is_login"])),
                "landmarks": landmarks,
                "label": primary,
                "is_login": bool(data["is_login"]),
            }
        )
        primary_to_id[primary] = sid

    _add_screen({**mains[home_primary], "is_login": False})
    for p in main_order:
        _add_screen(mains[p])
    for p in sorted(logins, key=lambda x: logins[x]["first"]):
        _add_screen(logins[p])

    if len(screens) < 2:
        return None

    screen_rows = [(str(s["id"]), str(s["kind"])) for s in screens]
    doc = tpl.build_template(app_id, project_id=project_id, screens=screen_rows)

    for st in doc.get("states") or []:
        sid = str(st.get("id") or "")
        screen = next((s for s in screens if s["id"] == sid), None)
        if not screen:
            continue
        st["identify"] = {
            "required": [
                {
                    "signal": "text_landmarks",
                    "any": list(screen["landmarks"]),
                    "none_of": list(chrome)[:12],
                }
            ]
        }
        st["guards"] = {}
        if screen.get("is_login"):
            st["role"] = "login"

    transition_counts: dict[tuple[str, str], int] = {}
    for i in range(len(turn_rows) - 1):
        a, b = turn_rows[i], turn_rows[i + 1]
        if not a or not b:
            continue
        if a["is_login"] or b["is_login"]:
            continue
        pa, pb = str(a["primary"]), str(b["primary"])
        if pa == pb or pa not in mains or pb not in mains:
            continue
        if pa not in primary_to_id or pb not in primary_to_id:
            continue
        key = (pa, pb)
        transition_counts[key] = transition_counts.get(key, 0) + 1

    nav_edges: list[dict[str, Any]] = []
    for (pa, pb), cnt in sorted(transition_counts.items(), key=lambda x: (-x[1], x[0])):
        if cnt < _MIN_EDGE_EVIDENCE:
            continue
        src = primary_to_id[pa]
        dst = primary_to_id[pb]
        dst_screen = next((s for s in screens if s["id"] == dst), None)
        targets = list((dst_screen or {}).get("landmarks") or [])[:3] or [pb]
        nav_edges.append(
            {
                "id": f"edge.{src.split('.')[-1]}_to_{dst.split('.')[-1]}",
                "kind": "nav",
                "from": src,
                "to": dst,
                "guard": {},
                "execute": {"steps": ["tap_element"]},
                "effect_assert": {
                    "within_ms": 8000,
                    "require_any": [{"text_landmarks": targets}],
                    "require_none": [],
                    "state_delta": {},
                },
                "on_fail": {},
                "scroll_into_view": {},
            }
        )

    doc["edges"] = nav_edges
    meta = dict(doc.get("meta") or {})
    meta["synthesized_from_capture"] = True
    meta["capture_scope"] = "cumulative"
    meta["capture_sessions"] = int(cap_meta.get("sessions") or 0)
    meta["capture_sessions_used"] = int(cap_meta.get("sessions_used") or 0)
    meta["capture_turns"] = int(cap_meta.get("turns") or len(ordered))
    meta["capture_session_id"] = str(cap_meta.get("latest_session_id") or "")
    meta["synthesis_mode"] = "cluster_v2"
    meta["synthesis_note"] = (
        "页面由采集聚类生成；导航边仅保留连续出现≥2次的跳转。"
        "登录页不与主流程自动连边。橙色恢复边供跑批迷路时回到首页。"
    )
    doc["meta"] = meta
    return autofill_draft_from_captures(app_id, doc)


def _field_needs_fill(value: Any) -> bool:
    if value is None:
        return True
    text = str(value).strip()
    return not text or text == CALIBRATE_MARK or CALIBRATE_MARK in text


def scrub_calibrate_marks(
    node: Any,
    *,
    landmark: str = "",
    resource_re: str = "",
    path: str = "",
) -> Any:
    """递归清除残留 `__CALIBRATE__`（含子串），发布前最后一道门禁。"""
    if isinstance(node, str):
        if CALIBRATE_MARK not in node:
            return node
        if node == CALIBRATE_MARK:
            low = path.lower()
            if "resource" in low and resource_re:
                return re.escape(resource_re)
            if ".steps[" in low or low.endswith(".steps"):
                return "tap_element"
            return landmark
        return node.replace(CALIBRATE_MARK, landmark)
    if isinstance(node, dict):
        return {
            k: scrub_calibrate_marks(
                v,
                landmark=landmark,
                resource_re=resource_re,
                path=f"{path}.{k}" if path else str(k),
            )
            for k, v in node.items()
        }
    if isinstance(node, list):
        return [
            scrub_calibrate_marks(
                v,
                landmark=landmark,
                resource_re=resource_re,
                path=f"{path}[{i}]",
            )
            for i, v in enumerate(node)
        ]
    return node


def finalize_for_publish(app_id: str, draft: dict[str, Any]) -> dict[str, Any]:
    """发布前终态：采集填平 + 强制写入 hierarchy_calibration + 去掉 recover 边占位符。"""
    doc = autofill_draft_from_captures(app_id, draft)
    _, cap_meta = capture.iter_cumulative_turns(app_id, limit_sessions=40, limit_turns=80)
    samples = list(capture.iter_samples(app_id, limit_sessions=5, limit_turns=40))
    latest_session = str(cap_meta.get("latest_session_id") or "").strip()
    if not latest_session:
        sessions = capture.list_sessions(app_id, limit=1)
        latest_session = str((sessions[0] or {}).get("session_id") or "") if sessions else ""

    landmarks = [(t, c) for t, c in capture.extract_landmark_stats(samples) if not _SKIP_LANDMARK_RE.search(t)]
    resources = [r for r, _ in capture.extract_resource_stats(samples) if not _is_noise_resource(r)]
    top_text = landmarks[0][0] if landmarks else ""
    top_rid = resources[0] if resources else ""
    account = ""
    for sample in samples:
        aid = str(sample.get("account_id") or "").strip()
        if aid:
            account = aid
            break
    if not top_text:
        tab_bar = (doc.get("meta") or {}).get("tab_bar") or {}
        home_id = str(tab_bar.get("home_state_id") or "").strip()
        labels = tab_bar.get("labels") if isinstance(tab_bar.get("labels"), dict) else {}
        top_text = str(labels.get(home_id) or "").strip()

    evidence_base = f"nav/capture/{app_id}"
    meta = dict(doc.get("meta") or {})
    hc = dict(meta.get("hierarchy_calibration") or {})
    hc["calibration_id"] = latest_session or f"passive:{app_id}"
    hc["evidence_rel_path"] = f"{evidence_base}/{latest_session}" if latest_session else evidence_base
    hc["account_id"] = account or str(hc.get("account_id") or "").strip() or "passive_capture"
    if _field_needs_fill(hc.get("project_id")):
        hc["project_id"] = str(doc.get("project_id") or "")
    hc["hierarchy_format"] = "accessibility_json"
    hc["hierarchy_result_key"] = "nodes"
    hc["follow_filled_detectable"] = "text"
    hc.setdefault("guard_recheck_on_anchor_hit", True)
    if _field_needs_fill(hc.get("anchor_on_first_screen")):
        hc["anchor_on_first_screen"] = top_text
    meta["hierarchy_calibration"] = hc
    doc["meta"] = meta
    doc["edges"] = [
        e
        for e in (doc.get("edges") or [])
        if isinstance(e, dict) and str(e.get("kind") or "nav") == "nav"
    ]
    doc = scrub_calibrate_marks(doc, landmark=top_text, resource_re=top_rid)
    return ensure_unique_state_ids(doc)


def autofill_draft_from_captures(app_id: str, draft: dict[str, Any]) -> dict[str, Any]:
    """用被动采集样本自动填平草稿里的 `__CALIBRATE__`（v2.5 不再依赖 walkthrough 批次）。"""
    out = dict(draft or {})
    _, cap_meta = capture.iter_cumulative_turns(app_id, limit_sessions=40, limit_turns=800)
    samples = list(capture.iter_samples(app_id, limit_sessions=5, limit_turns=80))
    sessions = capture.list_sessions(app_id, limit=1)
    latest_session = str(cap_meta.get("latest_session_id") or "")
    if not latest_session and sessions:
        latest_session = str(sessions[0].get("session_id") or "")

    account = ""
    for sample in samples:
        aid = str(sample.get("account_id") or "").strip()
        if aid:
            account = aid
            break

    landmarks = [(t, c) for t, c in capture.extract_landmark_stats(samples) if not _SKIP_LANDMARK_RE.search(t)]
    resources = [r for r, _ in capture.extract_resource_stats(samples) if not _is_noise_resource(r)]
    top_text = landmarks[0][0] if landmarks else ""
    top_rid = resources[0] if resources else ""
    if not top_text:
        tab_bar = (out.get("meta") or {}).get("tab_bar") or {}
        home_id = str(tab_bar.get("home_state_id") or "").strip()
        labels = tab_bar.get("labels") if isinstance(tab_bar.get("labels"), dict) else {}
        top_text = str(labels.get(home_id) or "").strip()

    meta = dict(out.get("meta") or {})
    synthesized = bool(meta.get("synthesized_from_capture"))
    capture_turns = int(meta.get("capture_turns") or cap_meta.get("turns") or 0)
    hc = dict(meta.get("hierarchy_calibration") or {})
    evidence_base = f"nav/capture/{app_id}"
    if latest_session:
        if _field_needs_fill(hc.get("calibration_id")):
            hc["calibration_id"] = latest_session
        if _field_needs_fill(hc.get("evidence_rel_path")):
            hc["evidence_rel_path"] = f"{evidence_base}/{latest_session}"
    elif synthesized or capture_turns > 0 or cap_meta.get("turns"):
        if _field_needs_fill(hc.get("calibration_id")):
            hc["calibration_id"] = f"passive:{app_id}"
        if _field_needs_fill(hc.get("evidence_rel_path")):
            hc["evidence_rel_path"] = evidence_base
    if _field_needs_fill(hc.get("account_id")):
        hc["account_id"] = account or "passive_capture"
    pid = str(out.get("project_id") or hc.get("project_id") or "")
    if pid and _field_needs_fill(hc.get("project_id")):
        hc["project_id"] = pid
    if _field_needs_fill(hc.get("hierarchy_format")):
        hc["hierarchy_format"] = "accessibility_json"
    if _field_needs_fill(hc.get("hierarchy_result_key")):
        hc["hierarchy_result_key"] = "nodes"
    if _field_needs_fill(hc.get("follow_filled_detectable")):
        hc["follow_filled_detectable"] = "text"
    if _field_needs_fill(hc.get("anchor_on_first_screen")):
        hc["anchor_on_first_screen"] = top_text
    meta["hierarchy_calibration"] = hc
    out["meta"] = meta
    walked = _walk_replace_calibrate(out, landmark=top_text, resource_re=top_rid, skip_paths={"meta.hierarchy_calibration"})
    walked_meta = dict(walked.get("meta") or {})
    walked_meta["hierarchy_calibration"] = hc
    walked["meta"] = walked_meta
    if (walked.get("meta") or {}).get("synthesized_from_capture"):
        return walked
    return _assign_per_state_landmarks(walked, landmarks)


def _assign_per_state_landmarks(doc: dict[str, Any], landmarks: list[tuple[str, int]]) -> dict[str, Any]:
    """模板多屏时按序分配不同界面文字，避免每个节点都是同一条协议文案。"""
    texts: list[str] = []
    for text, _ in landmarks:
        val = str(text or "").strip()
        if not val or _SKIP_LANDMARK_RE.search(val):
            continue
        if val not in texts:
            texts.append(val)
    if not texts:
        return doc
    states = doc.get("states") or []
    for i, st in enumerate(states):
        if not isinstance(st, dict):
            continue
        primary = texts[i % len(texts)]
        extras = [t for t in texts if t != primary][:2]
        identify = dict(st.get("identify") or {})
        block = {"signal": "text_landmarks", "any": [primary, *extras], "none_of": []}
        identify["required"] = [block]
        st["identify"] = identify
        guards = st.get("guards") or {}
        if isinstance(guards, dict):
            for widget in guards.values():
                if not isinstance(widget, dict):
                    continue
                for state_def in (widget.get("states") or {}).values():
                    if not isinstance(state_def, dict):
                        continue
                    detect = state_def.get("detect") or {}
                    if isinstance(detect, dict):
                        match_any = detect.get("match_any") or []
                        for row in match_any:
                            if isinstance(row, dict) and "hierarchy_contains" in row:
                                row["hierarchy_contains"] = primary
    doc["states"] = states
    return doc


def _walk_replace_calibrate(
    node: Any,
    *,
    landmark: str,
    resource_re: str,
    path: str = "",
    skip_paths: set[str] | None = None,
) -> Any:
    cur = path or "<root>"
    if skip_paths and cur in skip_paths:
        return node
    if isinstance(node, str):
        if node != CALIBRATE_MARK:
            return node
        low = path.lower()
        if "resource_id" in low and resource_re:
            return re.escape(resource_re)
        if ".steps[" in low or low.endswith(".steps"):
            return "tap_element"
        return landmark
    if isinstance(node, dict):
        return {
            k: _walk_replace_calibrate(
                v,
                landmark=landmark,
                resource_re=resource_re,
                path=f"{path}.{k}" if path else k,
                skip_paths=skip_paths,
            )
            for k, v in node.items()
        }
    if isinstance(node, list):
        return [
            _walk_replace_calibrate(
                v,
                landmark=landmark,
                resource_re=resource_re,
                path=f"{path}[{i}]",
                skip_paths=skip_paths,
            )
            for i, v in enumerate(node)
        ]
    return node


def _tab_label_from_state(st: dict[str, Any]) -> str:
    identify = st.get("identify") or {}
    required = identify.get("required")
    blocks = required if isinstance(required, list) else ([required] if required else [])
    for block in blocks:
        if isinstance(block, dict) and block.get("signal") == "tab_bar":
            return str((block.get("match") or {}).get("selected") or "").strip()
    return ""


def _framework_kind_from_state(st: dict[str, Any]) -> str:
    identify = st.get("identify") or {}
    required = identify.get("required")
    blocks = required if isinstance(required, list) else ([required] if required else [])
    for block in blocks:
        if isinstance(block, dict) and block.get("signal") == "layout_framework":
            return str((block.get("match") or {}).get("kind") or "").strip()
    return ""


def ensure_unique_state_ids(doc: dict[str, Any]) -> dict[str, Any]:
    """保证 states.id 全局唯一；冲突时按 Tab/结构重新分配，并同步边的 from/to。"""
    from mino_nexus.services.nav_layout import sub_page_identity_from_state
    from mino_nexus.services.nav_layout import semantic_page_role
    from mino_nexus.services.nav_synthesis import _slug_semantic_sub, _slug_tab

    states_in = [dict(s) for s in (doc.get("states") or []) if isinstance(s, dict)]
    used: set[str] = set()
    id_map: dict[str, str] = {}
    new_states: list[dict[str, Any]] = []

    for st in states_in:
        row = dict(st)
        sid = str(row.get("id") or "").strip()
        if not sid or sid in used:
            old = sid
            tab = _tab_label_from_state(row)
            if row.get("entry") and tab:
                sid = _slug_tab(tab, used)
            else:
                tab_key, role, _chrome = sub_page_identity_from_state(row)
                if not role:
                    fw_match = None
                    req = (row.get("identify") or {}).get("required") or []
                    blocks = req if isinstance(req, list) else [req]
                    for block in blocks:
                        if isinstance(block, dict) and block.get("signal") == "layout_framework":
                            fw_match = block.get("match") or {}
                            break
                    role = semantic_page_role(fw_match)
                sid = _slug_semantic_sub(tab_key or tab or "tab", role or "main", used)
            if old and old != sid:
                id_map[old] = sid
        used.add(sid)
        row["id"] = sid
        new_states.append(row)

    def _remap(ref: str) -> str:
        val = str(ref or "").strip()
        while val in id_map:
            val = id_map[val]
        return val

    new_edges: list[dict[str, Any]] = []
    for ed in doc.get("edges") or []:
        if not isinstance(ed, dict):
            continue
        if str(ed.get("kind") or "nav") != "nav":
            continue
        row = dict(ed)
        src = _remap(str(row.get("from") or ""))
        dst = _remap(str(row.get("to") or ""))
        row["from"] = src
        row["to"] = dst
        if row.get("from_state") is not None:
            row["from_state"] = src
        if row.get("to_state") is not None:
            row["to_state"] = dst
        new_edges.append(row)

    out = dict(doc or {})
    out["states"] = new_states
    out["edges"] = new_edges
    meta = dict(out.get("meta") or {})
    tab_bar = dict(meta.get("tab_bar") or {})
    entries = tab_bar.get("entries")
    if isinstance(entries, list):
        tab_bar["entries"] = [_remap(str(e)) for e in entries]
    labels = tab_bar.get("labels")
    if isinstance(labels, dict):
        tab_bar["labels"] = {_remap(str(k)): v for k, v in labels.items()}
    home = str(tab_bar.get("home_state_id") or recover_home(meta) or "").strip()
    if home:
        tab_bar["home_state_id"] = _remap(home)
    meta["tab_bar"] = tab_bar
    recover = dict(meta.get("recover") or {})
    for key in ("default_state_id", "default_goal_state_id"):
        if recover.get(key):
            recover[key] = _remap(str(recover[key]))
    meta["recover"] = recover
    layout = meta.get("studio_layout")
    if isinstance(layout, dict) and isinstance(layout.get("states"), dict):
        layout["states"] = {_remap(str(k)): v for k, v in layout["states"].items()}
        meta["studio_layout"] = layout
    out["meta"] = meta
    return out


def recover_home(meta: dict[str, Any]) -> str:
    recover = meta.get("recover") if isinstance(meta.get("recover"), dict) else {}
    return str(recover.get("default_goal_state_id") or recover.get("default_state_id") or "")


def _union_graph_edges(
    *docs: dict[str, Any] | None,
    id_map: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    remap = id_map or {}
    seen_ids: set[str] = set()
    seen_pairs: set[tuple[str, str]] = set()
    out: list[dict[str, Any]] = []
    for doc in docs:
        for ed in (doc or {}).get("edges") or []:
            if not isinstance(ed, dict):
                continue
            if str(ed.get("kind") or "nav") != "nav":
                continue
            row = dict(ed)
            src = remap.get(str(row.get("from") or "").strip(), str(row.get("from") or "").strip())
            dst = remap.get(str(row.get("to") or "").strip(), str(row.get("to") or "").strip())
            eid = str(row.get("id") or "").strip()
            pair = (src, dst)
            if pair in seen_pairs:
                continue
            if eid and eid in seen_ids:
                continue
            if eid:
                seen_ids.add(eid)
            seen_pairs.add(pair)
            row["from"] = src
            row["to"] = dst
            out.append(row)
    return out


def merge_synthesized_doc(
    app_id: str,
    existing: dict[str, Any] | None,
    synthesized: dict[str, Any],
) -> dict[str, Any]:
    """累积合并：新采集追加节点/边；Tab 入口可被新证据整体替换（无页数上限）。"""
    from mino_nexus.services.nav_layout import sub_page_identity_from_state

    if not synthesized:
        return existing
    if not existing:
        return ensure_unique_state_ids(synthesized)

    mode = str((synthesized.get("meta") or {}).get("synthesis_mode") or "")
    if mode not in ("tab_bar", "tab_bar_layered"):
        used: set[str] = set()
        states: list[dict[str, Any]] = []
        for doc in (synthesized, existing):
            for st in (doc or {}).get("states") or []:
                if not isinstance(st, dict):
                    continue
                sid = str(st.get("id") or "").strip()
                if not sid or sid in used:
                    continue
                used.add(sid)
                states.append(dict(st))
        out = dict(synthesized)
        out["states"] = states
        out["edges"] = _union_graph_edges(synthesized, existing)
        return ensure_unique_state_ids(out)

    syn_meta = dict(synthesized.get("meta") or {})
    syn_tab = syn_meta.get("tab_bar") if isinstance(syn_meta.get("tab_bar"), dict) else {}
    syn_labels = syn_tab.get("labels") if isinstance(syn_tab.get("labels"), dict) else {}
    new_tab_names = {str(v).strip() for v in syn_labels.values() if str(v).strip()}
    old_entry_by_tab: dict[str, dict[str, Any]] = {}
    old_tab_names: set[str] = set()
    for st in existing.get("states") or []:
        if not isinstance(st, dict):
            continue
        tab = _tab_label_from_state(st)
        if tab:
            old_tab_names.add(tab)
        if st.get("entry") and tab:
            old_entry_by_tab[tab] = st
    replace_entries = len(new_tab_names) >= 2 and new_tab_names != old_tab_names
    removed_tab_prefixes: set[str] = set()
    if replace_entries:
        for tab in old_tab_names - new_tab_names:
            slug = re.sub(r"[^\w\u4e00-\u9fff]", "", tab.strip())[:16] or "tab"
            removed_tab_prefixes.add(f"page.tab_{slug}")

    old_sub_by_identity: dict[tuple[str, str, tuple[str, ...]], list[dict[str, Any]]] = {}
    for st in existing.get("states") or []:
        if not isinstance(st, dict) or st.get("entry"):
            continue
        identity = sub_page_identity_from_state(st)
        if identity[0]:
            old_sub_by_identity.setdefault(identity, []).append(st)

    id_map: dict[str, str] = {}
    used_ids: set[str] = set()
    merged_states: list[dict[str, Any]] = []

    for st in synthesized.get("states") or []:
        if not isinstance(st, dict):
            continue
        tab = _tab_label_from_state(st)
        row = dict(st)
        new_id = str(st.get("id") or "").strip()
        if st.get("entry") and tab and tab in old_entry_by_tab and not replace_entries:
            prev = old_entry_by_tab[tab]
            old_id = str(prev.get("id") or "").strip()
            if old_id:
                row["id"] = old_id
                if new_id and new_id != old_id:
                    id_map[new_id] = old_id
        elif not st.get("entry") and tab:
            identity = sub_page_identity_from_state(st)
            for prev_sub in old_sub_by_identity.get(identity, []):
                old_id = str(prev_sub.get("id") or "").strip()
                if not old_id or old_id in used_ids:
                    continue
                row["id"] = old_id
                if new_id and new_id != old_id:
                    id_map[new_id] = old_id
                break
        sid = str(row.get("id") or "").strip()
        if sid in used_ids:
            continue
        if sid:
            used_ids.add(sid)
        merged_states.append(row)

    for st in existing.get("states") or []:
        if not isinstance(st, dict):
            continue
        sid = str(st.get("id") or "").strip()
        if not sid or sid in used_ids:
            continue
        tab = _tab_label_from_state(st)
        if st.get("entry") and replace_entries and tab and tab not in new_tab_names:
            continue
        if replace_entries and removed_tab_prefixes:
            if tab and tab not in new_tab_names:
                continue
            if any(sid.startswith(prefix) for prefix in removed_tab_prefixes):
                continue
        used_ids.add(sid)
        merged_states.append(dict(st))

    valid_ids = {str(s.get("id") or "").strip() for s in merged_states if str(s.get("id") or "").strip()}
    out = dict(synthesized)
    out["states"] = merged_states
    out["edges"] = [
        ed
        for ed in _union_graph_edges(synthesized, existing, id_map=id_map)
        if str(ed.get("from") or "") in valid_ids and str(ed.get("to") or "") in valid_ids
    ]
    meta = dict(syn_meta)
    if len(new_tab_names) >= 2:
        meta["tab_bar"] = syn_tab
    else:
        old_tab = (existing.get("meta") or {}).get("tab_bar")
        if isinstance(old_tab, dict) and old_tab.get("labels"):
            meta["tab_bar"] = old_tab
    new_layout = meta.get("studio_layout") if isinstance(meta.get("studio_layout"), dict) else {}
    if new_layout.get("states"):
        meta["studio_layout"] = new_layout
    recover = meta.get("recover") if isinstance(meta.get("recover"), dict) else {}
    if not recover.get("default_state_id"):
        home_id = recover_home(meta) or str((meta.get("tab_bar") or {}).get("home_state_id") or "").strip()
        if not home_id:
            home_id = next((str(s.get("id") or "") for s in merged_states if s.get("entry")), "")
        if home_id:
            meta["recover"] = {**recover, "default_state_id": home_id}
    meta["capture_merge_at"] = int(__import__("time").time())
    out["meta"] = meta
    return ensure_unique_state_ids(out)


def prepare_publish(
    app_id: str,
    *,
    auto_accept_min: float = _AUTO_ACCEPT_MIN,
    promote: bool = False,
    updated_by: str = "",
) -> dict[str, Any]:
    """编译候选 → 自动接受高置信度 → 合并草稿 → 用采集填平 → 可选正式发布。"""
    from mino_nexus.services import nav_fsm_template as tpl
    from mino_nexus.services import project_store as ps

    draft = calib.read_draft(app_id)
    if not draft:
        project_id = ""
        try:
            app = ps.find_app(app_id)
            project_id = str((app or {}).get("project_id") or "")
        except Exception:
            project_id = ""
        draft = tpl.build_template(app_id, project_id=project_id)
        calib.save_draft(app_id, draft)

    batch = compile_from_captures(app_id)
    for row in batch.get("candidates") or []:
        cid = str(row.get("candidate_id") or "")
        if not cid:
            continue
        kind = str(row.get("kind") or "")
        proposed = str(row.get("proposed_value") or "")
        if kind == "resource_id" and _is_noise_resource(proposed):
            if str(row.get("status") or "") != "rejected":
                cand_store.review_candidate(app_id, cid, status="rejected", note="system_ui")
            continue
        if str(row.get("status") or "") == "pending":
            cand_store.review_candidate(app_id, cid, status="accepted", note="auto")

    applied = 0
    try:
        merge = apply_accepted_to_draft(app_id)
        applied = int(merge.get("applied") or 0)
    except Exception:
        merge = {}

    draft = calib.read_draft(app_id) or draft
    project_id = str(draft.get("project_id") or "")
    from mino_nexus.services import nav_fsm_store as fsm_store

    existing = draft
    try:
        pub = fsm_store.read_raw(app_id)
        if pub:
            existing = pub
    except Exception:
        pass
    from mino_nexus.services.nav_target_scope import resolve_app_target_scope

    synthesized = build_fsm_from_captures(app_id, project_id=project_id)
    if synthesized:
        draft = merge_synthesized_doc(app_id, existing, synthesized)
        scope = resolve_app_target_scope(app_id)
        if scope:
            meta = dict(draft.get("meta") or {})
            meta["target_scope"] = scope.as_dict()
            draft["meta"] = meta
        result_source = "capture_synthesis"
    else:
        result_source = "template_autofill"
    draft = finalize_for_publish(app_id, draft)
    saved = calib.save_draft(app_id, draft)
    pending = tpl.pending_marks(saved)

    result: dict[str, Any] = {
        "draft": saved,
        "pending": pending,
        "pending_count": len(pending),
        "human_summary": humanize_pending_list(pending),
        "ready_to_publish": len(pending) == 0,
        "candidates_compiled": len(batch.get("candidates") or []),
        "applied": applied,
        "source": result_source,
        "synthesized_from_capture": bool((saved.get("meta") or {}).get("synthesized_from_capture")),
        "screen_labels": [_primary_landmark(st) for st in (saved.get("states") or [])],
    }

    if promote:
        from_capture = bool(result.get("synthesized_from_capture"))
        if result["ready_to_publish"] or from_capture:
            try:
                result["published"] = fsm_store.promote_prepared(app_id, saved, updated_by=updated_by)
                result["runtime_ready"] = True
            except Exception as exc:  # noqa: BLE001
                result["publish_blocked"] = True
                result["runtime_ready"] = False
                result["publish_error"] = str(exc)
        else:
            result["publish_blocked"] = True
            result["runtime_ready"] = False
    return result

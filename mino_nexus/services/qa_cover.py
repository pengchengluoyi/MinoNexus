"""qa-process tick / 脑图 / 用例草稿。LLM 有 Key 就调，没有就结构化失败，绝不 501。"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from mino_nexus.ai.llm_client import call_chat_text, resolve_regression_provider
from mino_nexus.ai.job_slots import assemble_json_chat_slots
from mino_nexus.ai.prompt_render import JobRenderError, render
from mino_nexus.core.log import SLog
from mino_nexus.services import qa_process_jobs as cover_jobs

TAG = "QaCover"
LLM_JOBS = ("analyze_req", "draft_mindmap", "draft_cases", "propose_atlas")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _nid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _source_text(req: dict) -> str:
    for key in ("source_text", "text", "body", "content", "description", "title"):
        val = str(req.get(key) or "").strip()
        if val:
            return val
    return ""


def _ask_json(job: str, user: str) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    provider, gate = resolve_regression_provider()
    if not provider:
        return None, {"error": gate.get("reason") or "未配置可用的大模型", "enabled": False}
    cover_jobs.report(phase=job, label=f"正在调用模型：{job}")
    slots, flags = assemble_json_chat_slots(user_payload=user[:24000])
    try:
        messages, job_meta = render(job, slots, flags)
    except JobRenderError as e:
        return None, {"error": str(e)}
    call = job_meta.get("call") or {}
    parsed, meta = call_chat_text(
        provider=provider,
        messages=messages,
        temperature=float(call.get("temperature", 0.2)),
        max_tokens=int(call.get("max_tokens", 4096)),
        timeout_sec=int(call.get("timeout_sec", 90)),
        json_mode=bool(call.get("json_mode", True)),
    )
    if parsed is None:
        return None, meta
    return parsed, meta


def _apply_analyze(req: dict, payload: dict) -> dict:
    req = dict(req)
    understanding = req.get("understanding") if isinstance(req.get("understanding"), dict) else {}
    understanding = dict(understanding)
    for key in ("summary", "change_kind", "baseline", "delta", "ac", "points", "features", "journeys", "new_features", "keep_features", "exceptions", "surfaces", "risks", "impact", "hang", "atlas_create"):
        if key in payload:
            understanding[key] = payload[key]
    if payload.get("points"):
        understanding["points"] = payload["points"]
    understanding["updated_at"] = _now()
    req["understanding"] = understanding
    if payload.get("summary"):
        req["summary"] = payload["summary"]
    return req


def _apply_mindmap(req: dict, payload: dict) -> dict:
    req = dict(req)
    tree = payload if isinstance(payload, dict) else {}
    if "mindmap" in tree and isinstance(tree.get("mindmap"), dict):
        tree = tree["mindmap"]
    if not tree.get("children") and tree.get("title"):
        tree = {"title": tree.get("title"), "children": tree.get("children") or []}
    req["mindmap"] = tree
    req["mindmap_updated_at"] = _now()
    return req


def _norm_case(row: dict, index: int) -> dict:
    cid = str(row.get("case_id") or "").strip() or f"draft-{_nid('c')}"
    steps = row.get("steps")
    expected = row.get("expected")
    if isinstance(steps, str):
        steps_list = [s.strip() for s in steps.splitlines() if s.strip()]
        steps_raw = steps
    else:
        steps_list = [str(x) for x in (steps or []) if str(x).strip()]
        steps_raw = str(row.get("steps_raw") or "") or "\n".join(f"{i}. {x}" for i, x in enumerate(steps_list, 1))
    if isinstance(expected, str):
        expected_list = [s.strip() for s in expected.splitlines() if s.strip()]
        expected_raw = expected
    else:
        expected_list = [str(x) for x in (expected or []) if str(x).strip()]
        expected_raw = str(row.get("expected_raw") or "") or "\n".join(f"{i}. {x}" for i, x in enumerate(expected_list, 1))
    return {
        **row,
        "case_id": cid,
        "name": str(row.get("name") or row.get("title") or cid),
        "module": str(row.get("module") or ""),
        "precondition": str(row.get("precondition") or ""),
        "steps": steps_list,
        "expected": expected_list,
        "steps_raw": steps_raw,
        "expected_raw": expected_raw,
        "platform": str(row.get("platform") or "app"),
        "aspect": str(row.get("aspect") or "正向"),
        "point_ids": [str(x) for x in (row.get("point_ids") or []) if str(x).strip()],
        "index": index,
    }


def _apply_cases(req: dict, payload: dict, *, app_id: str = "", replace: bool = False) -> dict:
    from mino_nexus.services.case_store import upsert_cases

    req = dict(req)
    incoming = payload.get("cases") if isinstance(payload.get("cases"), list) else []
    rows = [_norm_case(x, i) for i, x in enumerate(incoming) if isinstance(x, dict)]
    rid = str(req.get("id") or "")
    if app_id:
        upsert_cases(app_id, rid, rows, replace=replace)
        req.pop("draft_cases", None)
    elif replace:
        req["draft_cases"] = rows
    else:
        existing = [x for x in (req.get("draft_cases") or []) if isinstance(x, dict)]
        seen = {str(x.get("case_id") or "") for x in existing}
        for row in rows:
            if row["case_id"] in seen:
                existing = [row if str(x.get("case_id")) == row["case_id"] else x for x in existing]
            else:
                existing.append(row)
                seen.add(row["case_id"])
        req["draft_cases"] = existing
    req["cases_updated_at"] = _now()
    return req


def run_llm_job(job: str, *, requirement: dict, cases: list | None = None, qa_process: dict | None = None) -> dict[str, Any]:
    req = dict(requirement or {})
    title = str(req.get("title") or req.get("external_id") or "需求")
    src = _source_text(req)
    if job == "analyze_req":
        user = json.dumps({"title": title, "source_text": src, "human_feedback": req.get("human_feedback") or ""}, ensure_ascii=False)
        parsed, meta = _ask_json(job, user)
        if parsed is None:
            return {"job": job, "status": "error", "error": meta.get("error") or "模型未返回分析", "engine": "llm", "meta": meta}
        return {"job": job, "status": "ok", "engine": "llm", "payload": parsed, "meta": meta}
    if job == "draft_mindmap":
        user = json.dumps({
            "title": title,
            "understanding": req.get("understanding") or {},
            "source_text": src,
            "previous_mindmap": req.get("mindmap") or {},
        }, ensure_ascii=False)
        parsed, meta = _ask_json(job, user)
        if parsed is None:
            return {"job": job, "status": "error", "error": meta.get("error") or "模型未返回脑图", "engine": "llm", "meta": meta}
        return {"job": job, "status": "ok", "engine": "llm", "payload": parsed, "meta": meta}
    if job == "draft_cases":
        user = json.dumps({
            "title": title,
            "understanding": req.get("understanding") or {},
            "mindmap": req.get("mindmap") or {},
            "existing_cases": (cases or [])[:40],
        }, ensure_ascii=False)
        parsed, meta = _ask_json(job, user)
        if parsed is None:
            return {"job": job, "status": "error", "error": meta.get("error") or "模型未返回用例", "engine": "llm", "meta": meta}
        return {"job": job, "status": "ok", "engine": "llm", "payload": parsed, "meta": meta}
    if job == "propose_atlas":
        user = json.dumps({
            "qa_process": {"app_atlas": (qa_process or {}).get("app_atlas"), "requirements": [(qa_process or {}).get("requirements") or []]},
            "requirement": {"id": req.get("id"), "title": title, "understanding": req.get("understanding") or {}},
        }, ensure_ascii=False)
        parsed, meta = _ask_json(job, user)
        if parsed is None:
            return {"job": job, "status": "error", "error": meta.get("error") or "模型未返回图谱建议", "engine": "llm", "meta": meta}
        return {"job": job, "status": "ok", "engine": "llm", "payload": parsed, "meta": meta}
    return {"job": job, "status": "error", "error": f"未知 job：{job}"}


def _default_jobs(req: dict, requested: list[str], *, app_id: str = "") -> list[str]:
    want = [j for j in requested if j in LLM_JOBS]
    if want:
        return want
    jobs: list[str] = []
    und = req.get("understanding") if isinstance(req.get("understanding"), dict) else {}
    if not und.get("points") and not und.get("summary"):
        jobs.append("analyze_req")
    if not req.get("mindmap"):
        jobs.append("draft_mindmap")
    drafts = req.get("draft_cases") if isinstance(req.get("draft_cases"), list) else []
    if not drafts:
        from mino_nexus.services.case_store import count_cases

        if not (app_id and count_cases(app_id, str(req.get("id") or ""))):
            jobs.append("draft_cases")
    return jobs or ["analyze_req"]


def tick(
    *,
    qa_process: dict,
    cases: list | None = None,
    requirement_id: str = "",
    user_note: str = "",
    force: bool = False,
    jobs: list | None = None,
    point_ids: list | None = None,
    rewrite_stubs: bool = False,
    replace_cases: bool = False,
    app_id: str = "",
) -> dict[str, Any]:
    del force, point_ids, rewrite_stubs
    doc = dict(qa_process or {})
    reqs = [dict(r) for r in (doc.get("requirements") or []) if isinstance(r, dict)]
    rid = str(requirement_id or "").strip()
    if rid:
        target = next((r for r in reqs if str(r.get("id") or "") == rid), None)
        if target is None:
            raise ValueError(f"找不到需求 {rid}")
        working = [target]
    else:
        working = reqs[:1]
        if not working:
            raise ValueError("还没有需求。请先在流程里建一条需求再推进。")
    actions: list[dict[str, Any]] = []
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    note = str(user_note or "").strip()
    cover_jobs.report(phase="running", label="开始推进", total=max(1, len(working) * 3))

    for req in working:
        job_list = _default_jobs(req, list(jobs or []), app_id=app_id)
        if note:
            req["human_feedback"] = note
        for job in job_list:
            cover_jobs.check()
            art = run_llm_job(job, requirement=req, cases=cases or [], qa_process=doc)
            meta = art.get("meta") if isinstance(art.get("meta"), dict) else {}
            for k in usage:
                usage[k] = int(usage.get(k) or 0) + int((meta.get(k) or 0) or (meta.get("usage") or {}).get(k) or 0)
            if art.get("status") != "ok":
                actions.append({"role": job, "action": "error", "detail": art.get("error") or "失败"})
                cover_jobs.report(inc=1, label=art.get("error") or job)
                continue
            payload = art.get("payload") if isinstance(art.get("payload"), dict) else {}
            if job == "analyze_req":
                req = _apply_analyze(req, payload)
            elif job == "draft_mindmap":
                req = _apply_mindmap(req, payload)
            elif job == "draft_cases":
                req = _apply_cases(req, payload, app_id=app_id, replace=replace_cases)
            elif job == "propose_atlas":
                patches = list(doc.get("atlas_patches") or [])
                patches.append({
                    "id": _nid("patch"),
                    "status": "pending",
                    "created_at": _now(),
                    "payload": payload,
                    "req_id": req.get("id"),
                })
                doc["atlas_patches"] = patches
            actions.append({"role": job, "action": "wrote", "detail": f"{job} 已写入"})
            cover_jobs.report(inc=1, label=f"已完成 {job}")
        for i, row in enumerate(reqs):
            if str(row.get("id") or "") == str(req.get("id") or ""):
                reqs[i] = req
                break
        doc["requirements"] = reqs
        doc["updated_at"] = _now()
        cover_jobs.save(doc)

    errors = [a for a in actions if a.get("action") == "error"]
    if errors and not [a for a in actions if a.get("action") == "wrote"]:
        # 全部失败：仍返回 done + qa_process，让 UI 展示 error 文案
        detail = errors[0].get("detail") or "推进失败"
        SLog.w(TAG, detail)
    return {
        "qa_process": doc,
        "actions": actions,
        "autonomy": doc.get("autonomy") if isinstance(doc.get("autonomy"), dict) else {},
        "usage": usage,
    }


def _count_mindmap_points(node: Any) -> int:
    if not isinstance(node, dict):
        return 0
    kids = [c for c in (node.get("children") or []) if isinstance(c, dict)]
    if not kids:
        text = str(node.get("text") or node.get("title") or "").strip()
        return 1 if text else 0
    return sum(_count_mindmap_points(c) for c in kids)


def import_cover(
    *,
    qa_process: dict,
    requirement_id: str = "",
    kind: str = "mindmap",
    text: str = "",
    filename: str = "",
    replace: bool = False,
    app_id: str = "",
) -> dict[str, Any]:
    from mino_nexus.services.cover_import import parse_cases

    doc = dict(qa_process or {})
    reqs = [dict(r) for r in (doc.get("requirements") or []) if isinstance(r, dict)]
    rid = str(requirement_id or "").strip()
    req = next((r for r in reqs if str(r.get("id") or "") == rid), None) if rid else (reqs[0] if reqs else None)
    if req is None:
        raise ValueError("请先选一条需求再导入")
    raw = str(text or "").strip()
    if not raw:
        raise ValueError("没有可导入的内容")
    kind = str(kind or "mindmap").strip().lower()
    stats: dict[str, Any]
    if kind in ("cases", "case", "csv"):
        incoming = parse_cases(raw, filename)
        if not incoming:
            raise ValueError("没有解析到用例")
        incoming = [{**c, "origin": "import", "locked": True} for c in incoming if isinstance(c, dict)]
        req = _apply_cases(req, {"cases": incoming}, app_id=app_id, replace=replace)
        total = 0
        if app_id:
            from mino_nexus.services.case_store import count_cases
            total = count_cases(app_id, str(req.get("id") or ""))
        else:
            total = len([x for x in (req.get("draft_cases") or []) if isinstance(x, dict)])
        stats = {
            "kind": "cases",
            "cases": len(incoming),
            "total": total,
            "action": "imported_cases",
        }
    else:
        parsed: Any = None
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = None
        tree = parsed if isinstance(parsed, dict) else {
            "title": str(req.get("title") or filename or "导入脑图"),
            "children": [{"id": _nid("n"), "text": ln.strip(), "kind": "point", "children": []} for ln in raw.splitlines() if ln.strip()],
        }
        req = _apply_mindmap(req, tree)
        stats = {
            "kind": "mindmap",
            "points": _count_mindmap_points(req.get("mindmap") or tree),
            "action": "imported_mindmap",
        }
    for i, row in enumerate(reqs):
        if str(row.get("id") or "") == str(req.get("id") or ""):
            reqs[i] = req
            break
    doc["requirements"] = reqs
    doc["updated_at"] = _now()
    return {"qa_process": doc, "requirement_id": req.get("id"), **stats}


def apply_atlas_patch(qa_process: dict, *, patch_id: str, action: str = "accept", after: dict | None = None) -> dict[str, Any]:
    doc = dict(qa_process or {})
    patches = [dict(x) for x in (doc.get("atlas_patches") or []) if isinstance(x, dict)]
    hit = next((p for p in patches if str(p.get("id") or "") == str(patch_id)), None)
    if hit is None:
        raise ValueError("找不到这条图谱变更")
    act = str(action or "accept").strip().lower()
    if act not in ("accept", "reject"):
        raise ValueError("action 必须是 accept 或 reject")
    hit["status"] = "accepted" if act == "accept" else "rejected"
    hit["reviewed_at"] = _now()
    if after:
        hit["after"] = after
    if act == "accept":
        atlas = doc.get("app_atlas") if isinstance(doc.get("app_atlas"), dict) else {"modules": []}
        payload = after or hit.get("payload") or {}
        modules = list(atlas.get("modules") or [])
        incoming = payload.get("modules") if isinstance(payload.get("modules"), list) else []
        if incoming:
            atlas["modules"] = incoming
        else:
            atlas["modules"] = modules
        atlas["updated_at"] = _now()
        doc["app_atlas"] = atlas
    for i, row in enumerate(patches):
        if str(row.get("id") or "") == str(patch_id):
            patches[i] = hit
            break
    doc["atlas_patches"] = patches
    doc["updated_at"] = _now()
    return {"qa_process": doc, "patch": hit}


def publish_mindmap(qa_process: dict, *, requirement_id: str = "") -> dict[str, Any]:
    doc = dict(qa_process or {})
    reqs = [dict(r) for r in (doc.get("requirements") or []) if isinstance(r, dict)]
    rid = str(requirement_id or "").strip()
    req = next((r for r in reqs if str(r.get("id") or "") == rid), None) if rid else (reqs[0] if reqs else None)
    if req is None:
        raise ValueError("请先选一条需求")
    mindmap = req.get("mindmap") if isinstance(req.get("mindmap"), dict) else {}
    if not mindmap:
        raise ValueError("这条需求还没有脑图")
    req["mindmap_published_at"] = _now()
    req["mindmap_hidden"] = False
    for i, row in enumerate(reqs):
        if str(row.get("id") or "") == str(req.get("id") or ""):
            reqs[i] = req
            break
    doc["requirements"] = reqs
    doc["updated_at"] = _now()
    return {"qa_process": doc, "mindmap": mindmap}


def hide_mindmap(qa_process: dict, *, requirement_id: str = "") -> dict[str, Any]:
    doc = dict(qa_process or {})
    reqs = [dict(r) for r in (doc.get("requirements") or []) if isinstance(r, dict)]
    rid = str(requirement_id or "").strip()
    req = next((r for r in reqs if str(r.get("id") or "") == rid), None) if rid else (reqs[0] if reqs else None)
    if req is None:
        raise ValueError("请先选一条需求")
    req["mindmap_hidden"] = True
    for i, row in enumerate(reqs):
        if str(row.get("id") or "") == str(req.get("id") or ""):
            reqs[i] = req
            break
    doc["requirements"] = reqs
    return {"qa_process": doc}

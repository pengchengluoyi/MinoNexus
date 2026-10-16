"""用例导入：preview / commit，冲突由用户决定覆盖或跳过。"""
from __future__ import annotations
from mino_nexus.core.execution_user import user_thread, owned_job, require_user_id


import copy
import os
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextvars import copy_context
from datetime import datetime, timezone
from typing import Any, Literal

from mino_nexus.services import cover_import as ci
from mino_nexus.services import project_store as ps
from mino_nexus.services import case_import_preview_store as preview_store
from mino_nexus.services.case_store import (
    find_similar_case,
    get_case,
    list_cases,
)

ConflictAction = Literal["overwrite", "skip"]

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_project(project_id: str) -> dict[str, Any]:
    row = ps.find_project(str(project_id or "").strip())
    if not row:
        raise ValueError("项目不存在")
    return row


def list_project_requirements(project_id: str) -> list[dict[str, Any]]:
    """聚合项目下各 App 的 qa_process.requirements（按 id 去重）。"""
    from mino_nexus.services import app_automation as aas

    _require_project(project_id)
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for app in ps.list_apps(project_id):
        app_row = ps.find_app(str(app.get("id") or ""))
        if not app_row:
            continue
        qp = aas.get_automation_config(app_row, hydrate_cases=False).get("qa_process") or {}
        for req in qp.get("requirements") or []:
            if not isinstance(req, dict):
                continue
            rid = str(req.get("id") or "").strip()
            if not rid or rid in seen:
                continue
            seen.add(rid)
            out.append({
                "id": rid,
                "title": str(req.get("title") or req.get("external_id") or rid),
                "external_id": str(req.get("external_id") or ""),
            })
    return out


def _requirement_title(project_id: str, requirement_id: str) -> str:
    rid = str(requirement_id or "").strip()
    for req in list_project_requirements(project_id):
        if str(req.get("id") or "") == rid:
            return str(req.get("title") or rid)
    return rid


def _resolve_import_platform(value: str, env_doc: dict | None, *, default: str = "") -> str:
    from mino_nexus.core.platform_contract import TARGET_PLATFORMS
    raw = str(value or default or "").strip()
    if raw in TARGET_PLATFORMS:
        return raw
    if not raw:
        raise ValueError("missing_platform")
    channels = (env_doc or {}).get("channels") or []
    matches = [ch for ch in channels if isinstance(ch, dict) and
               raw in {str(ch.get(k) or "").strip() for k in ("id", "label", "alias")}]
    if len(matches) != 1:
        raise ValueError("ambiguous_platform" if matches else "invalid_platform")
    channel = matches[0]
    platform = channel.get("platform")
    if channel.get("kind") == "server" or platform not in TARGET_PLATFORMS:
        raise ValueError("invalid_platform")
    return platform


def _norm_import_row(
    raw: dict[str, Any],
    index: int,
    default_platform: str = "",
    env_doc: dict | None = None,
) -> dict[str, Any]:
    steps = raw.get("steps")
    expected = raw.get("expected")
    if isinstance(steps, str):
        steps_list = [s.strip() for s in steps.splitlines() if s.strip()]
        steps_raw = steps
    else:
        steps_list = [str(x) for x in (steps or []) if str(x).strip()]
        steps_raw = str(raw.get("steps_raw") or "") or "\n".join(f"{i}. {x}" for i, x in enumerate(steps_list, 1))
    if isinstance(expected, str):
        expected_list = [s.strip() for s in expected.splitlines() if s.strip()]
        expected_raw = expected
    else:
        expected_list = [str(x) for x in (expected or []) if str(x).strip()]
        expected_raw = str(raw.get("expected_raw") or "") or "\n".join(
            f"{i}. {x}" for i, x in enumerate(expected_list, 1)
        )
    cid = str(raw.get("case_id") or "").strip()
    input_platform = str(raw.get("platform") or default_platform or "").strip()
    platform_error = ""
    try:
        platform = _resolve_import_platform(input_platform, env_doc)
    except ValueError as exc:
        platform, platform_error = input_platform, str(exc)
    return {
        **raw,
        "case_id": cid,
        "name": str(raw.get("name") or cid or f"导入用例 {index + 1}"),
        "module": str(raw.get("module") or ""),
        "precondition": str(raw.get("precondition") or ""),
        "steps": steps_list,
        "expected": expected_list,
        "steps_raw": steps_raw,
        "expected_raw": expected_raw,
        "platform": platform,
        "platform_error": platform_error,
        "aspect": str(raw.get("aspect") or "正向"),
        "point_ids": [str(x) for x in (raw.get("point_ids") or []) if str(x).strip()],
        "index": index,
        "source": "import",
    }


def _similar_case_info(
    project_id: str,
    requirement_id: str,
    row: dict[str, Any],
) -> dict[str, Any] | None:
    ex = find_similar_case(project_id, requirement_id, row)
    if not ex:
        return None
    existing_rid = str(ex.get("requirement_id") or "")
    target_rid = str(requirement_id or "")
    return {
        "exists": True,
        "existing_case_id": ex.get("case_id") or "",
        "existing_updated_at": ex.get("updated_at"),
        "existing_name": ex.get("name") or "",
        "existing_requirement_id": existing_rid,
        "existing_requirement_title": _requirement_title(project_id, existing_rid),
        "requirement_changed": bool(existing_rid and target_rid and existing_rid != target_rid),
        "content_match": True,
    }


def _conflict_info(
    project_id: str,
    case_id: str,
    target_requirement_id: str,
) -> dict[str, Any] | None:
    cid = str(case_id or "").strip()
    if not cid:
        return None
    existing = get_case(project_id, cid)
    if not existing:
        return None
    existing_rid = str(existing.get("requirement_id") or "")
    target_rid = str(target_requirement_id or "")
    return {
        "exists": True,
        "existing_case_id": cid,
        "existing_updated_at": existing.get("updated_at"),
        "existing_name": existing.get("name") or "",
        "existing_requirement_id": existing_rid,
        "existing_requirement_title": _requirement_title(project_id, existing_rid),
        "requirement_changed": bool(existing_rid and target_rid and existing_rid != target_rid),
    }


def preview_import(
    *,
    project_id: str,
    requirement_id: str,
    table: list[list[str]] | None = None,
    text: str = "",
    filename: str = "",
    header_row: int = 0,
    skip_rows: list[int] | None = None,
    column_map: dict[str, int] | None = None,
    default_platform: str = "",
) -> dict[str, Any]:
    pid = str(project_id or "").strip()
    rid = str(requirement_id or "").strip()
    if not pid:
        raise ValueError("缺少 project_id")
    if not rid:
        raise ValueError("请选择需求")
    _require_project(pid)
    reqs = list_project_requirements(pid)
    if not any(str(r.get("id") or "") == rid for r in reqs):
        raise ValueError(f"需求 {rid} 不属于该项目")
    grid = normalize_table(table) if table else ci.load_table_from_text(text, filename)
    labels = ci.header_labels(grid, header_row)
    cmap = dict(column_map or ci.suggest_column_map(labels))
    parsed, meta = ci.parse_table_rows(
        grid,
        header_row=header_row,
        skip_rows=skip_rows,
        column_map=cmap,
    )
    preview_rows: list[dict[str, Any]] = []
    conflicts: list[str] = []
    default_plat = str(default_platform or "").strip()
    try:
        env_doc = ps.project_env(pid)
    except KeyError:
        env_doc = {}
    for i, raw in enumerate(parsed):
        row = _norm_import_row(raw, i, default_plat, env_doc)
        name = str(row.get("name") or "").strip()
        cid = str(row.get("case_id") or "").strip()
        conflict = _conflict_info(pid, cid, rid) if cid else None
        if not conflict:
            conflict = _similar_case_info(pid, rid, row)
        if conflict:
            conflicts.append(str(conflict.get("existing_case_id") or cid or name))
        pre = row.get("precondition") or ""
        claim_summary = ""
        try:
            from mino_nexus.services.case_resource_claim import compile_resource_key_from_precondition
            from mino_nexus.services.resource_claim_summary import resource_claim_summary

            if row.get("platform_error"):
                raise ValueError(row["platform_error"])
            preview_plat = str(row.get("platform") or default_plat or "").strip()
            claim = compile_resource_key_from_precondition(
                str(pre),
                env_doc=env_doc,
                platform=preview_plat,
            )
            claim_summary = resource_claim_summary(
                claim,
                scene=claim.get("case_scene") if isinstance(claim.get("case_scene"), dict) else None,
                precondition=str(pre),
            )
            row["resource_key"] = claim
        except Exception:
            claim_summary = ""
        steps = row.get("steps") or []
        expected = row.get("expected") or []
        flags: list[str] = [row["platform_error"]] if row.get("platform_error") else []
        header_names = {
            "用例名称", "名称", "用例编号", "编号", "模块", "端", "前置条件",
            "步骤", "测试步骤", "预期", "预期效果", "预期结果",
        }
        if name in header_names:
            flags.append("likely_header")
        if name and not steps and not cid:
            flags.append("empty_steps")
        if name and not expected and not cid:
            flags.append("empty_expected")
        if conflict and not cid:
            flags.append("likely_duplicate")
        steps_text = "\n".join(str(x) for x in steps if str(x).strip()) or str(row.get("steps_raw") or "")
        expected_text = "\n".join(str(x) for x in expected if str(x).strip()) or str(row.get("expected_raw") or "")
        incomplete = not name or not steps_text.strip() or not expected_text.strip()
        if incomplete:
            flags.append("incomplete")
        preview_rows.append({
            "row_index": raw.get("row_index", i),
            "case_id": cid,
            "generated_id": not bool(cid),
            "name": name,
            "module": row.get("module") or "",
            "platform": row.get("platform") or "",
            "precondition_preview": str(pre),
            "resource_claim_summary": claim_summary,
            "steps_preview": "\n".join(steps),
            "expected_preview": "\n".join(expected),
            "steps_parsed": [],
            "expected_parsed": [],
            "remark": ("平台需明确为 mac / win / android / web：" + row["platform_error"]) if row.get("platform_error") else ("标题、操作、预期缺一列，不解析" if incomplete else ""),
            "step_key_validation": {},
            "ui_automation": {"coverable": False, "blocked_by": []} if incomplete else {"coverable": True, "blocked_by": []},
            "parse_status": "invalid_platform" if row.get("platform_error") else ("incomplete" if incomplete else "pending"),
            "flags": flags,
            "selected_by_default": (not incomplete) and not row.get("platform_error") and "likely_header" not in flags,
            "platform_error": row.get("platform_error", ""),
            "conflict": conflict,
            "payload": row,
        })
    from mino_nexus.services.settings_store import get_case_import_settings

    token = uuid.uuid4().hex
    app_id = next((str(app.get("id") or "") for app in ps.list_apps(pid) if app.get("id")), "")
    preview_store.create(token, {
        "owner_user_id": require_user_id(),
        "created_at": _now(),
        "project_id": pid,
        "app_id": app_id,
        "env_doc": env_doc,
        "requirement_id": rid,
        "default_platform": default_plat,
        "rows": copy.deepcopy(preview_rows),
        "meta": meta,
        "cancel": False,
        "batch_size": int(get_case_import_settings().get("preview_batch_size") or 5),
    })
    _start_preview_worker(token)
    return {
        "preview_token": token,
        "project_id": pid,
        "requirement_id": rid,
        "requirement_title": _requirement_title(pid, rid),
        "parsed": sum(1 for row in preview_rows if row.get("parse_status") != "pending"),
        "total": len(preview_rows),
        "done": all(row.get("parse_status") != "pending" for row in preview_rows),
        "conflicts": conflicts,
        "rows": preview_rows,
        "import_defaults": get_case_import_settings(),
        **meta,
    }


def preview_status(preview_token: str, project_id: str = "") -> dict[str, Any] | None:
    doc = preview_store.get(str(preview_token or ""), project_id)
    if not doc:
        return None
    if not doc.get("cancel") and any(row.get("parse_status") == "pending" for row in doc.get("rows") or []):
        _start_preview_worker(str(preview_token))
    rows = list(doc.get("rows") or [])
    parsed = sum(1 for row in rows if row.get("parse_status") != "pending")
    return {
        "preview_token": str(preview_token),
        "parsed": parsed,
        "total": len(rows),
        "done": parsed >= len(rows) and not any(row.get("parse_status") == "pending" for row in rows),
        "rows": rows,
        "cancelled": bool(doc.get("cancel")),
    }


def cancel_preview(preview_token: str, project_id: str = "") -> bool:
    doc = preview_store.get(str(preview_token or ""), project_id)
    if not doc:
        return False
    doc["cancel"] = True
    for row in doc.get("rows") or []:
        if row.get("parse_status") == "pending":
            row["parse_status"] = "cancelled"
    return preview_store.update(str(preview_token), doc)


def retry_preview_row(preview_token: str, row_index: int, project_id: str = "") -> bool:
    doc = preview_store.get(str(preview_token or ""), project_id)
    if not doc:
        return False
    found = False
    for row in doc.get("rows") or []:
        if int(row.get("row_index", -1)) != int(row_index):
            continue
        if row.get("parse_status") not in ("error", "cancelled"):
            return False
        row["parse_status"] = "pending"
        row["steps_parsed"] = []
        row["expected_parsed"] = []
        found = True
        break
    if not found:
        return False
    preview_store.update_row(str(preview_token), row, restart=True)
    _start_preview_worker(str(preview_token))
    return True


def _start_preview_worker(token: str) -> None:
    worker_id = f"{os.getpid()}:{uuid.uuid4().hex}"
    if preview_store.claim(token, worker_id):
        try:
            user_thread(target=_compile_preview_batches, args=(token, worker_id), daemon=True).start()
        except Exception:
            preview_store.release(token, worker_id)
            raise


def _compile_preview_batches(token: str, worker_id: str) -> None:
    from mino_nexus.services.case_key_registry import import_row_key_summary

    try:
        while True:
            doc = preview_store.get(token)
            if not doc or doc.get("cancel"):
                return
            pending = [row for row in doc.get("rows") or [] if row.get("parse_status") == "pending"]
            if not pending:
                return
            batch = pending[: max(1, min(4, int(doc.get("batch_size") or 5)))]
            with ThreadPoolExecutor(max_workers=len(batch)) as executor:
                futures = {
                    executor.submit(copy_context().run, import_row_key_summary, row.get("payload") or {}, project_id=str(doc.get("project_id") or ""), app_id=str(doc.get("app_id") or ""), env_doc=doc.get("env_doc")): row["row_index"]
                    for row in batch
                }
                for future in as_completed(futures):
                    doc = preview_store.get(token)
                    if not doc or doc.get("cancel"):
                        return
                    row = next(item for item in doc["rows"] if item["row_index"] == futures[future])
                    try:
                        summary = future.result()
                    except Exception as exc:
                        row["parse_status"] = "error"
                        row["remark"] = str(exc)[:200]
                        row["selected_by_default"] = False
                    else:
                        row["steps_parsed"] = list(summary.get("steps_parsed") or [])
                        row["expected_parsed"] = list(summary.get("expected_parsed") or [])
                        row["remark"] = str(summary.get("remark") or "")
                        row["payload"]["meta"] = summary.pop("compiled_meta", {})
                        row["step_key_validation"] = summary
                        row["automation_details"] = summary.get("automation_details") or {}
                        row["ui_automation"] = summary.get("ui_automation") or {"coverable": True, "blocked_by": []}
                        flags = [flag for flag in row.get("flags") or [] if flag not in ("step_key_issues", "ui_not_coverable")]
                        if summary.get("issues"):
                            flags.append("step_key_issues")
                        if row["ui_automation"].get("coverable") is False:
                            flags.append("ui_not_coverable")
                        row["flags"] = flags
                        row["parse_status"] = "done"
                    if not preview_store.update_row(token, row, worker_id=worker_id):
                        return
    finally:
        preview_store.release(token, worker_id)


def normalize_table(table: Any) -> list[list[str]]:
    return ci.normalize_table(table)


def commit_import(
    *,
    project_id: str,
    requirement_id: str,
    preview_token: str = "",
    rows: list[dict[str, Any]] | None = None,
    default_on_conflict: ConflictAction = "skip",
    default_platform: str = "",
    block_on_step_key_issues: bool = False,
    client_request_id: str = "",
) -> dict[str, Any]:
    pid = str(project_id or "").strip()
    rid = str(requirement_id or "").strip()
    if not pid or not rid:
        raise ValueError("缺少 project_id 或 requirement_id")
    _require_project(pid)
    from mino_nexus.services import case_import_receipts as receipts
    request_id, digest = receipts.request_identity({
        "requirement_id": rid, "preview_token": preview_token, "rows": rows,
        "default_on_conflict": default_on_conflict, "default_platform": default_platform,
        "block_on_step_key_issues": block_on_step_key_issues,
    }, client_request_id, preview_token)
    receipt_id = receipts.find(pid, request_id, digest)
    if receipt_id:
        return receipts.resume(pid, receipt_id)
    work_rows: list[dict[str, Any]] = []
    fill_plat = str(default_platform or "").strip()
    if preview_token:
        doc = preview_store.get(str(preview_token), pid)
        if not doc:
            raise ValueError("预览已过期，请重新解析")
        if str(doc.get("project_id") or "") != pid:
            raise ValueError("预览与项目不匹配")
        if str(doc.get("requirement_id") or "") != rid:
            raise ValueError("预览与需求不匹配")
        if any(row.get("parse_status") == "pending" for row in (doc.get("rows") or [])):
            raise ValueError("解析还没完成")
        if doc.get("cancel"):
            raise ValueError("预览已取消，请重新解析")
        if not fill_plat:
            fill_plat = str(doc.get("default_platform") or "").strip()
        work_rows = copy.deepcopy(list(doc.get("rows") or []))
        overrides = {
            item.get("row_index"): item
            for item in (rows or [])
            if isinstance(item, dict) and item.get("row_index") is not None
        }
        for item in work_rows:
            ov = overrides.get(item.get("row_index"))
            if not ov:
                continue
            if "on_conflict" in ov:
                item["on_conflict"] = ov["on_conflict"]
            if "selected" in ov:
                item["selected"] = ov["selected"]
        if any(item.get("selected") is not False and item.get("parse_status") != "done" for item in work_rows):
            raise ValueError("所选用例尚未成功解析，请重试或取消勾选")
    elif rows:
        work_rows = rows
    else:
        raise ValueError("请提供 preview_token 或 rows")
    if not work_rows:
        raise ValueError("没有可导入的行")
    try:
        env_doc = ps.project_env(pid)
    except KeyError:
        env_doc = {}
    app_id = ""
    for app in ps.list_apps(pid):
        if str(app.get("id") or ""):
            app_id = str(app.get("id") or "")
            break
    if any(not isinstance(item, dict) for item in work_rows):
        raise ValueError("导入来源行重复或格式错误")
    work_rows = copy.deepcopy(work_rows)
    for ordinal, item in enumerate(work_rows):
        if not preview_token and item.get("row_index") is None:
            item["row_index"] = ordinal
    indexes = [item.get("row_index") for item in work_rows]
    if any(type(index) is not int or index < 0 for index in indexes) or len(indexes) != len(set(indexes)):
        raise ValueError("导入来源行重复或格式错误")
    receipt_id = receipts.create(pid, request_id, digest, {
        "project_id": pid, "requirement_id": rid, "rows": work_rows, "env_doc": env_doc,
        "app_id": app_id, "compiled": bool(preview_token), "default_platform": fill_plat,
        "default_on_conflict": default_on_conflict, "block_on_step_key_issues": block_on_step_key_issues,
    })
    return receipts.resume(pid, receipt_id)


def prepare_receipt_line(snapshot, item):
    from mino_nexus.services.case_store import prepare_import_row

    if item.get("selected") is False:
        return {"status": "excluded"}
    payload = dict(item.get("payload") if isinstance(item.get("payload"), dict) else item)
    if snapshot["default_platform"] and not str(payload.get("platform") or "").strip():
        payload["platform"] = snapshot["default_platform"]
    index = int(payload.get("index") or item.get("row_index") or 0)
    row = _norm_import_row(payload, index, "", snapshot["env_doc"])
    if row.get("platform_error"):
        return {"status": "blocked", "reason": row["platform_error"], "case_id": str(row.get("case_id") or "")}
    # Confirmation imports the original source, even when preview compilation
    # has gaps. The durable job reports those gaps after the atomic source write.
    if not str(row.get("platform") or "").strip():
        return {"status": "skipped", "reason": "missing_platform", "case_id": str(row.get("case_id") or "")}
    conflict = item.get("conflict") or {}
    on_conflict = "overwrite" if item.get("on_conflict", snapshot["default_on_conflict"]) == "overwrite" else "skip"
    return prepare_import_row(snapshot["project_id"], snapshot["requirement_id"], row,
        on_conflict=on_conflict, compiled=snapshot["compiled"], expected_updated_at=conflict.get("existing_updated_at"))


def cases_payload(project_id: str) -> dict[str, Any]:
    pid = str(project_id or "").strip()
    _require_project(pid)
    cases = list_cases(pid)
    req_titles = {str(r.get("id") or ""): str(r.get("title") or "") for r in list_project_requirements(pid)}
    rows: list[dict[str, Any]] = []
    from mino_nexus.services.case_key_registry import step_keys_summary_for_case

    for raw in cases:
        title = req_titles.get(str(raw.get("requirement_id") or ""), "需求")
        rows.append(
            {
                **raw,
                "requirement_title": title,
                "step_keys_summary": step_keys_summary_for_case(raw),
            }
        )
    return {
        "cases": rows,
        "total": len(rows),
        "source": "project_cases",
        "project_id": pid,
    }

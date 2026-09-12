"""NavFSM 校准证据：hierarchy 片段 + 有序 walkthrough 记录。

设计稿 docs/NAVIGATION_ATLAS.md §0.2.2、§8.4。

**证据不进库、不进 git**，只落 `data_dir()`：

    {data_dir}/nav/calibration/{app_id}/{calibration_id}/
      manifest.json      project_id / app_id / account_id / hierarchy_format / 各 detect 结论
      walkthrough.json   有序步骤：step_key / run_id / case_id / turn_id
      hierarchy/step_001.txt

结论（`detect.strength`、`resource_id_regex` 等）由人比对证据后录进 `nav_fsm*` 表，
不由本模块自动写库 —— 设计稿 §7「图谱不自动膨胀」。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from mino_nexus.core.paths import nav_calibration_dir
from mino_nexus.loop.hierarchy_slots import HIERARCHY_FORMAT

MANIFEST_NAME = "manifest.json"
DRAFT_NAME = "nav_fsm_draft.json"
WALKTHROUGH_NAME = "walkthrough.json"
HIERARCHY_DIR = "hierarchy"


def new_calibration_id() -> str:
    """UTC 紧凑时间戳，与设计稿示例 `20260910T120000Z` 同形。"""
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def start(
    app_id: str,
    *,
    project_id: str = "",
    account_id: str = "",
    calibration_id: str = "",
    hierarchy_result_key: str = "nodes",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """开一次 walkthrough，写初始 manifest。返回 manifest 内容。

    `account_id` 必须是跑批将用的租号（§11.4）—— 开发机个人账号采的证据不得录入配置。
    """
    cid = str(calibration_id or "").strip() or new_calibration_id()
    manifest = {
        "calibration_id": cid,
        "app_id": str(app_id or ""),
        "project_id": str(project_id or ""),
        "account_id": str(account_id or ""),
        "hierarchy_format": HIERARCHY_FORMAT,
        "hierarchy_result_key": str(hierarchy_result_key or "nodes"),
        "started_at": int(time.time()),
        "samples": [],
        **dict(extra or {}),
    }
    root = nav_calibration_dir(app_id, cid)
    _write_json(root / MANIFEST_NAME, manifest)
    _write_json(root / WALKTHROUGH_NAME, {"calibration_id": cid, "steps": []})
    return manifest


def append_step(
    app_id: str,
    calibration_id: str,
    *,
    step_key: str,
    hierarchy_text: str = "",
    nodes: list[dict[str, Any]] | None = None,
    run_id: str = "",
    case_id: str = "",
    turn_id: int = 0,
    note: str = "",
) -> dict[str, Any]:
    """记一步。**顺序即语义** —— walkthrough 中途会改账号状态，跳步补采无效（§8.4.0）。"""
    root = nav_calibration_dir(app_id, calibration_id)
    doc = _read_json(root / WALKTHROUGH_NAME, {"calibration_id": calibration_id, "steps": []})
    steps = list(doc.get("steps") or [])
    idx = len(steps) + 1
    rel = f"{HIERARCHY_DIR}/step_{idx:03d}.txt"

    body = hierarchy_text
    if not body and nodes is not None:
        from mino_nexus.loop.hierarchy_slots import flatten

        body = flatten(nodes)
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body or "", encoding="utf-8")
    if nodes is not None:
        _write_json(root / HIERARCHY_DIR / f"step_{idx:03d}.json", nodes)

    step = {
        "step_key": str(step_key or f"W{idx}"),
        "index": idx,
        "run_id": str(run_id or ""),
        "case_id": str(case_id or ""),
        "turn_id": int(turn_id or 0),
        "hierarchy_rel_path": rel,
        "node_count": len(nodes or []),
        "note": str(note or ""),
        "at": int(time.time()),
    }
    steps.append(step)
    doc["steps"] = steps
    _write_json(root / WALKTHROUGH_NAME, doc)
    return step


def finish(app_id: str, calibration_id: str, *, conclusions: dict[str, Any] | None = None) -> dict[str, Any]:
    """收工：把比对结论并进 manifest（`follow_filled_detectable`、`dialog_layer` 等）。"""
    root = nav_calibration_dir(app_id, calibration_id)
    manifest = _read_json(root / MANIFEST_NAME, {})
    manifest.update(dict(conclusions or {}))
    manifest["finished_at"] = int(time.time())
    walkthrough = _read_json(root / WALKTHROUGH_NAME, {"steps": []})
    manifest["samples"] = [
        {k: s.get(k) for k in ("step_key", "index", "turn_id", "node_count", "hierarchy_rel_path")}
        for s in (walkthrough.get("steps") or [])
    ]
    _write_json(root / MANIFEST_NAME, manifest)
    return manifest


def read(app_id: str, calibration_id: str) -> dict[str, Any] | None:
    """读一份证据摘要。正文片段按需另取（`read_step`）。"""
    root = nav_calibration_dir(app_id, calibration_id)
    if not root.exists():
        return None
    return {
        "app_id": str(app_id or ""),
        "calibration_id": str(calibration_id or ""),
        "evidence_rel_path": f"nav/calibration/{app_id}/{calibration_id}",
        "manifest": _read_json(root / MANIFEST_NAME, {}),
        "walkthrough": _read_json(root / WALKTHROUGH_NAME, {"steps": []}),
    }


def read_step(app_id: str, calibration_id: str, index: int) -> str:
    path = nav_calibration_dir(app_id, calibration_id) / HIERARCHY_DIR / f"step_{int(index):03d}.txt"
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def list_calibrations(app_id: str) -> list[dict[str, Any]]:
    root = nav_calibration_dir(app_id)
    if not root.exists():
        return []
    rows: list[dict[str, Any]] = []
    for child in sorted(root.iterdir(), reverse=True):
        if not child.is_dir():
            continue
        manifest = _read_json(child / MANIFEST_NAME, {})
        walkthrough = _read_json(child / WALKTHROUGH_NAME, {"steps": []})
        rows.append(
            {
                "calibration_id": child.name,
                "account_id": str(manifest.get("account_id") or ""),
                "hierarchy_format": str(manifest.get("hierarchy_format") or ""),
                "started_at": int(manifest.get("started_at") or 0),
                "finished_at": int(manifest.get("finished_at") or 0),
                "step_count": len(walkthrough.get("steps") or []),
            }
        )
    return rows


# ---------------- 待校准草稿 ----------------
#
# 草稿**故意不进 `nav_fsm*` 表**：设计稿 §10.5 的门禁是「任意字段含 `__CALIBRATE__` →
# load / PUT 一律拒绝」，也就是**库里永远不该有占位符**。但人填表要跨会话保存进度，
# 于是把半成品放在证据目录旁边 —— 它本来就是照着同一批 walkthrough 证据填的。
# 填完走 `promote()` 校验后才进库。


def _legacy_draft_path(app_id: str) -> Path:
    return nav_calibration_dir(app_id) / DRAFT_NAME


def _read_legacy_draft_file(app_id: str) -> dict[str, Any] | None:
    path = _legacy_draft_path(app_id)
    if not path.exists():
        return None
    legacy = _read_json(path, None)
    return legacy if isinstance(legacy, dict) else None


def _migrate_legacy_draft(app_id: str) -> dict[str, Any] | None:
    """一次性：旧版 `nav_fsm_draft.json` → `nav_fsm` 表 version=draft。"""
    legacy = _read_legacy_draft_file(app_id)
    if not legacy:
        return None
    from mino_nexus.services import nav_fsm_store as store

    try:
        saved = store.save_draft(app_id, legacy)
        try:
            _legacy_draft_path(app_id).unlink()
        except OSError:
            pass
        return saved
    except Exception:
        return legacy


def save_draft(app_id: str, doc: dict[str, Any], *, updated_by: str = "") -> dict[str, Any]:
    """草稿写入 `nav_fsm` 表（version=draft）。DB 不可用时回退遗留 JSON（仅测试/离线）。"""
    from mino_nexus.services import nav_fsm_store as store

    try:
        return store.save_draft(app_id, doc, updated_by=updated_by)
    except Exception:
        payload = dict(doc or {})
        payload["_draft_saved_at"] = int(time.time())
        _write_json(_legacy_draft_path(app_id), payload)
        return payload


def read_draft(app_id: str) -> dict[str, Any] | None:
    from mino_nexus.services import nav_fsm_store as store

    try:
        doc = store.read_draft(app_id)
        if doc:
            return doc
    except Exception:
        pass
    legacy = _read_legacy_draft_file(app_id)
    if legacy:
        return legacy
    try:
        return _migrate_legacy_draft(app_id)
    except Exception:
        return None


def delete_draft(app_id: str) -> bool:
    from mino_nexus.services import nav_fsm_store as store

    deleted = store.delete(app_id, version=store.DRAFT_VERSION)
    path = _legacy_draft_path(app_id)
    if path.exists():
        try:
            path.unlink()
            return True
        except OSError:
            pass
    return deleted


def promote_draft(app_id: str, *, updated_by: str = "") -> dict[str, Any]:
    """draft 表行 → v1 正式发布。"""
    read_draft(app_id)  # 触发遗留 JSON 迁移
    from mino_nexus.services import nav_fsm_store as store

    return store.promote_draft(app_id, updated_by=updated_by)

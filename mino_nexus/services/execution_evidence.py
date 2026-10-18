"""Project execution evidence from historical session events, never from live nodes."""
from __future__ import annotations

import re


def object_value(value):
    return value if isinstance(value, dict) else {}


def public_identity(value):
    value = object_value(value)
    safe = {key: value[key][:256] for key in ("instance_id", "build_id", "source_commit", "source_digest", "identity_kind")
            if isinstance(value.get(key), str)}
    safe.update({key: value[key] for key in ("dirty", "source_unchanged") if type(value.get(key)) is bool})
    return safe


def credible_build(value):
    value = public_identity(value)
    if value.get("source_unchanged") is not True or not re.fullmatch(r"[a-f0-9]{40,64}", value.get("source_commit", "")):
        return False
    if value.get("identity_kind") == "source":
        digest = value.get("source_digest", "")
        return bool(re.fullmatch(r"[a-f0-9]{64}", digest)) and value.get("build_id") == "dev-" + digest[:24]
    return value.get("identity_kind") == "release" and bool(value.get("build_id")) and value.get("build_id") != "source-unpackaged"


def summarize(meta, events, *, instance_id, expected_node_id="", expected_build_id="",
              expected_nexus_build_id="", truncated=False):
    issues = []

    def issue(code, message, *, failed=False):
        if not any(row["code"] == code for row in issues):
            issues.append({"code": code, "message": message, "failed": failed})

    provenance = next((object_value(event["payload"]).get("nexus") for event in events
                       if event["type"] == "session/provenance"), None)
    nexus = public_identity(provenance)
    if nexus.get("instance_id") != instance_id or not credible_build(nexus):
        issue("nexus_identity_missing", "会话未记录可核对的 Nexus 实例与进程 build")
    if not expected_nexus_build_id:
        issue("expected_nexus_build_missing", "尚未指定预期 Nexus build")
    elif nexus.get("build_id") != expected_nexus_build_id:
        issue("nexus_build_mismatch", "会话实际 Nexus build 与预期不符", failed=True)
    if not expected_node_id or not expected_build_id:
        issue("expected_scout_missing", "尚未指定预期测试节点与 Scout build")
    nodes = {}
    result_count = 0
    success_count = 0
    for event in events:
        payload = object_value(event["payload"])
        raw = object_value(payload.get("raw_response"))
        scout = object_value(payload.get("scout") or raw.get("scout"))
        if not scout:
            if event["type"] in {"device/action", "device/observation"} or (
                event["type"] == "tool/result" and payload.get("executor_used") not in
                {None, "", "internal", "internal_executor", "vlm", "vlm_executor", "hitl", "hitl_executor", "ai_persona_executor"}
            ):
                issue("scout_receipt_missing", "部分执行结果没有 Scout 进程回执，不能证明执行版本")
            continue
        result_count += 1
        if payload.get("status") in {"pass", "success", "passed"}:
            success_count += 1
        receipt = object_value(scout.get("execution_identity"))
        node_id = str(scout.get("node_id") or "")
        build = public_identity(receipt.get("build"))
        generation = str(receipt.get("scout_process_generation") or "")
        connection = str(receipt.get("node_connection_generation") or "")
        if (receipt.get("schema_version") != 1 or not node_id or receipt.get("node_id") != node_id
                or not generation or generation != scout.get("scout_process_generation")
                or not connection or connection != scout.get("node_connection_generation")):
            issue("scout_identity_unconfirmed", "Scout 回执与派发节点或进程、连接身份不一致", failed=bool(receipt))
        if not credible_build(build):
            issue("scout_build_unconfirmed", "Scout build 缺失或启动后源码已变化，需重启后重新执行")
        dispatch_nexus = public_identity(scout.get("nexus_identity"))
        if not credible_build(dispatch_nexus) or any(dispatch_nexus.get(key) != nexus.get(key) for key in ("instance_id", "build_id")):
            issue("nexus_execution_unconfirmed", "派发时 Nexus 身份不完整或源码已变化，需重新执行")
        if expected_node_id and node_id != expected_node_id:
            issue("scout_node_mismatch", "实际执行节点与预期测试节点不符", failed=True)
        if expected_build_id and build.get("build_id") != expected_build_id:
            issue("scout_build_mismatch", "实际执行 Scout build 与预期不符", failed=True)
        key = (node_id, generation, build.get("build_id", ""))
        node = nodes.setdefault(key, {"node_id": node_id, "scout_process_generation": generation,
                                     "build": build, "result_count": 0, "event_seqs": [], "devices": []})
        node["result_count"] += 1
        if len(node["event_seqs"]) < 20:
            node["event_seqs"].append(event["seq"])
        device = str(scout.get("sn") or "")
        if device and device not in node["devices"]:
            node["devices"].append(device)
    if len({key[1] for key in nodes}) > 1:
        issue("scout_process_changed", "会话跨越多个 Scout 进程，不能作为单一 build 的执行证明", failed=True)
    if not result_count or not success_count:
        issue("execution_missing", "尚无测试 Scout 成功执行的会话证据")
    if not meta.get("finished_at"):
        issue("session_unfinished", "会话尚未结束")
    elif meta.get("status") not in {"pass", "passed", "success", "done"}:
        issue("session_failed", "用例未通过", failed=True)
    if object_value(meta.get("verdict")).get("verification_status") in {"unverified", "verified_fail"}:
        issue("verdict_unverified", "会话终态尚未核验通过")
    if truncated:
        issue("evidence_truncated", "会话执行记录超出读取上限，证据不完整")
    state = "failed" if any(row["failed"] for row in issues) else "incomplete" if issues else "verified"
    return {"schema_version": 1, "scope": "session_execution", "state": state, "issues": issues,
            "instance_id": instance_id, "session_id": meta["session_id"], "run_id": meta["run_id"],
            "case_id": meta.get("case_id", ""), "status": meta.get("status", ""),
            "nexus": nexus, "nodes": list(nodes.values()), "result_count": result_count,
            "truncated": truncated, "web_chain_verified": False, "mcp_regression_verified": False,
            "reference": {"instance_id": instance_id, "session_id": meta["session_id"],
                          "run_id": meta["run_id"], "nexus": nexus,
                          "scouts": [{"node_id": row["node_id"], "build": row["build"],
                                      "scout_process_generation": row["scout_process_generation"]} for row in nodes.values()]}}

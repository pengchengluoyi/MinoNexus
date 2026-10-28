"""按用例脚本执行一条用例。前置、正文、后置由运行时调用，后置在 finally 里。"""
from __future__ import annotations

import builtins
import sys
import time
import traceback
from typing import Any, Callable

from mino_nexus.casework.script_check import ScriptRejected, check_script
from mino_nexus.contract.step_pointer import build_seq_nodes, compute_case_wall_budget_sec
from mino_nexus.core.log import SLog
from mino_nexus.loop.observe.agent_stream import emit_agent_event
from mino_nexus.loop.router_proxy import RouterProxy
from mino_nexus.loop.script_runtime import CaseRuntime, ScriptAbort, ScriptHalt
from mino_nexus.loop.web.web_env import cleanup_after_case, reset_before_case
from mino_nexus.records.eventlog.writer import SessionPersistenceError, bind_writer, open_session
from mino_nexus.runtime.run_context import bind_case_project, build_run_context

TAG = "ScriptCase"
_FILENAMES = (
    "project_pretest.py",
    "case_pretest.py",
    "case_run.py",
    "case_posttest.py",
    "project_posttest.py",
)


def run_script_case(
    *,
    run_id: str,
    case: dict[str, Any],
    sn: str,
    app_id: str,
    package: str = "",
    provider_id: str = "",
    playbook: dict | None = None,
    app_name: str = "",
    cancel_check=None,
    case_seq: int = 0,
    playwright_headless: bool = True,
    parent_session_id: str = "",
    fork_state: dict[str, Any] | None = None,
    run_env_brief: str = "",
    next_case: dict[str, Any] | None = None,
    resource_attempt: dict[str, Any] | None = None,
    scripts: dict[str, str] | None = None,
    repo: dict[str, str] | None = None,
    platform: str = "",
) -> dict[str, Any]:
    del app_name, parent_session_id, fork_state, resource_attempt
    bundle = dict(scripts or {})
    cid = str(case.get("case_id") or "")
    stream_id = str(case.get("report_run_id") or "").strip() or run_id
    t0 = time.time()
    ctx = build_run_context(sn, run_id=run_id, app_id=app_id, provider_id=provider_id, target_package=package)
    ctx.session_id = stream_id
    ctx.case_id = cid
    ctx.case_seq = case_seq
    ctx.scout_run_id = stream_id
    if playbook:
        ctx.playbook = playbook
    if str(run_env_brief or "").strip():
        ctx.env_label = str(run_env_brief).strip()
    project_error = ""
    try:
        bind_case_project(ctx, case)
    except ValueError as exc:
        project_error = str(exc)
    try:
        from mino_nexus.resources.accounts.lease import restore_lease_for_run

        if not project_error:
            restore_lease_for_run(ctx, run_id)
    except Exception as exc:
        SLog.w(TAG, f"restore lease failed case={cid}: {exc!r}")
    try:
        from mino_nexus.casework import run_store as _rs

        doc = _rs.get(run_id) or {}
        ctx.env_profile = str(doc.get("env_profile") or getattr(ctx, "env_profile", "") or "test")
    except Exception:
        doc = {}
    proxy = RouterProxy(
        sn,
        run_id=stream_id,
        task_id=run_id,
        target_package=package,
        playwright_headless=playwright_headless,
        run_context=ctx,
    )
    deadline = t0 + compute_case_wall_budget_sec(case)
    try:
        writer = open_session(
            session_id=stream_id,
            run_id=run_id,
            case_id=cid,
            app_id=app_id,
            provider_id=provider_id,
            sn=sn,
            sop_id="run-case-script",
            extra={
                "goal": str(case.get("name") or cid),
                "execution_kind": "case_script",
                "script_digest": str(bundle.get("digest") or ""),
                "project_script_digest": str(bundle.get("project_digest") or ""),
                "seq_node_count": len(build_seq_nodes(case)),
                "project_id": str(getattr(ctx, "project_id", "") or ""),
            },
        )
    except SessionPersistenceError as exc:
        return {
            "status": "blocked",
            "summary": str(exc),
            "steps": [],
            "reason_code": exc.reason_code,
            "failure_stage": exc.failure_stage,
            "execution_started": "no",
            "elapsed_ms": int((time.time() - t0) * 1000),
        }
    runtime = CaseRuntime(
        case=case,
        ctx=ctx,
        proxy=proxy,
        writer=writer,
        run_id=run_id,
        stream_id=stream_id,
        app_id=app_id,
        provider_id=provider_id,
        deadline=deadline,
        cancel_check=cancel_check,
    )
    status = "pass"
    summary = "脚本执行完成"
    reason_code = ""
    failure_stage = ""
    loaded: dict[str, Callable | None] = {}
    files: dict[str, str] = {}
    case_step = 0
    use_repo = bool(repo and str(repo.get("url") or "").strip())
    try:
        with bind_writer(writer):
            if project_error:
                raise ScriptAbort(project_error, reason_code="project_scope_invalid")
            if not ctx.has_control_channel:
                raise ScriptAbort("没有可用设备通道。请确认 Scout 在线并已上报这台设备。", reason_code="device_channel_unavailable")
            if use_repo:
                reset_before_case(proxy, ctx, run_id=stream_id, case_seq=case_seq, case=case)
                _run_repo(runtime, project_id=str(getattr(ctx, "project_id", "") or case.get("project_id") or ""),
                          case_id=cid, platform=platform, repo=repo or {}, case=case)
            else:
                loaded = _load_all(bundle, files)
                reset_before_case(proxy, ctx, run_id=stream_id, case_seq=case_seq, case=case)
                _call(runtime, "pretest", loaded["project_pretest.py"])
                _call(runtime, "pretest", loaded["case_pretest.py"])
                _call(runtime, "run", loaded["case_run.py"])
    except ScriptHalt as exc:
        case_step = int(runtime.current_step.n if runtime.current_step else 0)
        status, summary, reason_code, failure_stage = _failure(runtime, exc, files)
        _write_error(runtime, exc, phase=failure_stage, files=files)
    except ScriptAbort as exc:
        case_step = int(runtime.current_step.n if runtime.current_step else 0)
        status, summary, reason_code, failure_stage = _failure(runtime, exc, files)
        _write_error(runtime, exc, phase=failure_stage, files=files)
    except Exception as exc:
        case_step = int(runtime.current_step.n if runtime.current_step else 0)
        status, summary, reason_code, failure_stage = _failure(runtime, exc, files)
        _write_error(runtime, exc, phase=failure_stage, files=files)
    finally:
        post_error: BaseException | None = None
        try:
            with bind_writer(writer):
                if not use_repo:
                    _call(runtime, "posttest", loaded.get("case_posttest.py"))
                    _call(runtime, "posttest", loaded.get("project_posttest.py"))
        except ScriptHalt as exc:
            post_error = exc
        except Exception as exc:
            post_error = exc
        try:
            cleanup_after_case(proxy, ctx, run_id=stream_id, case_seq=case_seq, case=case, next_case=next_case)
        except Exception as exc:
            SLog.w(TAG, f"cleanup failed case={cid}: {exc!r}")
        if post_error is not None:
            post_status, post_summary, post_code, post_stage = _failure(runtime, post_error, files, phase="posttest")
            if status == "pass":
                status, summary, reason_code, failure_stage = post_status, post_summary, post_code, post_stage
                case_step = int(runtime.current_step.n if runtime.current_step else 0)
            else:
                summary = f"{summary}；后置清理失败：{post_summary}"[:300]
            _write_error(runtime, post_error, phase="posttest", files=files)
        try:
            writer.close(status=status, summary=summary[:240], step_count=len(runtime.steps), reason_code=reason_code)
        except Exception as exc:
            SLog.w(TAG, f"close session failed case={cid}: {exc!r}")
    return {
        "status": status,
        "summary": summary[:240],
        "steps": runtime.steps,
        "elapsed_ms": int((time.time() - t0) * 1000),
        "reason_code": reason_code,
        "failure_stage": failure_stage,
        "case_step": case_step,
        "execution_started": "yes",
    }


def _run_repo(runtime, *, project_id: str, case_id: str, platform: str, repo: dict[str, str], case: dict) -> None:
    import ast

    from mino_nexus.casework.script_workspace import (
        hook_file, resolve_script, sync_workspace, venv_python,
    )
    from mino_nexus.loop.script_bridge import run_in_workspace

    checkout = sync_workspace(project_id, repo)
    try:
        script = resolve_script(checkout, case_id, platform, str(case.get("script_path") or ""))
    except ValueError as exc:
        raise ScriptAbort("脚本路径必须是仓库内的 .py 相对路径", reason_code="script_path_invalid") from exc
    if script is None:
        raise ScriptAbort(
            f"仓库里没有这条用例的脚本 {case.get('script_path') or 'cases/' + case_id + '.py'}",
            reason_code="script_missing",
        )

    def has(path, name: str) -> bool:
        if path is None:
            return False
        tree = ast.parse(path.read_text(encoding="utf-8"))
        return any(isinstance(node, ast.FunctionDef) and node.name == name for node in tree.body)

    before = []
    project_pre = hook_file(checkout, "pretest")
    if has(project_pre, "pretest"):
        before.append({"path": str(project_pre), "entry": "pretest"})
    if has(script, "pretest"):
        before.append({"path": str(script), "entry": "pretest"})
    before.append({"path": str(script), "entry": "run"})
    after = []
    if has(script, "posttest"):
        after.append({"path": str(script), "entry": "posttest"})
    project_post = hook_file(checkout, "posttest")
    if has(project_post, "posttest"):
        after.append({"path": str(project_post), "entry": "posttest"})
    head = _git_head(checkout)
    if runtime.writer is not None:
        runtime.writer.append("script/repo", {"head": head, "script": str(script.name), "platform": platform})
    run_in_workspace(python=venv_python(project_id), plan={"before": before, "after": after}, runtime=runtime)


def _git_head(checkout) -> str:
    import subprocess
    try:
        proc = subprocess.run(["git", "-C", str(checkout), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _load_all(bundle: dict[str, str], files: dict[str, str]) -> dict[str, Callable | None]:
    mapping = {
        "project_pretest.py": ("pretest", bundle.get("project_pretest") or ""),
        "case_pretest.py": ("pretest", bundle.get("case_pretest") or ""),
        "case_run.py": ("run", bundle.get("run") or ""),
        "case_posttest.py": ("posttest", bundle.get("case_posttest") or ""),
        "project_posttest.py": ("posttest", bundle.get("project_posttest") or ""),
    }
    loaded: dict[str, Callable | None] = {}
    for filename, (role, source) in mapping.items():
        loaded[filename] = _compile(source, filename, role, files)
    if loaded["case_run.py"] is None:
        raise ScriptRejected("用例没有 run(case)", line=1)
    return loaded


def _compile(source: str, filename: str, role: str, files: dict[str, str]) -> Callable | None:
    text = str(source or "")
    if not text.strip():
        return None
    files[filename] = text
    check_script(text, role=role)
    namespace = {"__builtins__": builtins}
    exec(compile(text, filename, "exec"), namespace, namespace)  # noqa: S102 — 受检后的用例脚本
    fn = namespace.get(role)
    if not callable(fn):
        raise ScriptRejected(f"缺少 {role}(case)", line=1)
    return fn


def _call(runtime: CaseRuntime, phase: str, fn: Callable | None) -> None:
    if fn is None:
        return
    runtime.begin_phase(phase)
    if runtime.writer is not None:
        runtime.writer.set_phase(phase)
        runtime.writer.append(f"script/{phase}", {"status": "start", "phase": phase})
    previous = sys.gettrace()
    sys.settrace(runtime.watch)
    try:
        fn(runtime)
    finally:
        sys.settrace(previous)
    if runtime.writer is not None:
        runtime.writer.append(f"script/{phase}", {"status": "pass", "phase": phase})


def _failure(runtime: CaseRuntime, exc: BaseException, files: dict[str, str], *, phase: str = "") -> tuple[str, str, str, str]:
    stage = phase or runtime.phase or "run"
    if isinstance(exc, ScriptHalt) and exc.reason_code == "task_cancelled":
        status = "cancelled"
    elif isinstance(exc, ScriptHalt):
        status = "blocked" if exc.reason_code == "case_budget_exhausted" else "fail"
    elif isinstance(exc, ScriptAbort) and exc.reason_code == "device_channel_unavailable":
        status = "fail"
    else:
        status = "fail"
    located = _locate(exc, files)
    node = runtime.current_step
    message = str(exc) or type(exc).__name__
    if located["line"] and node is not None:
        message = f"{node.instruction} · 第 {located['line']} 行 · {located['source']} · {message}"
    elif located["line"]:
        message = f"{stage} · 第 {located['line']} 行 · {located['source']} · {message}"
    reason = getattr(exc, "reason_code", "") or "script_error"
    return status, message[:300], reason, stage


def _write_error(runtime: CaseRuntime, exc: BaseException, *, phase: str, files: dict[str, str]) -> None:
    located = _locate(exc, files)
    node = runtime.current_step
    payload = {
        "phase": phase,
        "n": located["n"] or (node.n if node else 0),
        "instruction": located["instruction"] or (node.instruction if node else ""),
        "line": located["line"],
        "source": located["source"],
        "filename": located["filename"],
        "exc_type": type(exc).__name__,
        "message": str(exc)[:300],
        "case_step": located["n"] or (node.n if node else 0),
        "status": "fail",
    }
    instruction = str(payload["instruction"] or "")
    line = int(payload["line"] or 0)
    summary = str(exc)[:300]
    if line:
        summary = f"第 {line} 行 {payload['source']} · {summary}"
    if instruction:
        summary = f"{instruction} · {summary}"
    visible = {
        "phase": "result",
        "step": int(payload["n"] or 0),
        "step_text": instruction,
        "thought": instruction or phase,
        "summary": summary[:300],
        "status": "fail",
        "line": line,
        "script_source": payload["source"],
        "exc_type": payload["exc_type"],
        "loop_phase": phase,
        "source": "case_script",
    }
    if runtime.writer is not None:
        runtime.writer.append("script/error", payload, phase=phase)
        runtime.writer.append("stream/emit", visible)
    emit_agent_event({
        "run_id": runtime.stream_id,
        "task_id": runtime.run_id,
        "case_id": runtime.case_id,
        "app_id": runtime.app_id,
        **visible,
    })


def _locate(exc: BaseException, files: dict[str, str]) -> dict[str, Any]:
    frames = traceback.extract_tb(exc.__traceback__)
    for frame in reversed(frames):
        if frame.filename not in _FILENAMES:
            continue
        line = int(frame.lineno or 0)
        rows = str(files.get(frame.filename) or "").splitlines()
        source = rows[line - 1].strip() if 1 <= line <= len(rows) else ""
        return {"filename": frame.filename, "line": line, "source": source, "n": 0, "instruction": ""}
    return {"filename": "", "line": 0, "source": "", "n": 0, "instruction": ""}

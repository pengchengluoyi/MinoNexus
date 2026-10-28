"""父进程桥：虚拟环境里的脚本把 case.* 交回当前这条用例的运行时。"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from mino_nexus.loop.script_runtime import ActionResult, CaseRuntime, ScriptAbort, ScriptHalt
from mino_nexus.platform.identity import user_thread


class ScriptBridge:
    def __init__(self, runtime: CaseRuntime):
        self.runtime = runtime
        self.error = ""
        self.reason_code = ""
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(1)
        self._server.settimeout(0.5)
        self.port = int(self._server.getsockname()[1])
        self._closed = False

    def serve_until(self, process: subprocess.Popen) -> None:
        conn = None
        try:
            while process.poll() is None and conn is None and not self._closed:
                try:
                    conn, _ = self._server.accept()
                except socket.timeout:
                    continue
            if conn is None:
                return
            conn.settimeout(0.5)
            pending = b""
            while process.poll() is None and not self._closed:
                try:
                    chunk = conn.recv(65536)
                except (socket.timeout, TimeoutError, OSError):
                    continue
                if not chunk:
                    break
                pending += chunk
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    if not line.strip():
                        continue
                    reply = json.dumps(self._handle(line), ensure_ascii=False).encode() + b"\n"
                    conn.sendall(reply)
        finally:
            if conn is not None:
                conn.close()

    def close(self) -> None:
        self._closed = True
        self._server.close()

    def _handle(self, line: bytes) -> dict[str, Any]:
        try:
            message = json.loads(line.decode())
            ident = message.get("id")
            result = self._call(str(message.get("method") or ""), list(message.get("args") or []), dict(message.get("kwargs") or {}))
            return {"id": ident, "ok": True, "result": _encode(result)}
        except ScriptHalt as exc:
            self.error = str(exc)
            self.reason_code = exc.reason_code
            return {"id": None, "ok": False, "error": str(exc), "reason_code": exc.reason_code}
        except ScriptAbort as exc:
            self.error = str(exc)
            self.reason_code = exc.reason_code
            return {"id": None, "ok": False, "error": str(exc), "reason_code": exc.reason_code}
        except Exception as exc:
            self.error = str(exc)
            self.reason_code = getattr(exc, "reason_code", "") or "script_failed"
            return {"id": None, "ok": False, "error": str(exc), "reason_code": self.reason_code}

    def _call(self, method: str, args: list, kwargs: dict):
        if method == "phase":
            self.runtime.begin_phase(str(args[0]))
            return None
        if method == "enter":
            self.runtime._enter_step(int(args[1]), str(args[0]))
            return None
        if method == "leave":
            self.runtime._leave_step(failed=bool(args[0]))
            return None
        target = getattr(self.runtime, method)
        return target(*args, **kwargs)


def run_in_workspace(
    *,
    python: Path,
    plan: dict[str, list],
    runtime: CaseRuntime,
    output_limit: int = 200_000,
) -> tuple[int, str]:
    """用虚拟环境的 python 跑计划。超时或取消时杀掉进程。"""
    bridge = ScriptBridge(runtime)
    worker = Path(__file__).with_name("script_worker.py")
    env = os.environ.copy()
    env["MINO_CASE_BRIDGE"] = f"127.0.0.1:{bridge.port}"
    env["MINO_CASE_PLAN"] = json.dumps(plan, ensure_ascii=False)
    env.pop("PYTHONPATH", None)
    process = subprocess.Popen(
        [str(python), str(worker)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    captured: list[bytes] = []

    def _drain(pipe) -> None:
        if pipe is None:
            return
        captured.append(pipe.read()[:output_limit])

    threads = [
        threading.Thread(target=_drain, args=(process.stdout,), daemon=True),
        threading.Thread(target=_drain, args=(process.stderr,), daemon=True),
    ]
    for item in threads:
        item.start()
    thread = user_thread(target=bridge.serve_until, args=(process,), daemon=True, name="script-bridge")
    thread.start()
    killed = ""
    try:
        while process.poll() is None:
            if runtime.cancel_check and runtime.cancel_check():
                killed = "任务已取消"
                process.kill()
                break
            if runtime.deadline and time.time() >= runtime.deadline and runtime.phase != "posttest":
                killed = "超过用例时间，已停止脚本进程"
                process.kill()
                break
            time.sleep(0.1)
    finally:
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
        bridge.close()
        thread.join(timeout=2)
        for item in threads:
            item.join(timeout=2)
    output = b"".join(captured).decode("utf-8", "replace")[:output_limit]
    if killed:
        raise ScriptHalt(killed, reason_code="task_cancelled" if "取消" in killed else "case_budget_exhausted")
    if bridge.error and process.returncode:
        raise ScriptAbort(bridge.error, reason_code=bridge.reason_code or "script_failed")
    if process.returncode:
        detail = output.strip() or bridge.error or f"脚本进程退出 {process.returncode}"
        raise ScriptAbort(detail[:500], reason_code="script_failed")
    return int(process.returncode or 0), output


def _encode(value: Any):
    if isinstance(value, ActionResult):
        return {"__kind__": "result", "ok": value.ok, "summary": value.summary, "status": value.status, "data": value.data}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return value
    data = getattr(value, "__dict__", None)
    if isinstance(data, dict) and "email" in data or (isinstance(data, dict) and "id" in data and "password" in data):
        return {"__kind__": "account", **{key: item for key, item in data.items() if not str(key).startswith("_")}}
    return value

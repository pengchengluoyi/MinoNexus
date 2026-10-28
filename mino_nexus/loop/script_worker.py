"""在用例虚拟环境里跑仓库脚本。本文件只用标准库，由该环境的 python 启动。

设备和模型不在这里实现。case.* 通过本地套接字交给 Nexus，再由 Nexus 派给 Scout。
"""
from __future__ import annotations

import importlib.util
import json
import os
import socket
import sys
import threading
import time
import traceback
from types import SimpleNamespace


class Result:
    def __init__(self, payload: dict):
        self.ok = bool(payload.get("ok"))
        self.summary = str(payload.get("summary") or "")
        self.status = str(payload.get("status") or "")
        self.data = dict(payload.get("data") or {})

    def __bool__(self) -> bool:
        return self.ok


class Scope:
    def __init__(self, client: "CaseClient", role: str, number: int):
        self._client = client
        self._role = role
        self._number = number

    def __enter__(self):
        self._client.call("enter", self._role, self._number)
        return self._client

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._client.call("leave", exc_type is not None)
        return False


class CaseClient:
    def __init__(self, sock: socket.socket):
        self._sock = sock
        self._pending = b""
        self._seq = 0

    def prep(self, number: int) -> Scope:
        return Scope(self, "prep", int(number))

    def do(self, number: int) -> Scope:
        return Scope(self, "do", int(number))

    def check(self, number: int) -> Scope:
        return Scope(self, "check", int(number))

    def __getattr__(self, name: str):
        def call(*args, **kwargs):
            value = self.call(name, *args, **kwargs)
            return _decode(value)
        return call

    def call(self, method: str, *args, **kwargs):
        self._seq += 1
        ident = self._seq
        self._send({"id": ident, "method": method, "args": args, "kwargs": kwargs})
        reply = self._recv()
        if not reply.get("ok"):
            raise RuntimeError(str(reply.get("error") or "脚本调用失败"))
        return reply.get("result")

    def _send(self, payload: dict) -> None:
        self._sock.sendall(json.dumps(payload, ensure_ascii=False).encode() + b"\n")

    def _recv(self) -> dict:
        while b"\n" not in self._pending:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise RuntimeError("与执行服务的连接已断开")
            self._pending += chunk
        line, self._pending = self._pending.split(b"\n", 1)
        body = json.loads(line.decode())
        if not isinstance(body, dict):
            raise RuntimeError("执行服务返回了无法识别的结果")
        return body


def _decode(value):
    if isinstance(value, dict) and value.get("__kind__") == "result":
        return Result(value)
    if isinstance(value, dict) and value.get("__kind__") == "account":
        data = {key: item for key, item in value.items() if key != "__kind__"}
        return SimpleNamespace(**data)
    return value


def _load(path: str, entry: str):
    spec = importlib.util.spec_from_file_location(f"case_{entry}_{os.path.basename(path)}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载 {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fn = getattr(module, entry, None)
    if not callable(fn):
        raise RuntimeError(f"{path} 没有 {entry}(case)")
    return fn


def _exit_when_parent_dies(parent_pid: int) -> None:
    """父进程消失后自己退出。看门狗在父进程里，父进程被杀掉就没人再杀这个循环。"""
    while True:
        time.sleep(0.2)
        if os.getppid() != parent_pid:
            os._exit(0)


def main() -> int:
    threading.Thread(target=_exit_when_parent_dies, args=(os.getppid(),), daemon=True).start()
    plan = json.loads(os.environ.get("MINO_CASE_PLAN") or "{}")
    address = os.environ.get("MINO_CASE_BRIDGE") or ""
    host, _, port = address.rpartition(":")
    sock = socket.create_connection((host or "127.0.0.1", int(port)), timeout=30)
    client = CaseClient(sock)
    failed = ""
    try:
        try:
            for item in plan.get("before") or []:
                _load(item["path"], item["entry"])(client)
        except Exception as exc:
            failed = str(exc)
            traceback.print_exc()
        finally:
            try:
                client.call("phase", "posttest")
            except Exception:
                pass
            for item in plan.get("after") or []:
                try:
                    _load(item["path"], item["entry"])(client)
                except Exception:
                    traceback.print_exc()
                    if not failed:
                        failed = "后置脚本失败"
    finally:
        sock.close()
    if failed:
        print(failed, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

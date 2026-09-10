"""Scout 节点接入：`WS /node`。

与上游 MiniOrangeServer 的关系：上游 `server/websocket/device_manager.py`（1,623 行）
同时干三件事 —— UI observers 广播、ClawNode 连接管理、adb 通道分叉。拆分后：
  - UI observers 广播        → `mino_nexus/websocket/observers.py`
  - ClawNode 连接管理        → **MinoScout** 的 `clawnode/`
  - **Scout 节点接入 → 本文件**（新写）

这一层只做搬运：收帧 → 交 NodeRegistry / 唤醒等待者；发帧 → 等应答。
选路与业务在 `loop/router_proxy.py`。
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import WebSocket, WebSocketDisconnect

from mino_nexus.core import protocol as P
from mino_nexus.core.log import SLog
from mino_nexus.services.node_registry import get_registry

TAG = "NodeWS"

NEXUS_VERSION = "0.1.7"
HEARTBEAT_INTERVAL_SEC = 15

# 协议 §1
MAX_FRAME_BYTES = 32 * 1024 * 1024

_seq = 0


def _msg_id() -> str:
    global _seq
    _seq += 1
    return f"{int(time.time() * 1000):013d}{_seq:06d}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


class NodeConnection:
    """一条 Scout 连接。负责编解码、请求-应答配对、超时。"""

    def __init__(self, ws: Any):
        self.ws = ws
        self.node_id = ""
        self._waiters: dict[str, asyncio.Future] = {}
        self._closed = False

    # ---------------- 对外：发请求等应答 ----------------

    async def request(
        self,
        mtype: P.MsgType,
        payload: Any,
        *,
        timeout: float = 30.0,
    ) -> Optional[P.Result]:
        """发一条 N→S 请求并等它的 RESULT。超时返回 None。

        协议 §6：调用方（RouterProxy）负责按同一 `(run_id, step_idx)` 重发一次；
        Scout 侧有幂等缓存，重发不会重复操作设备。
        """
        if self._closed:
            return None
        env = P.Envelope(type=mtype, msg_id=_msg_id(), ts=_now_iso(), payload=payload)
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._waiters[env.msg_id] = fut
        try:
            await self.ws.send_text(json.dumps(P.dumps(env), ensure_ascii=False))
        except Exception as exc:
            self._waiters.pop(env.msg_id, None)
            SLog.e(TAG, f"[{self.node_id}] 发送 {mtype.value} 失败: {exc}")
            return None
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._waiters.pop(env.msg_id, None)
            SLog.w(TAG, f"[{self.node_id}] {mtype.value} 等 RESULT 超时 {timeout}s")
            return None
        except asyncio.CancelledError:
            self._waiters.pop(env.msg_id, None)
            SLog.w(TAG, f"[{self.node_id}] {mtype.value} 等待被取消（节点断开）")
            return None

    # ---------------- 收帧 ----------------

    async def on_text(self, raw: str) -> None:
        try:
            env = P.loads(json.loads(raw))
        except P.UnknownMessageType as exc:
            # 协议 §7：未知 type 记 warn 并忽略，不可关连接
            SLog.w(TAG, f"[{self.node_id}] 未知消息类型 {exc}，忽略（协议 §7）")
            return
        except Exception as exc:
            SLog.e(TAG, f"[{self.node_id}] 解码失败，忽略: {type(exc).__name__}: {exc}")
            return

        if env.v != P.PROTOCOL_VERSION:
            raise RuntimeError(f"Scout 用了协议 v{env.v}，本 Nexus 是 v{P.PROTOCOL_VERSION}")

        # RESULT 是应答，交给等待者
        if env.type is P.MsgType.RESULT:
            fut = self._waiters.pop(env.reply_to or "", None)
            if fut is None:
                SLog.w(TAG, f"[{self.node_id}] 收到无人等待的 RESULT（reply_to={env.reply_to}），忽略")
                return
            if not fut.done():
                fut.set_result(env.payload)
            return

        reg = get_registry()
        if env.type is P.MsgType.REGISTER:
            await self._on_register(env)
        elif env.type is P.MsgType.HEARTBEAT:
            reg.heartbeat(env.payload)
        elif env.type is P.MsgType.EXECUTE:
            await self._on_inbound_execute(env)
        else:
            SLog.w(TAG, f"[{self.node_id}] 收到不该由 Nexus 处理的消息 {env.type.value}，忽略")

    async def _on_inbound_execute(self, env: P.Envelope) -> None:
        req: P.Execute = env.payload
        cap = str(req.capability_id or "")
        if cap not in P.NODE_EVENT_CAPS:
            SLog.w(TAG, f"[{self.node_id}] 忽略非框架入站 EXECUTE cap={cap}")
            await self._reply_result(env, P.Result(
                run_id=req.run_id, step_idx=req.step_idx,
                status=P.EventStatus.FAIL, summary=f"Nexus 不处理 cap={cap}",
                error=f"Nexus 不处理 cap={cap}", executor_used="nexus",
            ))
            return
        interrupted = get_registry().node_event(req, node_id=self.node_id)
        if interrupted:
            from mino_nexus.services.run_store import interrupt_runs

            event = cap.split(".", 1)[-1]
            interrupt_runs(interrupted, reason=f"节点 {self.node_id} {event}")
        await self._reply_result(env, P.Result(
            run_id=req.run_id, step_idx=req.step_idx,
            status=P.EventStatus.PASS, summary=f"acked {cap}",
            executor_used="nexus",
        ))

    async def _on_register(self, env: P.Envelope) -> None:
        req: P.Register = env.payload
        reg = get_registry()

        if req.protocol_version != P.PROTOCOL_VERSION:
            await self._reply(env, P.Registered(
                accepted=False, nexus_version=NEXUS_VERSION,
                reason=f"协议版本不一致：Scout v{req.protocol_version} vs Nexus "
                       f"v{P.PROTOCOL_VERSION}。按协议 §7 不做降级协商",
            ))
            return

        if not _token_ok(req.token):
            await self._reply(env, P.Registered(
                accepted=False, nexus_version=NEXUS_VERSION,
                reason="配对 token 无效",
            ))
            SLog.w(TAG, f"拒绝注册 node={req.node_id}：token 无效")
            return

        self.node_id = req.node_id
        owner_user_id = ""
        from mino_nexus.services.runtime_tokens import peek_token

        info = peek_token(req.token)
        if info:
            owner_user_id = str(info.get("user_id") or "")
        warnings = reg.register(req, send=self.request, owner_user_id=owner_user_id)
        await self._reply(env, P.Registered(
            accepted=True,
            nexus_version=NEXUS_VERSION,
            session_token=f"sess-{_msg_id()}",
            heartbeat_interval_sec=HEARTBEAT_INTERVAL_SEC,
            warnings=warnings,
        ))

    async def _reply_result(self, req_env: P.Envelope, payload: P.Result) -> None:
        env = P.Envelope(
            type=P.MsgType.RESULT, msg_id=_msg_id(), ts=_now_iso(),
            payload=payload, reply_to=req_env.msg_id,
        )
        await self.ws.send_text(json.dumps(P.dumps(env), ensure_ascii=False))

    async def _reply(self, req_env: P.Envelope, payload: Any) -> None:
        env = P.Envelope(
            type=P.MsgType.REGISTERED, msg_id=_msg_id(), ts=_now_iso(),
            payload=payload, reply_to=req_env.msg_id,
        )
        await self.ws.send_text(json.dumps(P.dumps(env), ensure_ascii=False))

    def close(self) -> None:
        self._closed = True
        for fut in self._waiters.values():
            if not fut.done():
                fut.cancel()
        self._waiters.clear()
        if self.node_id:
            runs = get_registry().disconnect(self.node_id)
            if runs:
                from mino_nexus.services.run_store import interrupt_runs

                interrupt_runs(runs, reason=f"节点 {self.node_id} 断开")


def _token_ok(token: str) -> bool:
    """配对 token 校验。

    顺序：Studio 领取的短 TTL 安装凭证 → 环境变量 `MINO_NEXUS_NODE_TOKEN` →
    本地开发时任意非空 token。多节点上线前必须换成每节点独立、可吊销、与
    node_id 绑定（docs/NODE_REGISTRY.md §6）。
    """
    import os

    tok = str(token or "").strip()
    if not tok:
        return False
    from mino_nexus.services.runtime_tokens import install_token_ok

    if install_token_ok(tok):
        return True
    expect = os.environ.get("MINO_NEXUS_NODE_TOKEN", "")
    if expect:
        return tok == expect
    return True


# ---------------- FastAPI 路由 ----------------


def register_routes(app: Any) -> None:
    # 注意：WebSocket 必须**模块级** import。本文件用了
    # `from __future__ import annotations`，注解是字符串，FastAPI 会拿模块 globals
    # 去 eval —— 函数内 import 会让它 NameError（踩过一次）。
    @app.websocket("/node")
    async def node_endpoint(ws: WebSocket) -> None:  # pragma: no cover - 需要真连接
        await ws.accept()
        conn = NodeConnection(ws)
        SLog.i(TAG, "Scout 连入，等 REGISTER")
        try:
            while True:
                raw = await ws.receive_text()
                await conn.on_text(raw)
        except WebSocketDisconnect:
            SLog.i(TAG, f"[{conn.node_id or '未注册'}] 断开")
        except Exception as exc:
            SLog.e(TAG, f"[{conn.node_id or '未注册'}] 连接异常: {type(exc).__name__}: {exc}")
        finally:
            conn.close()

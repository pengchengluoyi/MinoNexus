"""EXECUTE 辅助编号：保留任务归属，区分新请求和同请求重取。"""
from __future__ import annotations

import threading
import time

from mino_nexus.core.protocol import Execute

_LOCK = threading.RLock()
# 启动微秒分代，后端重启后不与仍在线 Scout 保存的旧辅助键重号。
_AUXILIARY_STEP = -(time.time_ns() // 1000)


def next_auxiliary_step_idx(run_id: str = "") -> int:
    """全进程唯一的负编号；-1 留作哨兵，不消耗任何 run 的正写编号。"""
    global _AUXILIARY_STEP
    with _LOCK:
        _AUXILIARY_STEP -= 1
        return _AUXILIARY_STEP


def assign_execute_identity(request: Execute) -> None:
    """首次发送补辅助编号；同一载荷重发保留身份和完整内容。"""
    global _AUXILIARY_STEP
    with _LOCK:
        if not request.run_id and request.device_hint.get("execution_mode") == "v5":
            raise ValueError("V5 EXECUTE 缺少 run_id")
        if request.step_idx == -1:
            request.step_idx = next_auxiliary_step_idx(request.run_id)
        elif request.step_idx < -1:
            _AUXILIARY_STEP = min(request.step_idx, _AUXILIARY_STEP)
        # run_id 原样保留。无任务的插件 RPC 仍为空，不伪造活动任务或设备负载。

"""跑批时间预算：截止时间与剩余秒数，纯数据运算。"""
from __future__ import annotations

import time


def deadline(doc, case=None):
    limits = [float((doc.get("execution_policy") or {}).get("batch_deadline_at") or 0)]
    if case:
        limits.append(float(case.get("attempt_deadline_at") or 0))
    return min((value for value in limits if value > 0), default=0)


def remaining(doc, case=None):
    end = deadline(doc, case)
    return max(0.0, end - time.time()) if end else None

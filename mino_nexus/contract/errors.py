"""跨域共享的异常类型。"""
from __future__ import annotations

from copy import deepcopy


class BindingAdmissionError(ValueError):
    def __init__(self, reason_code, diagnostics=()):
        self.reason_code = reason_code
        self.diagnostics = deepcopy(list(diagnostics))
        super().__init__(str(reason_code))


class RuntimeReadOnlyError(RuntimeError):
    """执行期只读：运行时不允许改配置。"""

    def __init__(self, reason: str = ""):
        self.reason = reason
        super().__init__(f"只读：{reason}" if reason else "只读")

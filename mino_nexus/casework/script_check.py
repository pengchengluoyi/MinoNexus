"""确认脚本能被执行。不限制导入、调用或属性访问。"""
from __future__ import annotations

import ast

_MAX_CHARS = 60000


class ScriptRejected(ValueError):
    reason_code = "script_rejected"

    def __init__(self, message: str, *, line: int = 0):
        self.line = int(line or 0)
        super().__init__(message)


def check_script(source: str, *, role: str) -> None:
    """role 是 run、pretest 或 posttest。空脚本不检查。"""
    text = str(source or "")
    if not text.strip():
        return
    if len(text) > _MAX_CHARS:
        raise ScriptRejected(f"脚本超过 {_MAX_CHARS} 字", line=1)
    if role not in {"run", "pretest", "posttest"}:
        raise ScriptRejected(f"未知脚本角色 {role}")
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        raise ScriptRejected(exc.msg or "脚本语法错误", line=int(exc.lineno or 0)) from exc
    if not any(isinstance(node, ast.FunctionDef) and node.name == role for node in tree.body):
        raise ScriptRejected(f"脚本要定义 {role}(case)", line=1)

"""用例文本的规则解析。

port(nexus): 从上游 `server/services/shared/semantic/case_text_semantic_service.py`
（417 行）里**只摘出 `parse_numbered_items_rules`** —— planner 里唯一用到的那个。

那个文件其余部分依赖 `expectation_semantic_service`（会调 LLM 做语义比对），
属于 shared/semantic 整体，等它那一批一起搬。这里先取纯正则的部分。
"""
from __future__ import annotations

import re

from mino_nexus.core.case_text import parse_numbered_items_rules  # noqa: F401
from typing import Any, Dict, List


from mino_nexus.core.case_text import _NUMBERED_PATTERN  # noqa: F401,E402


from mino_nexus.core.case_text import _strip_number_prefix  # noqa: F401,E402

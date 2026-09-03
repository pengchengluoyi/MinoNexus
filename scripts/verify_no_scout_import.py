#!/usr/bin/env python3
"""硬约束 3：依赖必须单向。Nexus 不 import Scout，也不 import 上游 server / driver 包。"""
import sys
from _scan import scan

sys.exit(scan(
    "verify_no_scout_import",
    "不 import mino_scout，也不 import 上游 MiniOrangeServer 的 server.* / driver.*",
    {
        r"\bfrom\s+mino_scout\b|\bimport\s+mino_scout\b": "import 了 Scout",
        r"\bfrom\s+server\.|\bimport\s+server\.": "import 了上游 server 包（搬迁时要改成本仓路径）",
        r"\bfrom\s+driver\.|\bimport\s+driver\.": "import 了上游 driver 包（该部分归 Scout）",
    },
))

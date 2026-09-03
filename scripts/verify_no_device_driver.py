#!/usr/bin/env python3
"""硬约束 1+2：Nexus 不碰设备、不含图像算法依赖。

设备操作全部经协议委托给 Scout。Nexus 一旦直连设备就没法上云、没法多节点。
图像算法（CLIP / OCR / 组件检测）在拆分时已确认批次路径零调用 —— 引回来等于
把上游 45% 的旧代码拖回来（含 torch / paddle 几个 G 的依赖）。

例外：pillow 允许 —— 缩略图压缩（make_thumb）需要它，且只做重采样，不做识别。

**匹配的是"调用/导入"，不是"提到"。** `"adb"` / `"playwright"` 作为 executor id
字符串在本仓到处都是（选路、菜单、连通性），把它们当违规会逼着人到处加豁免，
最后守门脚本被关掉 —— 那比没有守门更糟。
"""
import sys
from _scan import scan

sys.exit(scan(
    "verify_no_device_driver",
    "不 import 设备驱动、不真的调 adb/浏览器、不 import 图像算法库",
    {
        # ---- 设备驱动：只看 import ----
        r"^\s*(?:from|import)\s+(adbutils|uiautomator2|facebook_wda|wda|appium|selenium)\b":
            "import 了设备驱动",
        r"^\s*(?:from|import)\s+playwright\b": "import 了 Playwright",
        r"^\s*(?:from|import)\s+(pywinauto|uiautomation|comtypes|pynput)\b":
            "import 了本机 UI/输入驱动",
        # ---- 真的去调 adb（而不是把 "adb" 当 executor id 用）----
        r"""subprocess\.\w+\(\s*\[?\s*["']adb["']""": "subprocess 直接调 adb",
        r"""["']adb["']\s*,\s*["']-s["']""": "拼 adb -s 命令行",
        r"\badb\s+(shell|exec-out|devices|install)\b": "内嵌 adb 命令",
        # ---- 图像算法：只看 import ----
        r"^\s*(?:from|import)\s+(torch|torchvision|open_clip|transformers)\b":
            "import 了 torch / CLIP 系",
        r"^\s*(?:from|import)\s+(paddleocr|paddle|rapidocr|ultralytics|onnxruntime)\b":
            "import 了 OCR / 检测模型",
        r"^\s*(?:from|import)\s+(cv2|numpy)\b.*#\s*vision": "为视觉算法引入 cv2/numpy",
        r"^\s*(?:from|import)\s+cv2\b": "import 了 opencv",
    },
))

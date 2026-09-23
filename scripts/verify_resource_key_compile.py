#!/usr/bin/env python3
"""用例密钥编译：设备/账号登录态拆分、账号与数据「字段名-状态」。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mino_nexus.services.account_requirement_compile import (  # noqa: E402
    TITLE_DEVICE_LOGIN,
    augment_requirements_from_precondition,
    classify_precondition_title,
)
from mino_nexus.services.case_resource_claim import (  # noqa: E402
    compile_resource_key_from_precondition,
)
from mino_nexus.services.resource_pool import empty_requirements  # noqa: E402

DEFS = [
    {
        "key": "field_7065t5",
        "label": "形象",
        "data_kind": "static",
        "options": [
            {"value": "unknown", "label": "未设置"},
            {"value": "no", "label": "未配置形象"},
            {"value": "yes", "label": "已配置形象"},
        ],
    },
    {
        "key": "flow_cdxvtf",
        "label": "新人引导",
        "data_kind": "dynamic",
        "options": [
            {"value": "stage_3", "label": "已配置形象"},
            {"value": "stage_4", "label": "老用户"},
        ],
    },
    {
        "key": "login_flow",
        "label": "登录流程",
        "data_kind": "dynamic",
        "options": [
            {"value": "logged_out", "label": "未登录"},
            {"value": "logged_in", "label": "已登录"},
        ],
    },
    {
        "key": "field_c4ziyt",
        "label": "个人信息配置",
        "data_kind": "static",
        "options": [
            {"value": "yes", "label": "已配置"},
            {"value": "no", "label": "未配置"},
        ],
    },
]


def _all_facets(req: dict) -> set[str]:
    out = set()
    for bucket in ("all", "prefer"):
        for c in req.get(bucket) or []:
            if isinstance(c, dict) and c.get("facet"):
                out.add(str(c["facet"]))
    return out


def _all_eq(req: dict, facet: str) -> str | None:
    for c in req.get("all") or []:
        if isinstance(c, dict) and c.get("facet") == facet:
            return str(c.get("value") or "")
    return None


def main() -> int:
    failed = 0

    if classify_precondition_title("登录态") != TITLE_DEVICE_LOGIN:
        print("FAIL: 旧「登录态」应视为设备登录态")
        failed += 1
    if classify_precondition_title("设备登录态") != TITLE_DEVICE_LOGIN:
        print("FAIL: 设备登录态分类")
        failed += 1
    if classify_precondition_title("账号登录态") == TITLE_DEVICE_LOGIN:
        print("FAIL: 账号登录态不应当成设备")
        failed += 1

    old_logged_out = (
        "1. 登录态：未登录\n"
        "2. 账号与数据：已配置形象\n"
        "3. 环境与权限：清除应用缓存"
    )
    req = augment_requirements_from_precondition(
        empty_requirements(), old_logged_out, None, field_defs=DEFS
    )
    if "login_flow" in {c.get("facet") for c in (req.get("all") or []) if isinstance(c, dict)}:
        print("FAIL: 设备未登录不应把 login_flow 写进 all", req)
        failed += 1
    if _all_eq(req, "flow_cdxvtf") == "stage_3":
        print("FAIL: 已配置形象不应绑到新人引导 stage_3", req)
        failed += 1
    if _all_eq(req, "field_7065t5") != "yes":
        print("FAIL: 已配置形象应落到 field_7065t5=yes", req)
        failed += 1

    bare_status = "1. 设备登录态：未登录\n2. 账号与数据：已配置形象"
    req_bare = augment_requirements_from_precondition(
        empty_requirements(), bare_status, None, field_defs=DEFS
    )
    if _all_eq(req_bare, "field_7065t5") != "yes":
        print("FAIL: 账号与数据仅写状态 已配置形象", req_bare)
        failed += 1

    named = "1. 设备登录态：未登录\n2. 账号与数据：形象-已配置形象"
    req2 = augment_requirements_from_precondition(
        empty_requirements(), named, None, field_defs=DEFS
    )
    if _all_eq(req2, "field_7065t5") != "yes":
        print("FAIL: 字段名-状态 形象-已配置形象", req2)
        failed += 1
    if "login_flow" in _all_facets(req2) and any(
        c.get("facet") == "login_flow" for c in (req2.get("all") or [])
    ):
        print("FAIL: 设备登录态 + 形象 不应硬约束 login_flow", req2)
        failed += 1

    acct = "1. 账号登录态：已登录\n2. 账号与数据：形象-已配置形象"
    req3 = augment_requirements_from_precondition(
        empty_requirements(), acct, None, field_defs=DEFS
    )
    sess = next(
        (c for c in (req3.get("all") or []) if isinstance(c, dict) and c.get("facet") == "session"),
        None,
    )
    if not sess or str(sess.get("value") or "") != "logged_in":
        print("FAIL: 账号登录态：已登录 应约束 session=logged_in", req3)
        failed += 1
    if any(c.get("facet") == "login_flow" for c in (req3.get("all") or []) if isinstance(c, dict)):
        print("FAIL: 账号登录态不应写 login_flow", req3)
        failed += 1

    claim = compile_resource_key_from_precondition(old_logged_out, env_doc={"account_facet_extensions": []})
    da = (claim.get("device_app") or {}).get("required_session")
    ars = (claim.get("account") or {}).get("required_session")
    if da not in ("logged_out", "guest"):
        print("FAIL: 旧登录态应编译为设备未登录", claim.get("device_app"))
        failed += 1
    if ars not in ("any", "", None):
        print("FAIL: 旧登录态不应设置账号登录态", claim.get("account"))
        failed += 1

    if failed:
        print(f"{failed} check(s) failed")
        return 1
    print("OK — resource key compile splits device/account login and field-status")
    return 0


if __name__ == "__main__":
    sys.exit(main())

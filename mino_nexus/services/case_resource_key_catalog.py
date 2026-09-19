"""用例密钥（ResourceClaim）词汇表：Console「用例密钥」页与编译器共用真源。"""
from __future__ import annotations

from typing import Any

CATALOG_VERSION = 1

# Claim 落库：case.meta.resource_key（v1）；与 case_scene 合并时 case_scene 优先同名字段。
CLAIM_STORAGE = {
    "primary": "case.meta.resource_key",
    "fallback_compile_from": ["precondition", "precondition_raw", "case_scene"],
    "scene_merge": "case_scene 与 resource_key 合并后 clamp_case_scene + lease_requirements",
}

LEASE_STORAGE = {
    "note": "跑批生成，勿写入用例",
    "ctx_fields": [
        "resource_lease.account_id",
        "resource_lease.project_id",
        "resource_lease.run_id",
        "resource_lease.facets",
        "picked_account",
        "sn（批次 sns[]）",
        "package_id（项目 App 包名）",
    ],
}


def catalog_payload() -> dict[str, Any]:
    return {
        "version": CATALOG_VERSION,
        "claim_storage": CLAIM_STORAGE,
        "lease_storage": LEASE_STORAGE,
        "precondition_format": _precondition_format(),
        "sections": _sections(),
        "entries": _entries(),
        "example_claim": _example_claim(),
        "example_precondition": _example_precondition_text(),
    }


def _precondition_format() -> dict[str, Any]:
    return {
        "lines": "编号行「类别：要求」；类别见各条 write_category",
        "machine_copy": "同步写入 case.meta.resource_key 或 case_scene（推荐二者由导入/Job 生成，人工以编号前置为准）",
    }


def _sections() -> list[dict[str, str]]:
    return [
        {"id": "account", "label": "账号（Claim → 号池选号）"},
        {"id": "device_app", "label": "设备 App 会话（Claim → 机态校验 / Prep）"},
        {"id": "device", "label": "设备（Claim → 派单过滤）"},
        {"id": "prep", "label": "前置动作（Claim → Prep 计划）"},
        {"id": "scene", "label": "CaseScene（Claim 子集，循环闸门）"},
        {"id": "lease", "label": "租约 Lease（运行时钥匙，勿手写）"},
    ]


def _entries() -> list[dict[str, Any]]:
    return [
        {
            "section": "account",
            "write_category": "登录态",
            "write_examples": ["未登录", "已登录", "游客"],
            "claim_path": "account.requirements / case_scene.lease_requirements",
            "resource": "pool_accounts + pool_account_facets",
            "config_keys": ["session", "login_status"],
            "dsl": "session eq logged_in | session in logged_out,guest,unknown",
            "runtime": "ensure_case_account_lease → 选号 DSL",
        },
        {
            "section": "account",
            "write_category": "账号与数据",
            "write_examples": ["已配置形象", "未配置形象", "号池模板选项标签（与 Console 号池模板字段一致）"],
            "claim_path": "account.requirements.all[]",
            "resource": "pool_account_facets",
            "config_keys": ["扩展 facet（如 avatar_config_status、field_*）"],
            "dsl": "facet eq/in，由 augment_requirements_from_precondition 编译",
            "runtime": "租号时 row_satisfies_requirements",
        },
        {
            "section": "account",
            "write_category": "账号环境",
            "write_examples": ["测试环境", "预发环境"],
            "claim_path": "account.env",
            "resource": "pool_accounts.env",
            "config_keys": ["env"],
            "dsl": "requirements.env = test|staging|…",
            "runtime": "仅匹配同 env 账号",
        },
        {
            "section": "account",
            "write_category": "号池模板",
            "write_examples": ["模板：已注册老用户（写 template_id 或 CaseScene.account_template_id）"],
            "claim_path": "account.template_id / case_scene.account_template_id",
            "resource": "project_pool_config + 号池模板目录",
            "config_keys": ["template_id", "default_facets", "facet_extensions"],
            "dsl": "augment_requirements_with_template",
            "runtime": "合并模板默认约束后再选号",
        },
        {
            "section": "device_app",
            "write_category": "登录态",
            "write_examples": ["未登录", "游客", "已登录"],
            "claim_path": "device_app.required_session / case_scene.required_session",
            "resource": "device_app_sessions（规划表）· RunContext.session_fact（当前）",
            "config_keys": ["session"],
            "dsl": "logged_out | guest | logged_in | any",
            "runtime": "Preflight 对照机态；不满足 → prep / inspect-session",
        },
        {
            "section": "device_app",
            "write_category": "绑定账号",
            "write_examples": ["登录后绑定租约账号（默认，无需写）", "显式：绑定账号须与租约一致"],
            "claim_path": "device_app.binding",
            "resource": "device_app_sessions.bound_account_id",
            "config_keys": ["must_match_lease"],
            "dsl": "binding: must_match_lease",
            "runtime": "登录流完成后写 bound_account_id = lease.account_id",
        },
        {
            "section": "device",
            "write_category": "端",
            "write_examples": ["Android", "iOS", "Web"],
            "claim_path": "platform / case_scene.platform",
            "resource": "m_device.platform",
            "config_keys": ["android", "ios", "web", "any"],
            "dsl": "platform eq android",
            "runtime": "批跑 sns[] 过滤平台；与 Scout 通道一致",
        },
        {
            "section": "device",
            "write_category": "设备形态",
            "write_examples": ["真机 App", "Web", "App+Web"],
            "claim_path": "device_need / case_scene.device_need",
            "resource": "派单菜单（executor 集合）",
            "config_keys": ["app", "web", "app_web", "ab_pair"],
            "dsl": "device_need",
            "runtime": "platform_gate / 能力菜单",
        },
        {
            "section": "prep",
            "write_category": "环境与权限",
            "write_examples": ["清除应用缓存"],
            "claim_path": "device_app.prep[] / case_scene.prep_items",
            "resource": "device_app_sessions + 账号 session（清缓存联动）",
            "config_keys": ["kind: clear_cache"],
            "dsl": 'prep_items: [{ "kind": "clear_cache", "phase": "before_launch" }]',
            "runtime": "Prep 执行 clear_app_cache；成功后 session→logged_out、绑定清除",
        },
        {
            "section": "prep",
            "write_category": "环境与权限",
            "write_examples": ["检查未登录", "检查已登录", "检查 App 版本"],
            "claim_path": "case_scene.prep_items",
            "resource": "device_app_sessions",
            "config_keys": ["check_not_logged_in", "check_logged_in", "check_app_version"],
            "dsl": "prep kind 见 session_gate.PREP_KINDS",
            "runtime": "after_launch 检查；失败则继续 prep 或拒跑",
        },
        {
            "section": "scene",
            "write_category": "前置登录策略",
            "write_examples": ["业务用例前置重登（隐式）", "登录模块用例勿自动登录"],
            "claim_path": "case_scene.session_prep",
            "resource": "RunContext（不持久化号池）",
            "config_keys": ["relogin", "logout", "skip"],
            "dsl": "relogin | logout | skip",
            "runtime": "session_ensure / 是否自动登录",
        },
        {
            "section": "scene",
            "write_category": "结构化租号",
            "write_examples": ["（Job 写入，一般不手写）"],
            "claim_path": "case_scene.lease_requirements",
            "resource": "pool_accounts",
            "config_keys": ["all", "prefer", "env"],
            "dsl": "与 account.requirements 同形",
            "runtime": "优先于纯文本编译的租号条件",
        },
        {
            "section": "lease",
            "write_category": "—",
            "write_examples": ["（跑批时 Nexus 写入，用例不写）"],
            "claim_path": "RunContext.resource_lease",
            "resource": "pool_accounts.lease + run_id",
            "config_keys": ["account_id", "run_id", "expires_at", "facets"],
            "dsl": "—",
            "runtime": "case 结束 / 取消 → release_run_lease",
        },
        {
            "section": "lease",
            "write_category": "—",
            "write_examples": ["（跑批参数 sns[] + 项目 App 包名）"],
            "claim_path": "批次 run + ctx.sn",
            "resource": "m_device + device_app_sessions",
            "config_keys": ["sn", "package_id", "node_id"],
            "dsl": "—",
            "runtime": "EXECUTE 路由；状态转移带 run_id 鉴权",
        },
    ]


def _example_precondition_text() -> str:
    return (
        "1. 登录态：未登录\n"
        "2. 账号与数据：已配置形象\n"
        "3. 环境与权限：清除应用缓存"
    )


def _example_claim() -> dict[str, Any]:
    return {
        "version": 1,
        "platform": "android",
        "device_need": "app",
        "target_app": {"app_id": "<项目内应用 ID>", "package": "<包名，派单解析>" },
        "device_app": {
            "required_session": "logged_out",
            "allow": ["logged_out", "guest", "unknown"],
            "prep": [{"kind": "clear_cache", "phase": "before_launch", "text": "清除应用缓存"}],
            "binding": "must_match_lease",
        },
        "account": {
            "env": "test",
            "template_id": "",
            "requirements": {
                "all": [
                    {"facet": "avatar_config_status", "op": "eq", "value": "configured"},
                    {"facet": "session", "op": "in", "value": "logged_out,guest,unknown"},
                    {"facet": "health", "op": "eq", "value": "available"},
                ],
                "prefer": [],
                "env": "test",
            },
        },
        "case_scene": {
            "required_session": "guest",
            "session_prep": "skip",
            "platform": "android",
            "device_need": "app",
            "prep_items": [
                {"kind": "clear_cache", "phase": "before_launch", "text": "清除应用缓存"},
            ],
        },
    }

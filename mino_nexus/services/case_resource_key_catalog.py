"""用例密钥（ResourceClaim）词汇表：Console「用例密钥」页与编译器共用真源。"""
from __future__ import annotations

from typing import Any

CATALOG_VERSION = 2

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

KEY_LAYERS = [
    {"id": "precondition", "label": "前置"},
    {"id": "operation", "label": "操作"},
    {"id": "expected", "label": "预期"},
    {"id": "generic", "label": "通用"},
]


def catalog_payload() -> dict[str, Any]:
    return {
        "version": CATALOG_VERSION,
        "claim_storage": CLAIM_STORAGE,
        "lease_storage": LEASE_STORAGE,
        "key_layers": KEY_LAYERS,
        "precondition_format": _precondition_format(),
        "prep_flow": _prep_flow(),
        "sections": _sections(),
        "entries": _entries(),
        "example_claim": _example_claim(),
        "example_precondition": _example_precondition_text(),
    }


def _precondition_format() -> dict[str, Any]:
    return {
        "lines": "编号行「类别：要求」；登录态须写清设备/账号；账号与数据用「字段名-状态」或直接写状态",
        "machine_copy": "同步写入 case.meta.resource_key 或 case_scene（推荐二者由导入/Job 生成，人工以编号前置为准）",
        "categories": [
            "设备登录态",
            "账号登录态",
            "账号与数据",
            "环境与权限",
        ],
    }


def _prep_flow() -> dict[str, Any]:
    return {
        "steps": [
            {"id": "pick_account", "label": "筛选账号", "runtime": "ensure_case_account_lease / lease_account"},
            {"id": "pick_device", "label": "筛选设备", "runtime": "批次 sns[] + device_app 机态"},
            {"id": "env_cleanup", "label": "环境清理", "runtime": "clear_app_cache 等 prep_items"},
        ],
        "removed": [
            {
                "id": "switch_run_env",
                "label": "切换测试环境",
                "note": "环境由批次 env_profile 确定，前置不再调用 check_run_env",
            }
        ],
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


def _entry(
    *,
    section: str,
    write_category: str,
    write_examples: list[str],
    claim_path: str,
    resource: str,
    config_keys: list[str],
    dsl: str,
    runtime: str,
    key_layer: str = "precondition",
) -> dict[str, Any]:
    return {
        "section": section,
        "key_layer": key_layer,
        "write_category": write_category,
        "write_examples": write_examples,
        "claim_path": claim_path,
        "resource": resource,
        "config_keys": config_keys,
        "dsl": dsl,
        "runtime": runtime,
    }


def _entries() -> list[dict[str, Any]]:
    return [
        _entry(
            section="device_app",
            write_category="设备登录态",
            write_examples=["未登录", "游客", "已登录"],
            claim_path="device_app.required_session / case_scene.required_session",
            resource="device_app_sessions（机态登记簿）· RunContext.session_fact",
            config_keys=["session"],
            dsl="logged_out | guest | logged_in | any",
            runtime="Preflight 对照机态；不满足 → prep / inspect-session。不写进号池 session",
        ),
        _entry(
            section="account",
            write_category="账号登录态",
            write_examples=["未登录", "已登录", "游客"],
            claim_path="account.required_session / account.requirements.session",
            resource="pool_accounts + pool_account_facets.session",
            config_keys=["session"],
            dsl="session eq logged_in | session in logged_out,guest,unknown",
            runtime="仅当写了「账号登录态」才约束号池；勿映射 login_flow",
        ),
        _entry(
            section="account",
            write_category="账号与数据",
            write_examples=["形象-已配置形象", "形象-未配置形象", "个人信息配置-已配置"],
            claim_path="account.requirements.all[]",
            resource="pool_account_facets",
            config_keys=["号池模板字段 key 或中文名", "选项 value 或标签"],
            dsl="「字段名-状态」→ 该 facet eq；不扫其它字段的同名标签",
            runtime="租号时 row_satisfies_requirements",
        ),
        _entry(
            section="account",
            write_category="账号环境",
            write_examples=["测试环境", "预发环境"],
            claim_path="account.env",
            resource="pool_accounts.env",
            config_keys=["env"],
            dsl="requirements.env = test|staging|…",
            runtime="批次 env_profile 匹配同 env 账号，不是前置「切换测试环境」步骤",
            key_layer="generic",
        ),
        _entry(
            section="account",
            write_category="号池模板",
            write_examples=["模板：已注册老用户（写 template_id 或 CaseScene.account_template_id）"],
            claim_path="account.template_id / case_scene.account_template_id",
            resource="project_pool_config + 号池模板目录",
            config_keys=["template_id", "default_facets", "facet_extensions"],
            dsl="augment_requirements_with_template",
            runtime="合并模板默认约束后再选号",
            key_layer="generic",
        ),
        _entry(
            section="device_app",
            write_category="登录态（兼容旧写法）",
            write_examples=["登录态：未登录 → 视为设备登录态，不约束号池 session"],
            claim_path="device_app.required_session",
            resource="device_app_sessions",
            config_keys=["session"],
            dsl="与「设备登录态」相同",
            runtime="旧前置「登录态」只打机态，避免误检账号未登录",
        ),
        _entry(
            section="device_app",
            write_category="绑定账号",
            write_examples=["登录后绑定租约账号（默认，无需写）", "显式：绑定账号须与租约一致"],
            claim_path="device_app.binding",
            resource="device_app_sessions.bound_account_id",
            config_keys=["must_match_lease"],
            dsl="binding: must_match_lease",
            runtime="登录流完成后写 bound_account_id = lease.account_id",
            key_layer="generic",
        ),
        _entry(
            section="device",
            write_category="端",
            write_examples=["Android", "iOS", "Web"],
            claim_path="platform / case_scene.platform",
            resource="m_device.platform",
            config_keys=["android", "ios", "web", "any"],
            dsl="platform eq android",
            runtime="批跑 sns[] 过滤平台；与 Scout 通道一致",
            key_layer="generic",
        ),
        _entry(
            section="device",
            write_category="设备形态",
            write_examples=["真机 App", "Web", "App+Web"],
            claim_path="device_need / case_scene.device_need",
            resource="派单菜单（executor 集合）",
            config_keys=["app", "web", "app_web", "ab_pair"],
            dsl="device_need",
            runtime="platform_gate / 能力菜单",
            key_layer="generic",
        ),
        _entry(
            section="prep",
            write_category="环境与权限",
            write_examples=["清除应用缓存"],
            claim_path="device_app.prep[] / case_scene.prep_items",
            resource="device_app_sessions + 账号 session（清缓存联动）",
            config_keys=["kind: clear_cache"],
            dsl='prep_items: [{ "kind": "clear_cache", "phase": "before_launch" }]',
            runtime="Prep 环境清理：clear_app_cache；成功后机态 session→logged_out、绑定清除",
        ),
        _entry(
            section="prep",
            write_category="环境与权限",
            write_examples=["检查未登录", "检查已登录", "检查 App 版本"],
            claim_path="case_scene.prep_items",
            resource="device_app_sessions",
            config_keys=["check_not_logged_in", "check_logged_in", "check_app_version"],
            dsl="prep kind 见 session_gate.PREP_KINDS",
            runtime="after_launch 检查；失败则继续 prep 或拒跑",
        ),
        _entry(
            section="scene",
            write_category="前置登录策略",
            write_examples=["业务用例前置重登（隐式）", "登录模块用例勿自动登录"],
            claim_path="case_scene.session_prep",
            resource="RunContext（不持久化号池）",
            config_keys=["relogin", "logout", "skip"],
            dsl="relogin | logout | skip",
            runtime="session_ensure / 是否自动登录（跟设备登录态，不跟号池 login_flow）",
        ),
        _entry(
            section="scene",
            write_category="结构化租号",
            write_examples=["（Job 写入，一般不手写）"],
            claim_path="case_scene.lease_requirements",
            resource="pool_accounts",
            config_keys=["all", "prefer", "env"],
            dsl="与 account.requirements 同形",
            runtime="优先于纯文本编译的租号条件",
            key_layer="generic",
        ),
        _entry(
            section="lease",
            write_category="—",
            write_examples=["（跑批时 Nexus 写入，用例不写）"],
            claim_path="RunContext.resource_lease",
            resource="pool_accounts.lease + run_id",
            config_keys=["account_id", "run_id", "expires_at", "facets"],
            dsl="—",
            runtime="case 结束 / 取消 → release_run_lease",
            key_layer="generic",
        ),
        _entry(
            section="lease",
            write_category="—",
            write_examples=["（跑批参数 sns[] + 项目 App 包名）"],
            claim_path="批次 run + ctx.sn",
            resource="m_device + device_app_sessions",
            config_keys=["sn", "package_id", "node_id"],
            dsl="—",
            runtime="EXECUTE 路由；状态转移带 run_id 鉴权",
            key_layer="generic",
        ),
    ]


def _example_precondition_text() -> str:
    return (
        "1. 设备登录态：未登录\n"
        "2. 账号与数据：形象-已配置形象\n"
        "3. 环境与权限：清除应用缓存"
    )


def _example_claim() -> dict[str, Any]:
    return {
        "version": CATALOG_VERSION,
        "platform": "android",
        "device_need": "app",
        "target_app": {"app_id": "<项目内应用 ID>", "package": "<包名，派单解析>"},
        "device_app": {
            "required_session": "logged_out",
            "allow": ["logged_out", "guest", "unknown"],
            "prep": [{"kind": "clear_cache", "phase": "before_launch", "text": "清除应用缓存"}],
            "binding": "must_match_lease",
        },
        "account": {
            "env": "test",
            "required_session": "any",
            "template_id": "",
            "requirements": {
                "all": [
                    {"facet": "field_7065t5", "op": "eq", "value": "yes"},
                    {"facet": "health", "op": "eq", "value": "available"},
                ],
                "prefer": [],
                "env": "test",
            },
        },
        "case_scene": {
            "required_session": "guest",
            "session_prep": "logout",
            "platform": "android",
            "device_need": "app",
            "prep_items": [
                {"kind": "clear_cache", "phase": "before_launch", "text": "清除应用缓存"},
            ],
        },
    }

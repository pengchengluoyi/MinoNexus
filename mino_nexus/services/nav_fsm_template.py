"""NavFSM 空壳模板：校准前先把骨架建起来。

设计稿 docs/NAVIGATION_ATLAS.md §8.5、§10.3。

§8.5 把配置分成两半：

| 可先写（与 hierarchy 无关） | 必须等 walkthrough（禁止残留 `__CALIBRATE__`） |
|---|---|
| `app_id` / `version` / `test_data` 骨架 | `detect.match_any` 里的 `resource_id_regex` |
| `state_id` + `identify` 骨架 | `detect.strength` |
| 边的 `from` / `to`、`guard` 逻辑、`effect_assert` **结构** | `effect_assert.require_any` 实测 id |
| `recover` 边模板 | `meta.hierarchy_calibration.*` 实测字段 |
| `scroll_into_view` 字段存在 | 需滚动的边的 `scroll_into_view` 参数 |

本模块产出的就是左列，右列一律填 `__CALIBRATE__` —— 于是 `validate_nav_fsm` 会拒绝它进 runtime
（`load` / `PUT` 双向门禁），逼着人把实测值补完。**这是特性不是缺陷**：宁可跑批时明确报「未校准」，
也不要让一份猜出来的配置悄悄生效。

状态名是**通用占位**（`page.home` / `page.list` / …），不是任何被测 App 的东西 —— 拿到手第一件事
就是按实际界面改名。
"""
from __future__ import annotations

from typing import Any

from mino_nexus.services.nav_fsm_store import CALIBRATE_MARK, DEFAULT_VERSION

# 通用骨架：主导航根 → 列表 → 详情 → 二次确认弹窗。够覆盖设计稿 §3.3 说的
# 「列表 guard 不足以覆盖『进详情页再操作』」那类流程。
DEFAULT_SCREENS: tuple[tuple[str, str], ...] = (
    ("page.home", "page"),
    ("page.list", "page"),
    ("page.detail", "page"),
    ("dialog.confirm", "dialog"),
)


def _identify_skeleton(state_id: str, kind: str) -> dict[str, Any]:
    """identify 的结构先摆好，landmark 文案等校准时照着实屏填。"""
    signals: list[dict[str, Any]] = [
        {"signal": "text_landmarks", "any": [CALIBRATE_MARK], "none_of": []}
    ]
    if kind == "page" and state_id.startswith("page."):
        signals.append({"signal": "tab_bar", "match": {"selected": CALIBRATE_MARK}})
    return {"required": signals}


def _guard_skeleton() -> dict[str, Any]:
    """一个 widget、两个态（正/反）。真实应用往往不止一个，校准后自行增删。"""
    return {
        "widget.primary": {
            "states": {
                "on": {
                    "guard_id": "primary.on",
                    "detect": {
                        "strength": CALIBRATE_MARK,
                        "match_any": [
                            {"hierarchy_contains": CALIBRATE_MARK},
                            {"resource_id_regex": CALIBRATE_MARK},
                        ],
                        "match_none": [],
                        "on_miss": "steer_only",
                    },
                    "tap_forbidden": [],
                    "tap_recommended": [],
                    "wiki_ref": "",
                },
                "off": {
                    "guard_id": "primary.off",
                    "detect": {
                        "strength": CALIBRATE_MARK,
                        "match_any": [{"hierarchy_contains": CALIBRATE_MARK}],
                        "match_none": [],
                        "on_miss": "steer_only",
                    },
                    "tap_allowed": [],
                },
            }
        }
    }


def _effect_assert_skeleton() -> dict[str, Any]:
    return {
        "within_ms": 8000,
        "require_any": [
            {"text_landmarks": [CALIBRATE_MARK]},
            {"resource_id_regex": CALIBRATE_MARK},
        ],
        "require_none": [],
        "state_delta": {},
    }


def build_template(
    app_id: str,
    *,
    project_id: str = "",
    version: str = DEFAULT_VERSION,
    screens: list[tuple[str, str]] | None = None,
    anchor_field: str = "case.nav_anchor.author_name",
) -> dict[str, Any]:
    """产出一份待校准的骨架。**故意含 `__CALIBRATE__`，不能直接跑批。**"""
    rows = list(screens or DEFAULT_SCREENS)
    ids = [sid for sid, _ in rows]

    states = [
        {
            "id": sid,
            "kind": kind,
            "identify": _identify_skeleton(sid, kind),
            "guards": _guard_skeleton() if kind == "page" and sid != ids[0] else {},
            "wiki_ref": "",
        }
        for sid, kind in rows
    ]

    edges: list[dict[str, Any]] = []
    for i in range(len(ids) - 1):
        src, dst = ids[i], ids[i + 1]
        edge: dict[str, Any] = {
            "id": f"edge.{src.split('.')[-1]}_to_{dst.split('.')[-1]}",
            "kind": "nav",
            "from": src,
            "to": dst,
            "guard": {},
            "execute": {"steps": [CALIBRATE_MARK]},
            "effect_assert": _effect_assert_skeleton(),
            "on_fail": {},
            "scroll_into_view": {},
        }
        # 列表 → 详情这条最可能要滚动到目标行（§10.4），把字段先摆出来
        if i == 1:
            edge["guard"] = {
                "widget.primary": "on",
                "anchor": {"author_name": "{{" + anchor_field + "}}"},
            }
            edge["scroll_into_view"] = {
                "required": CALIBRATE_MARK,
                "anchor_match": {
                    "text_landmarks": ["{{" + anchor_field + "}}"],
                    "parent_resource_id_regex": CALIBRATE_MARK,
                },
                "max_swipes": 8,
                "direction": "up",
            }
        edges.append(edge)

    # 恢复边是全局的，`from` 留空表示任意状态可走（§2.3）
    for cap in ("press_back", "close_dialog", "go_home", "restart_target_app"):
        edges.append(
            {
                "id": f"recover.{cap}",
                "kind": "recover",
                "from": "",
                "to": ids[0],
                "guard": {},
                "execute": {"steps": [f"recover_{cap}"]},
                "effect_assert": {},
                "on_fail": {},
                "scroll_into_view": {},
            }
        )

    return {
        "app_id": str(app_id or ""),
        "project_id": str(project_id or ""),
        "version": version,
        "meta": {
            "hierarchy_calibration": {
                "calibration_id": CALIBRATE_MARK,
                "evidence_rel_path": CALIBRATE_MARK,
                "account_id": CALIBRATE_MARK,
                "project_id": str(project_id or ""),
                "hierarchy_format": "accessibility_json",
                "hierarchy_result_key": "nodes",
                "follow_filled_detectable": CALIBRATE_MARK,
                "guard_recheck_on_anchor_hit": True,
                "anchor_on_first_screen": CALIBRATE_MARK,
            },
            "guard_catalog": [],
        },
        "test_data": {
            "lease_tags": [],
            "anchor_field": anchor_field,
        },
    } | {"states": states, "edges": edges}


def pending_marks(doc: dict[str, Any]) -> list[str]:
    """列出还没补完的字段路径。Studio 拿它渲染「还差哪些」的清单。"""
    out: list[str] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, str):
            if CALIBRATE_MARK in node:
                out.append(path or "<root>")
        elif isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{path}.{k}" if path else str(k))
        elif isinstance(node, (list, tuple)):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")

    walk(doc, "")
    return out

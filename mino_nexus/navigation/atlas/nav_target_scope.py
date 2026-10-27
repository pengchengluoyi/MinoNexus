"""导航目标作用域：按 app + 平台区分 Android 包名 / macOS Bundle / Windows 应用 / Web 网址。

同一 project 下可有多个 app；每个 app 的 NavFSM / 采集 / 合成只认本 app 的 target_scope，
不把其它包名或站点混进图谱。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

_LAUNCHER_PACKAGES = frozenset(
    {
        "com.android.launcher",
        "com.android.launcher3",
        "com.miui.home",
        "com.huawei.android.launcher",
    }
)

# 状态栏 / 输入法 —— 任意屏都会出现，不代表前台归属
_SYSTEM_ONLY_PKGS = frozenset(
    {
        "android",
        "com.android.systemui",
        "com.android.inputmethod.latin",
        "com.baidu.input",
        "com.sohu.inputmethod.sogou",
        "com.google.android.inputmethod.latin",
    }
)

# 系统弹窗（权限 / 安装器 / 选择器）—— 盖在被测应用上，不是「另一个应用」。
# 以前这类包名被算成 foreign，前台判成 no，截图被送图策略扣掉、模型看不到画面直接弃赛
# （cr-a390541e5dd2 的 api-1-214/226/233）。它们应该走 fb.global.system_dialog。
SYSTEM_DIALOG_PKGS = frozenset(
    {
        "com.android.permissioncontroller",
        "com.google.android.permissioncontroller",
        "com.android.packageinstaller",
        "com.google.android.packageinstaller",
        "com.android.documentsui",
        "com.android.providers.media.module",
        "com.google.android.documentsui",
        "com.android.settings",
    }
)


def is_system_dialog_pkg(package: str) -> bool:
    pkg = str(package or "").strip().lower()
    if not pkg:
        return False
    if pkg in SYSTEM_DIALOG_PKGS:
        return True
    return any(pkg.startswith(owner + ".") or pkg == owner for owner in SYSTEM_DIALOG_PKGS)


@dataclass(frozen=True)
class TargetScope:
    platform: str
    target_id: str
    kind: str  # package | bundle | application | url

    def as_dict(self) -> dict[str, str]:
        return {
            "platform": self.platform,
            "target_id": self.target_id,
            "kind": self.kind,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> TargetScope | None:
        if not isinstance(raw, dict):
            return None
        platform = _norm_platform(str(raw.get("platform") or ""))
        target_id = str(raw.get("target_id") or "").strip()
        kind = str(raw.get("kind") or "").strip().lower()
        if not target_id:
            return None
        expected_kind = _kind_for(platform, target_id)
        if kind and kind != expected_kind:
            raise ValueError("target_scope_kind_conflict")
        kind = expected_kind
        return cls(platform=platform, target_id=_normalize_target_id(platform, kind, target_id), kind=kind)


def _norm_platform(platform: str) -> str:
    from mino_nexus.core.platform_contract import target_platform
    return target_platform(platform)


def _looks_like_url(value: str) -> bool:
    val = str(value or "").strip().lower()
    return val.startswith("http://") or val.startswith("https://") or val.startswith("//")


def _kind_for(platform: str, target_id: str) -> str:
    return {"web": "url", "android": "package", "mac": "bundle", "win": "application"}[_norm_platform(platform)]


def web_page_matches_target(target: str, page: str) -> bool:
    """Only an exact parsed origin proves that the page belongs to this site."""
    left, right = normalize_web_origin(target), normalize_web_origin(page)
    return bool(left and right and left == right)


def normalize_web_origin(url: str) -> str:
    raw = str(url or "").strip()
    try:
        parsed = urlparse(raw)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return ""
        port = parsed.port
        host = parsed.hostname.encode("idna").decode("ascii").lower()
        if ":" in host:
            host = f"[{host}]"
        if port is not None and port != {"http": 80, "https": 443}[parsed.scheme]:
            host += f":{port}"
        return f"{parsed.scheme}://{host}"
    except (ValueError, UnicodeError):
        return ""


def _normalize_target_id(platform: str, kind: str, target_id: str) -> str:
    val = str(target_id or "").strip()
    if not val:
        return ""
    if _norm_platform(platform) == "web":
        origin = normalize_web_origin(val)
        if not origin:
            raise ValueError("target_origin_invalid")
        return origin
    return val


def scope_from_values(platform: str, target_id: str) -> TargetScope | None:
    tid = str(target_id or "").strip()
    if not tid:
        return None
    plat = _norm_platform(platform)
    kind = _kind_for(plat, tid)
    return TargetScope(platform=plat, target_id=_normalize_target_id(plat, kind, tid), kind=kind)


def extract_android_packages(nodes: list[dict[str, Any]] | None) -> set[str]:
    out: set[str] = set()
    for node in nodes or []:
        rid = str(node.get("resource_id") or "").strip()
        if ":" in rid:
            out.add(rid.split(":", 1)[0].strip())
        pkg = str(node.get("package") or "").strip()
        if pkg:
            out.add(pkg)
    return out


def infer_foreground(
    nodes: list[dict[str, Any]] | None,
    *,
    scope: TargetScope | None = None,
    target_package: str = "",
    platform: str = "",
) -> dict[str, str]:
    """推断当前前台：包名 / Bundle / Web origin + screen_kind。"""
    if scope:
        plat = scope.platform
        target = scope.target_id
    else:
        target = str(target_package or "").strip()
        plat = _norm_platform(platform)
        if _looks_like_url(target):
            target = normalize_web_origin(target)

    rids: list[str] = []
    for node in nodes or []:
        rid = str(node.get("resource_id") or "").split("/")[-1].lower()
        if rid:
            rids.append(rid)
    rid_set = set(rids)
    launcher_hits = sum(1 for r in rids if r in {"shortcut_menu_layer", "drag_layer", "workspace", "hotseat", "launcher"})
    is_launcher = launcher_hits >= 2 or ("shortcut_menu_layer" in rid_set and "drag_layer" in rid_set)

    if plat == "web":
        page_url = ""
        for node in nodes or []:
            for key in ("url", "page_url", "location"):
                val = str(node.get(key) or "").strip()
                if _looks_like_url(val):
                    page_url = normalize_web_origin(val)
                    break
            if page_url:
                break
        fg = page_url
        if is_launcher:
            kind = "launcher"
        elif target and page_url and not web_page_matches_target(target, page_url):
            kind = "foreign"
        elif target and page_url:
            kind = "app"
        else:
            kind = "unknown"
        return {
            "platform": "web",
            "target_id": target,
            "foreground_id": fg,
            "target_package": target,
            "foreground_package": fg,
            "screen_kind": kind,
        }

    pkgs = extract_android_packages(nodes)
    if is_launcher or (pkgs and pkgs <= _LAUNCHER_PACKAGES):
        fg = next(iter(pkgs), "com.android.launcher3") if pkgs else "com.android.launcher3"
        return {
            "platform": plat,
            "target_id": target,
            "foreground_id": fg,
            "target_package": target,
            "foreground_package": fg,
            "screen_kind": "launcher",
        }

    overlay_pkg = ""
    if pkgs:
        app_pkgs = {p for p in pkgs if p not in _SYSTEM_ONLY_PKGS}
        dialogs = {p for p in app_pkgs if is_system_dialog_pkg(p)}
        if target and target in pkgs:
            fg = target
            kind = "app"
        elif dialogs and target:
            # 只有系统弹窗、看不到被测应用自己的节点：不能因此断定被测应用在前台
            # （否则「应用根本没起来」会被误判成 app_foreground=yes，
            # 前置启动核验直接跳过 → prepare_launch_unverified）。
            # 只标记这是系统弹窗、前台未知，让截图照常送出并由 system_dialog 块处理。
            fg = ""
            kind = "system_dialog"
            overlay_pkg = sorted(dialogs)[0]
        elif dialogs and not target:
            fg = sorted(dialogs)[0]
            kind = "system_dialog"
            overlay_pkg = fg
        elif not app_pkgs:
            fg = ""
            kind = "unknown"
        elif target:
            fg = sorted(app_pkgs)[0]
            kind = "foreign"
        else:
            fg = sorted(app_pkgs)[0]
            kind = "app"
    elif target:
        fg = ""
        kind = "unknown"
    else:
        fg = ""
        kind = "unknown"

    out = {
        "platform": plat,
        "target_id": target,
        "foreground_id": fg,
        "target_package": target,
        "foreground_package": fg,
        "screen_kind": kind,
    }
    if overlay_pkg:
        out["overlay_package"] = overlay_pkg
    return out


def turn_matches_scope(turn: dict[str, Any], scope: TargetScope | None) -> bool:
    if not scope or not scope.target_id:
        return True
    stored = TargetScope.from_dict(turn.get("target_scope") if isinstance(turn.get("target_scope"), dict) else None)
    if stored and (stored.target_id != scope.target_id or stored.platform != scope.platform):
        return False
    fg = infer_foreground(
        turn.get("nodes") or [],
        scope=scope,
        target_package=str(turn.get("target_package") or scope.target_id),
    )
    kind = str(fg.get("screen_kind") or "")
    if kind in ("launcher", "foreign"):
        return False
    if scope.kind == "url":
        fg_id = normalize_web_origin(str(fg.get("foreground_id") or fg.get("foreground_package") or ""))
        return bool(fg_id) and fg_id == scope.target_id
    fg_pkg = str(fg.get("foreground_id") or fg.get("foreground_package") or "").strip()
    return bool(fg_pkg) and fg_pkg == scope.target_id


def resolve_app_target_scope(app_id: str, *, platform: str = "", target_package: str = "") -> TargetScope | None:
    """Use published scope or an explicit app target; read/contract errors propagate."""
    aid = str(app_id or "").strip()
    if not aid:
        return None
    from mino_nexus.navigation.atlas import nav_fsm_store as store
    doc = store.read_raw(aid)
    if doc:
        meta = doc.get("meta") if isinstance(doc.get("meta"), dict) else {}
        scoped = TargetScope.from_dict(meta.get("target_scope"))
        if scoped:
            if platform and scoped.platform != _norm_platform(platform):
                raise ValueError("target_scope_platform_conflict")
            if target_package and scope_from_values(scoped.platform, target_package).target_id != scoped.target_id:
                raise ValueError("target_scope_identity_conflict")
            return scoped
    # 没有已发布范围、调用方也没给平台时，不编造范围。采集样本自己带 target_scope。
    if not str(platform or "").strip():
        return None
    plat = _norm_platform(platform)
    tid = str(target_package or "").strip()
    if not tid:
        from mino_nexus.resources import project_store as ps
        from mino_nexus.resources.app_automation import package_for_app
        app = ps.find_app(aid)
        if app:
            tid = str(package_for_app(app, platform=plat) or "").strip()
    return scope_from_values(plat, tid)



def attach_scope_to_turn(turn: dict[str, Any], scope: TargetScope | None) -> dict[str, Any]:
    if not scope:
        return turn
    row = dict(turn)
    row["platform"] = scope.platform
    row["target_scope"] = scope.as_dict()
    row["target_package"] = scope.target_id
    fg = infer_foreground(row.get("nodes") or [], scope=scope)
    row["foreground_package"] = fg.get("foreground_package") or ""
    row["foreground_id"] = fg.get("foreground_id") or ""
    row["screen_kind"] = fg.get("screen_kind") or ""
    return row

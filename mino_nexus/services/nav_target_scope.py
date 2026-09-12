"""导航目标作用域：按 app + 平台区分 Android 包名 / iOS Bundle / Web 网址。

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


@dataclass(frozen=True)
class TargetScope:
    platform: str
    target_id: str
    kind: str  # package | bundle | url

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
        target_id = str(raw.get("target_id") or raw.get("target_package") or "").strip()
        kind = str(raw.get("kind") or "").strip().lower()
        if not target_id:
            return None
        if not kind:
            kind = _kind_for(platform, target_id)
        return cls(platform=platform, target_id=_normalize_target_id(platform, kind, target_id), kind=kind)


def _norm_platform(platform: str) -> str:
    plat = str(platform or "android").strip().lower()
    if plat in ("web", "browser", "playwright"):
        return "web"
    if plat in ("ios", "iphone", "ipad"):
        return "ios"
    return "android"


def _looks_like_url(value: str) -> bool:
    val = str(value or "").strip().lower()
    return val.startswith("http://") or val.startswith("https://") or val.startswith("//")


def _kind_for(platform: str, target_id: str) -> str:
    plat = _norm_platform(platform)
    if plat == "web" or _looks_like_url(target_id):
        return "url"
    if plat == "ios" and target_id and "." in target_id and not target_id.startswith("com."):
        return "bundle"
    return "package"


def normalize_web_origin(url: str) -> str:
    raw = str(url or "").strip()
    if not raw:
        return ""
    if not _looks_like_url(raw):
        raw = f"https://{raw}"
    parsed = urlparse(raw)
    if not parsed.netloc:
        return raw.lower().rstrip("/")
    scheme = parsed.scheme or "https"
    host = parsed.netloc.lower()
    return f"{scheme}://{host}".rstrip("/")


def _normalize_target_id(platform: str, kind: str, target_id: str) -> str:
    val = str(target_id or "").strip()
    if not val:
        return ""
    if kind == "url" or _norm_platform(platform) == "web" or _looks_like_url(val):
        return normalize_web_origin(val)
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
        plat = _norm_platform(platform) if platform else ("web" if _looks_like_url(target) else "android")
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
            for key in ("url", "page_url", "location", "href"):
                val = str(node.get(key) or "").strip()
                if _looks_like_url(val):
                    page_url = normalize_web_origin(val)
                    break
            if page_url:
                break
        fg = page_url or target
        if is_launcher:
            kind = "launcher"
        elif target and fg and fg != target:
            kind = "foreign"
        elif target:
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

    if pkgs:
        if target and target in pkgs:
            fg = target
            kind = "app"
        elif target:
            fg = sorted(pkgs)[0]
            kind = "foreign" if target not in pkgs else "app"
        else:
            fg = sorted(pkgs)[0]
            kind = "app"
    elif target:
        fg = target
        kind = "app"
    else:
        fg = ""
        kind = "unknown"

    return {
        "platform": plat,
        "target_id": target,
        "foreground_id": fg,
        "target_package": target,
        "foreground_package": fg,
        "screen_kind": kind,
    }


def turn_matches_scope(turn: dict[str, Any], scope: TargetScope | None) -> bool:
    if not scope or not scope.target_id:
        return True
    stored = TargetScope.from_dict(turn.get("target_scope") if isinstance(turn.get("target_scope"), dict) else None)
    if stored and stored.target_id == scope.target_id and stored.platform == scope.platform:
        return True
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
        return not fg_id or fg_id == scope.target_id
    fg_pkg = str(fg.get("foreground_id") or fg.get("foreground_package") or "").strip()
    return not fg_pkg or fg_pkg == scope.target_id


def resolve_app_target_scope(
    app_id: str,
    *,
    platform: str = "",
    target_package: str = "",
) -> TargetScope | None:
    """从 nav_fsm.meta / apps.env 解析本 app 的导航作用域。"""
    aid = str(app_id or "").strip()
    if not aid:
        return None
    try:
        from mino_nexus.services import nav_fsm_store as store

        doc = store.read_raw(aid)
        if doc:
            meta = doc.get("meta") if isinstance(doc.get("meta"), dict) else {}
            scoped = TargetScope.from_dict(meta.get("target_scope"))
            if scoped:
                return scoped
    except Exception:  # noqa: BLE001
        pass

    tid = str(target_package or "").strip()
    plat = _norm_platform(platform)
    if not tid:
        try:
            from mino_nexus.services import project_store as ps
            from mino_nexus.services.app_automation import package_for_app

            app = ps.find_app(aid)
            if app:
                tid = str(package_for_app(app, platform=plat) or "").strip()
        except Exception:  # noqa: BLE001
            tid = ""
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

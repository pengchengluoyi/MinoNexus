"""从 Scout 上报的 manifest 构造 RunContext。

port(nexus): `RunContext` 这个 dataclass 原样搬自 MiniOrangeServer
`server/services/runtime/run_context.py`（40 个字段一字不改 —— prompt 和落库都按它的
形状读）。**但工厂函数是重写的。**

为什么必须重写：上游 `build_run_context()` 干的是**探测** ——
`connectivity_probe.probe_adb/remote/vlm/hitl`、`probe_playwright()`、
`driver.agent.Crawl.device_bootstrap.resolve_mobile_serial`、查 `MDevice` 表。

拆分后：
  - 通道连通性       ← **Scout 的 REGISTER / HEARTBEAT**（CLAUDE.md §6：不要自己探）
  - adb serial      ← Scout 的 manifest / EXECUTE.device_hint
  - 设备元信息       ← Nexus 的设备表（models/ 未搬，暂取 Scout 上报的 model/platform）
  - vlm / hitl 通道  ← 本仓自己就知道（有没有配 provider、UI 在不在）

所以工厂改成 `from_node(session, sn, ...)`：把 NodeSession 翻译成 RunContext，
零探测、零设备访问。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from mino_nexus.core.log import SLog

TAG = "RunContext"


@dataclass
class RunContext:
    """一次 run 的上下文。字段与上游逐字一致。"""

    sn: str
    platform: str = "android"
    run_id: str = ""

    # 任务归属：落到 trace，供按应用/任务查询用例历史
    app_id: str = ""
    batch_id: str = ""

    # 设备元信息
    device_type: str = ""
    model: str = ""
    os_version: str = ""
    resolution: str = ""
    role: str = ""

    # 通道状态（state + meta）。**来自 Scout 上报，不是本仓探的。**
    remote: dict[str, Any] = field(default_factory=dict)
    adb: dict[str, Any] = field(default_factory=dict)
    vlm: dict[str, Any] = field(default_factory=dict)
    hitl: dict[str, Any] = field(default_factory=dict)
    ios: dict[str, Any] = field(default_factory=dict)
    playwright: dict[str, Any] = field(default_factory=dict)

    # AI provider hints
    provider_id: str = ""
    model_name: str = ""

    # 被测应用包名
    target_package: str = ""

    # 应用基础逻辑快照（下发时从库拷入，本趟任务内不变）
    playbook: dict[str, Any] = field(default_factory=dict)
    # 本条用例开跑前租到的测试资源
    accounts_brief: str = ""
    picked_account: dict[str, Any] = field(default_factory=dict)
    resource_lease: dict[str, Any] = field(default_factory=dict)
    resource_env: dict[str, Any] = field(default_factory=dict)
    # 开跑前设备预置 / 登录态事实
    keep_permission_prompt: bool = False
    provision_report: dict[str, Any] = field(default_factory=dict)
    session_fact: dict[str, Any] = field(default_factory=dict)
    task_session: dict[str, Any] = field(default_factory=dict)
    task_memory: list = field(default_factory=list)
    # 清缓存 / 杀进程之后登录态不再可信，必须重新看图
    session_dirty: bool = False
    case_scene: dict[str, Any] = field(default_factory=dict)
    app_version: str = ""
    env_profile: str = ""
    env_label: str = ""
    env_fact: dict[str, Any] = field(default_factory=dict)

    # ---- 拆分新增：这条 run 派给了哪个节点 ----
    node_id: str = ""

    def remember_app_version(self, version: str) -> None:
        v = str(version or "").strip()
        if v:
            self.app_version = v

    @property
    def device_signature(self) -> str:
        bits = [self.model or self.device_type or "?", self.os_version, self.resolution]
        return " / ".join(b for b in bits if b)

    @property
    def connectivity_flags(self) -> dict[str, bool]:
        """喂给 catalog.registry.filter_capabilities。

        取值口径与上游一致：只有 `connected` / `available` 算通。
        """
        def _ok(d: dict[str, Any]) -> bool:
            return str((d or {}).get("state") or "") in ("connected", "available", "Authenticated")

        return {
            "adb": _ok(self.adb),
            "remote": _ok(self.remote),
            "ios_wda": _ok(self.ios),
            "playwright": _ok(self.playwright),
            "vlm": _ok(self.vlm),
            "hitl": _ok(self.hitl),
            "ai_persona": _ok(self.vlm),   # 拟人化依赖 VLM
            "internal": True,               # wait/noop 永远可用
        }

    @property
    def has_control_channel(self) -> bool:
        flags = self.connectivity_flags
        return any(flags.get(k) for k in ("adb", "remote", "ios_wda", "playwright"))

    def to_prompt_brief(self, *, app_cache_cleared: bool = False) -> dict[str, Any]:
        """注入到 PLAN_OVERVIEW / decide prompt 的 connectivity_brief 区段。"""
        flags = self.connectivity_flags
        return {
            "sn": self.sn,
            "platform": self.platform,
            "device": self.device_signature,
            "node_id": self.node_id,
            "channels": {k: v for k, v in flags.items() if k not in ("internal",)},
            "target_package": self.target_package,
            "app_version": self.app_version,
            "env": self.env_label or self.env_profile,
            "app_cache_cleared": app_cache_cleared,
            "session_dirty": self.session_dirty,
        }

    def to_dict(self) -> dict[str, Any]:
        from dataclasses import asdict

        return asdict(self)


# ---------------- 工厂：从 Scout 的 manifest 构造 ----------------


def _channel(state: str, **meta: Any) -> dict[str, Any]:
    return {"state": state, **meta}


def from_node(
    session: Any,
    sn: str,
    *,
    run_id: str = "",
    app_id: str = "",
    batch_id: str = "",
    provider_id: str = "",
    model_name: str = "",
    target_package: str = "",
) -> RunContext:
    """NodeSession + sn → RunContext。**零探测、零设备访问。**

    `session` 是 `node_registry.NodeSession`（用 Any 避免循环 import）。
    """
    dev = (session.devices or {}).get(sn)
    channels = dict(getattr(dev, "channels", None) or {})

    platform = str(getattr(dev, "platform", "") or "").strip()
    if is_web_slot(sn, platform):
        platform = "web"
    elif not platform:
        platform = "android"
    ctx = RunContext(
        sn=sn,
        platform=platform,
        run_id=run_id,
        app_id=app_id,
        batch_id=batch_id or run_id,
        provider_id=provider_id,
        model_name=model_name,
        target_package=(target_package or "").strip(),
        node_id=session.node_id,
        model=str(getattr(dev, "model", "") or ""),
        device_type=platform,
    )

    def _state_for(channel: str, executor: str) -> dict[str, Any]:
        """只看这台 sn 自己报的通道。节点上装了 adb 插件 ≠ 这台设备能走 adb。"""
        raw = channels.get(channel)
        if isinstance(raw, dict):
            state = str(raw.get("state") or raw.get("status") or "").strip()
            if state:
                return _channel(state, source="scout_manifest")
        else:
            state = str(raw or "").strip()
            if state:
                return _channel(state, source="scout_manifest")
        return _channel("not_applicable", reason=f"该设备未上报 {channel} 通道")

    ctx.adb = _state_for("adb", "adb")
    ctx.remote = _state_for("remote", "remote")
    ctx.ios = _state_for("ios", "ios_wda")
    ctx.playwright = _state_for("playwright", "playwright")

    if platform == "web":
        ctx.remote = _channel("not_applicable", reason="web slot")
        ctx.adb = _channel("not_applicable", reason="web slot")
        ctx.ios = _channel("not_applicable", reason="web slot")
        if not ctx.model:
            ctx.model = "本机浏览器"

    # vlm / hitl 是 Nexus 自己的能力，本仓直接知道，不用问 Scout
    ctx.vlm = _probe_vlm(provider_id, model_name)
    ctx.hitl = _probe_hitl()

    SLog.i(
        TAG,
        f"RunContext from node={session.node_id} sn={sn} platform={platform} "
        f"channels={ {k: v.get('state') for k, v in (('adb', ctx.adb), ('remote', ctx.remote), ('ios', ctx.ios), ('playwright', ctx.playwright)) } } "
        f"vlm={ctx.vlm.get('state')} hitl={ctx.hitl.get('state')}",
    )
    return ctx


def build_run_context(sn: str, **kwargs: Any) -> RunContext:
    """兼容上游签名：按 sn 找节点再构造。

    找不到节点时返回一个**通道全空**的 RunContext —— `has_control_channel` 为 False，
    上层应据此 decline，而不是拿着空菜单去问 LLM。
    """
    from mino_nexus.services.node_registry import get_registry

    node, why = get_registry().resolve(sn)
    if node is None:
        SLog.w(TAG, f"构造 RunContext 失败：{why}")
        ctx = RunContext(sn=sn, platform=str(kwargs.get("platform") or "android"),
                         run_id=str(kwargs.get("run_id") or ""))
        for attr in ("adb", "remote", "ios", "playwright"):
            setattr(ctx, attr, _channel("disconnected", reason=why))
        ctx.vlm = _probe_vlm(str(kwargs.get("provider_id") or ""), str(kwargs.get("model_name") or ""))
        ctx.hitl = _probe_hitl()
        return ctx
    kwargs.pop("platform", None)
    return from_node(node, sn, **{k: v for k, v in kwargs.items() if k in {
        "run_id", "app_id", "batch_id", "provider_id", "model_name", "target_package"}})


def _probe_vlm(provider_id: str = "", model_name: str = "") -> dict[str, Any]:
    """有没有配可用的 LLM provider。这是本仓的设置，不是设备状态。"""
    try:
        from mino_nexus.services import settings

        provider = settings.get_ai_provider_credentials(provider_id or None)
        if provider.get("configured") and provider.get("api_key"):
            if provider.get("enabled") is False:
                return _channel("disabled", reason=f"provider disabled: {provider.get('id')}")
            return _channel("available", provider_id=provider.get("id"),
                            model=model_name or provider.get("model") or "")
        return _channel("unavailable", reason="未配置 AI provider 的 api_key")
    except Exception as exc:  # pragma: no cover
        return _channel("unavailable", reason=f"读取 provider 失败: {exc}")


def _probe_hitl() -> dict[str, Any]:
    """有没有人能被问到。

    上游 `connectivity_probe.probe_hitl()` 看的是有没有 UI observer 在线。
    `websocket/observers.py` 尚未搬迁 —— 暂报 unavailable，**不假装可用**：
    假装可用会让 LLM 选 human_* 能力，然后卡在没人应答上。
    """
    return _channel("unavailable", reason="UI observers 尚未搬迁（websocket/observers.py）")


# ---------------- 平台判断（上游同名函数，去掉 playwright_hub 依赖） ----------------

LEGACY_WEB_SLOT_SN = "web-local"
LEGACY_WEB_SLOT_SNS = frozenset({"web-local", "web_local"})


def _looks_ios_token(value: str) -> bool:
    t = str(value or "").lower()
    return "ios" in t or "iphone" in t or "ipad" in t


def is_legacy_web_sn(sn: str = "") -> bool:
    """Old global Playwright slot names. Purge-only; not a valid current sn."""
    return str(sn or "").strip().lower() in LEGACY_WEB_SLOT_SNS


def is_web_slot(sn: str = "", platform: str = "") -> bool:
    """上游在 `runtime/playwright_hub` 里，那个模块归 Scout。这里只做纯判断。

    Current sn is `web` + alphanumeric scout_id. `web-local` / `web_local` are
    leftovers, still recognized so an upgrading node can retire them.
    """
    s = str(sn or "").strip().lower()
    plat = str(platform or "").strip().lower()
    if plat in ("web", "browser", "playwright"):
        return True
    if is_legacy_web_sn(s) or s == "web" or s.startswith("web-"):
        return True
    return s.startswith("web") and len(s) > 3 and s[3:].isalnum()


def is_per_node_web_sn(sn: str = "") -> bool:
    """`web` + [a-z0-9]+ — the post-upgrade Playwright slot, not `web-local`."""
    s = str(sn or "").strip().lower()
    return s.startswith("web") and len(s) > 3 and s[3:].isalnum()


def device_platform_kind(device_type: str = "", channels: Any = None, sn: str = "") -> str:
    """根据设备元信息判断 android / ios / web。"""
    if is_web_slot(sn, device_type):
        return "web"
    dt = str(device_type or "").lower()
    if dt in ("web", "browser", "playwright"):
        return "web"
    if _looks_ios_token(dt) or _looks_ios_token(sn):
        return "ios"
    if isinstance(channels, dict) and str((channels.get("ios") or {}).get("state") or "") == "connected":
        return "ios"
    return "android"


def stamp_app_version(ctx: Any, version: str) -> None:
    """上游同名函数。Scout 侧的 get_app_version 现在把版本放 raw_response 回传，
    由调用方拿到 EventResult 后调这里落到 ctx。
    """
    try:
        ctx.remember_app_version(version)
    except Exception:
        pass

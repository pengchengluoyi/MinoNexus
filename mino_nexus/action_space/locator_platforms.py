"""Locator-chain strategies. One interface; android / web / mac / win each implement it."""
from __future__ import annotations

from typing import Any, Protocol

from mino_nexus.action_space.locator_chain import LocatorChain, LocatorStep, POINT_CAPS, _add, _norm
from mino_nexus.core.platform_contract import target_platform
from mino_nexus.navigation.geometry.hierarchy_slots import rid_short

_LABEL_KEYS = ("text", "content_desc", "aria_label", "name", "label", "title", "description")
_CSS_FORBIDDEN = ("xpath=", "text=", ">>", "nth", ":first", ":last", "javascript:", ":has")
_MAC_ID_KEYS = ("identifier", "accessibility_id", "resource_id", "id")
_WIN_ID_KEYS = ("automation_id", "resource_id", "id", "accessibility_id")
_MAC_TITLE_KEYS = ("title", "name", "text", "content_desc", "label", "description")
_WIN_NAME_KEYS = ("name", "title", "text", "content_desc", "label")


class LocatorPlatform(Protocol):
    name: str
    structural: bool
    key_order: tuple[str, ...]
    spec_cell: str

    def ingest(self, chain: LocatorChain, *, params: dict[str, Any], compiled: dict[str, Any],
               fingerprints: dict[str, Any] | None, match_phrases: list[str],
               control: dict[str, Any] | None) -> LocatorChain: ...

    def match_node(self, node: dict[str, Any], step: LocatorStep, snapshot_id: str) -> bool: ...

    def usable(self, nodes: list[dict[str, Any]] | None, capability_id: str) -> list[dict[str, Any]]: ...

    def assemble_hit(self, params: dict[str, Any], node: dict[str, Any],
                     result: Any, nodes: list[dict[str, Any]]) -> dict[str, Any]: ...

    def spec_from_hit(self, node: dict[str, Any], used_key: str, used_value: str) -> dict[str, Any] | None: ...

    def parse_value(self, value: str, *, source: str) -> list[LocatorStep]: ...


def _css_like(value: str) -> bool:
    return any(ch in value for ch in "#.[]>()")


def _usable(nodes: list[dict[str, Any]] | None, capability_id: str) -> list[dict[str, Any]]:
    cap = str(capability_id or "").strip()
    out: list[dict[str, Any]] = []
    for node in nodes or []:
        if not isinstance(node, dict):
            continue
        if node.get("cross_origin") or node.get("enabled") is False:
            continue
        if node.get("visible") is False:
            continue
        if cap == "input_text":
            cls = str(node.get("class") or "").lower()
            role = str(node.get("role") or "").lower()
            compact = role.replace("ax", "").replace("_", "").replace("-", "")
            if not (node.get("editable") or "edittext" in cls or role in {"textbox", "searchbox", "input"}
                    or "textfield" in compact or compact == "edit"):
                continue
        elif cap in POINT_CAPS and node.get("clickable") is False:
            continue
        out.append(node)
    return out


def _node_ref_match(node: dict[str, Any], value: str, snapshot_id: str) -> bool:
    return bool(snapshot_id) and node.get("node_ref") == value and str(node.get("snapshot_id") or "") == snapshot_id


def _compact_role(value: str) -> str:
    return str(value or "").strip().lower().replace("ax", "").replace("_", "").replace("-", "")


def _role_ok(node: dict[str, Any], role: str) -> bool:
    want = str(role or "").strip().lower()
    if not want:
        return True
    cls = str(node.get("class") or "").lower()
    node_role = str(node.get("role") or "").lower()
    want_c = _compact_role(want)
    role_c = _compact_role(node_role)
    if want == "checkbox" or want_c == "checkbox":
        return bool(node.get("checkable") or "checkbox" in cls or "checkbox" in node_role)
    if want in {"input", "edittext", "textbox"} or want_c in {"textfield", "edit"}:
        return bool(node.get("editable") or "edittext" in cls or node_role in {"textbox", "searchbox", "input"}
                    or "textfield" in role_c or role_c == "edit")
    return node_role == want or want in cls or role_c == want_c or want_c in _compact_role(cls)


def _first_field(blob: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = str(blob.get(key) or "").strip()
        if value:
            return value
    return ""


def _selector_spec(cell: str, kind: str, value: str, *, exact: bool = False, name: str = "") -> dict[str, Any]:
    sel: dict[str, Any] = {"kind": kind, "value": value}
    if exact:
        sel["exact"] = True
    if name:
        sel["name"] = name
    return {cell: {"cardinality": "one", "selector": sel}}


def _assemble_tree_hit(params, node, result, nodes, *, id_key: str, id_aliases: tuple[str, ...],
                       label_aliases: tuple[str, ...]) -> dict[str, Any]:
    from mino_nexus.navigation.geometry.ui_consent import tap_params_for_control

    out = dict(params or {})
    out.update(tap_params_for_control(node, nodes))
    target = dict(out.get("target") or {}) if isinstance(out.get("target"), dict) else {}
    ident = _first_field(node, id_aliases)
    label = _first_field(node, label_aliases) or str(getattr(result, "used_value", "") or "").strip()
    if ident:
        target[id_key] = ident
    if label:
        target["text"] = label
        out.setdefault("selector_text", label)
    if target:
        out["target"] = target
    if node.get("node_ref"):
        out["node_ref"] = node["node_ref"]
        out["snapshot_id"] = node.get("snapshot_id") or ""
    out["locator_chain"] = True
    return out


class AndroidLocator:
    name = "android"
    structural = True
    key_order = ("node_ref", "resource_id", "content_desc", "text", "role_label")
    spec_cell = "uiautomator"

    def ingest(self, chain: LocatorChain, *, params, compiled, fingerprints, match_phrases, control):
        steps: list[LocatorStep] = []
        seen: set[tuple[str, str]] = set()
        _ingest_compiled(steps, seen, compiled, self.key_order)
        _ingest_node_ref(steps, seen, params)
        for blob, source in ((fingerprints, "cache"), (params.get("target") if isinstance(params.get("target"), dict) else {}, "target"),
                             (params.get("element") if isinstance(params.get("element"), dict) else {}, "element"),
                             (params, "params")):
            if not isinstance(blob, dict):
                continue
            rid = str(blob.get("resource_id") or blob.get("id") or "").strip()
            if rid:
                _add(steps, seen, key="resource_id", value=rid, source=source, allowed=self.key_order)
            desc = str(blob.get("content_desc") or "").strip()
            if desc:
                _add(steps, seen, key="content_desc", value=desc, source=source, allowed=self.key_order)
        cap = chain.capability_id
        if cap != "input_text":
            for blob, source in ((params.get("target") if isinstance(params.get("target"), dict) else {}, "target"),
                                 (params, "params"), (fingerprints or {}, "cache")):
                if not isinstance(blob, dict):
                    continue
                for key in ("text", "selector_text", "name", "label"):
                    _add(steps, seen, key="text", value=str(blob.get(key) or ""), source=source, allowed=self.key_order)
        else:
            target = params.get("target") if isinstance(params.get("target"), dict) else {}
            _add(steps, seen, key="text",
                 value=str(params.get("selector_text") or target.get("text") or ""),
                 source="params", allowed=self.key_order)
            if isinstance(fingerprints, dict):
                _add(steps, seen, key="text", value=str(fingerprints.get("text") or ""), source="cache",
                     allowed=self.key_order)
        for phrase in match_phrases:
            _add(steps, seen, key="text", value=phrase, source="match", allowed=self.key_order)
        _ingest_role(steps, seen, params, self.key_order)
        _ingest_control(self, steps, seen, control)
        chain.steps = steps
        return chain

    def match_node(self, node, step, snapshot_id):
        key, value = step.key, str(step.value or "").strip()
        if not value:
            return False
        if key == "node_ref":
            return _node_ref_match(node, value, snapshot_id)
        if key == "resource_id":
            return rid_short(str(node.get("resource_id") or node.get("id") or "")) == rid_short(value)
        if key == "content_desc":
            return _norm(str(node.get("content_desc") or "")) == _norm(value)
        if key == "text":
            labels = {_norm(str(node.get(k) or "")) for k in _LABEL_KEYS}
            return _norm(value) in labels
        if key == "role_label":
            if not _role_ok(node, step.role):
                return False
            if (not value) or _norm(value) == _norm(step.role):
                return True
            blob = " ".join(str(node.get(k) or "") for k in _LABEL_KEYS)
            return _norm(value) in _norm(blob)
        return False

    def usable(self, nodes, capability_id):
        return _usable(nodes, capability_id)

    def assemble_hit(self, params, node, result, nodes):
        from mino_nexus.navigation.geometry.ui_consent import tap_params_for_control

        out = dict(params or {})
        out.update(tap_params_for_control(node, nodes))
        target = dict(out.get("target") or {}) if isinstance(out.get("target"), dict) else {}
        rid = str(node.get("resource_id") or "").strip()
        label = str(node.get("text") or node.get("content_desc") or getattr(result, "used_value", "") or "").strip()
        if rid:
            target["resource_id"] = rid
        if label:
            target["text"] = label
            target.setdefault("content_desc", str(node.get("content_desc") or label))
            out.setdefault("selector_text", label)
        if target:
            out["target"] = target
        if node.get("node_ref"):
            out["node_ref"] = node["node_ref"]
            out["snapshot_id"] = node.get("snapshot_id") or ""
        out["locator_chain"] = True
        return out

    def spec_from_hit(self, node, used_key, used_value):
        kind = used_key if used_key in {"resource_id", "content_desc", "text"} else ""
        if used_key == "role_label":
            kind = "role"
        if not kind or not used_value:
            return None
        sel: dict[str, Any] = {"kind": kind, "value": used_value}
        if kind in {"content_desc", "text"}:
            sel["exact"] = True
        if kind == "role":
            sel["name"] = str(node.get("text") or node.get("content_desc") or used_value)
        cfg: dict[str, Any] = {"cardinality": "one", "selector": sel}
        return {self.spec_cell: cfg}

    def parse_value(self, value, *, source):
        text = str(value or "").strip()
        if not text or _css_like(text):
            return []
        if _looks_like_native_id(text):
            return [LocatorStep(key="resource_id", value=text, source=source)]
        return [LocatorStep(key="text", value=text, source=source)]


class WebLocator:
    name = "web"
    structural = True
    key_order = ("node_ref", "test_id", "role_name", "label", "css", "text")
    spec_cell = "playwright"

    def ingest(self, chain: LocatorChain, *, params, compiled, fingerprints, match_phrases, control):
        steps: list[LocatorStep] = []
        seen: set[tuple[str, str]] = set()
        _ingest_compiled(steps, seen, compiled, self.key_order)
        _ingest_node_ref(steps, seen, params)
        for blob, source in ((fingerprints, "cache"), (params.get("target") if isinstance(params.get("target"), dict) else {}, "target"),
                             (params, "params")):
            if not isinstance(blob, dict):
                continue
            for raw_key, step_key in (("test_id", "test_id"), ("data-testid", "test_id"),
                                      ("resource_id", "test_id"), ("id", "test_id"),
                                      ("name", "label"), ("label", "label"), ("aria_label", "label"),
                                      ("css", "css"), ("selector", "css")):
                _add(steps, seen, key=step_key, value=str(blob.get(raw_key) or ""), source=source,
                     allowed=self.key_order)
            role = str(blob.get("role") or "").strip()
            name = str(blob.get("name") or blob.get("text") or "").strip()
            if role:
                _add(steps, seen, key="role_name", value=name or role, source=source, role=role,
                     allowed=self.key_order)
        cap = chain.capability_id
        if cap != "input_text":
            for phrase in match_phrases:
                _add(steps, seen, key="label", value=phrase, source="match", allowed=self.key_order)
                _add(steps, seen, key="text", value=phrase, source="match", allowed=self.key_order)
        _ingest_role(steps, seen, params, self.key_order, key="role_name")
        _ingest_control(self, steps, seen, control)
        chain.steps = steps
        return chain

    def match_node(self, node, step, snapshot_id):
        key, value = step.key, str(step.value or "").strip()
        if not value:
            return False
        if key == "node_ref":
            return _node_ref_match(node, value, snapshot_id)
        if key == "test_id":
            got = str(node.get("test_id") or node.get("data-testid") or node.get("resource_id")
                      or node.get("id") or "")
            return _norm(got) == _norm(value) or rid_short(got) == rid_short(value)
        if key == "role_name":
            if not _role_ok(node, step.role):
                return False
            names = {_norm(str(node.get(k) or "")) for k in ("name", "text", "label", "aria_label")}
            return (not step.role) or _norm(value) in names or _norm(value) == _norm(step.role)
        if key == "label":
            labels = {_norm(str(node.get(k) or "")) for k in ("label", "aria_label", "name", "text")}
            return _norm(value) in labels
        if key == "css":
            if any(token in value.lower() for token in _CSS_FORBIDDEN):
                return False
            css = str(node.get("css") or node.get("selector") or "")
            return bool(css) and _norm(css) == _norm(value)
        if key == "text":
            labels = {_norm(str(node.get(k) or "")) for k in _LABEL_KEYS}
            return _norm(value) in labels
        return False

    def usable(self, nodes, capability_id):
        return _usable(nodes, capability_id)

    def assemble_hit(self, params, node, result, nodes):
        from mino_nexus.action_space.web import apply_web_point
        from mino_nexus.navigation.geometry.ui_consent import tap_params_for_control

        out = dict(params or {})
        out.update(tap_params_for_control(node, nodes))
        target = dict(out.get("target") or {}) if isinstance(out.get("target"), dict) else {}
        test_id = str(node.get("test_id") or node.get("data-testid") or "").strip()
        label = str(node.get("name") or node.get("text") or node.get("label") or getattr(result, "used_value", "") or "").strip()
        if test_id:
            target["test_id"] = test_id
        if label:
            target["text"] = label
            out.setdefault("selector_text", label)
        if target:
            out["target"] = target
        if node.get("node_ref"):
            out["node_ref"] = node["node_ref"]
            out["snapshot_id"] = node.get("snapshot_id") or ""
        used = str(getattr(result, "used_key", "") or "")
        if used in {"test_id", "role_name", "label", "css"} and getattr(result, "used_value", ""):
            out.setdefault("locator_spec", self.spec_from_hit(node, used, str(result.used_value)))
        out["locator_chain"] = True
        return apply_web_point(out)

    def spec_from_hit(self, node, used_key, used_value):
        kind = {"test_id": "test_id", "label": "label", "css": "css", "role_name": "role"}.get(used_key)
        if not kind or not used_value:
            return None
        sel: dict[str, Any] = {"kind": kind, "value": used_value}
        if kind in {"role", "label"}:
            sel["exact"] = True
        if kind == "role":
            sel["name"] = str(node.get("name") or node.get("text") or used_value)
            sel["value"] = str(node.get("role") or "button")
        from mino_nexus.contract.web_v5_contract import CONTRACTS

        return {self.spec_cell: {
            "contract": str(CONTRACTS.get("locator_contract") or ""),
            "cardinality": "one",
            "scope": {"kind": "page", "page_id": "main"},
            "selector": sel,
        }}

    def parse_value(self, value, *, source):
        text = str(value or "").strip()
        if not text:
            return []
        low = text.lower()
        if any(token in low for token in _CSS_FORBIDDEN):
            return []
        if text.startswith("#") or text.startswith("[data-testid"):
            key = "css" if text.startswith("#") or text.startswith("[") else "test_id"
            return [LocatorStep(key=key, value=text, source=source)]
        if _css_like(text):
            return [LocatorStep(key="css", value=text, source=source)]
        return [LocatorStep(key="label", value=text, source=source)]


class _TreeLocator:
    """AX / UIA 结构链：稳定 id → 标题/名称 → 角色。空树 miss，不跳过成看图平台。"""
    structural = True
    id_key: str
    id_aliases: tuple[str, ...]
    label_key: str
    label_aliases: tuple[str, ...]

    def ingest(self, chain: LocatorChain, *, params, compiled, fingerprints, match_phrases, control):
        steps: list[LocatorStep] = []
        seen: set[tuple[str, str]] = set()
        _ingest_compiled(steps, seen, compiled, self.key_order)
        _ingest_node_ref(steps, seen, params)
        blobs = (
            (fingerprints, "cache"),
            (params.get("target") if isinstance(params.get("target"), dict) else {}, "target"),
            (params.get("element") if isinstance(params.get("element"), dict) else {}, "element"),
            (params, "params"),
        )
        for blob, source in blobs:
            if not isinstance(blob, dict):
                continue
            ident = _first_field(blob, self.id_aliases)
            if ident:
                _add(steps, seen, key=self.id_key, value=ident, source=source, allowed=self.key_order)
        cap = chain.capability_id
        if cap != "input_text":
            for blob, source in blobs:
                if not isinstance(blob, dict):
                    continue
                title = _first_field(blob, self.label_aliases)
                if title:
                    _add(steps, seen, key=self.label_key, value=title, source=source, allowed=self.key_order)
        else:
            target = params.get("target") if isinstance(params.get("target"), dict) else {}
            _add(steps, seen, key=self.label_key,
                 value=str(params.get("selector_text") or target.get("text") or target.get("name") or ""),
                 source="params", allowed=self.key_order)
        for phrase in match_phrases:
            _add(steps, seen, key=self.label_key, value=phrase, source="match", allowed=self.key_order)
        _ingest_role(steps, seen, params, self.key_order)
        _ingest_control(self, steps, seen, control)
        chain.steps = steps
        return chain

    def match_node(self, node, step, snapshot_id):
        key, value = step.key, str(step.value or "").strip()
        if not value:
            return False
        if key == "node_ref":
            return _node_ref_match(node, value, snapshot_id)
        if key == self.id_key:
            got = _first_field(node, self.id_aliases)
            return bool(got) and (rid_short(got) == rid_short(value) or _norm(got) == _norm(value))
        if key == self.label_key:
            labels = {_norm(str(node.get(k) or "")) for k in self.label_aliases}
            return _norm(value) in labels
        if key == "role_label":
            if not _role_ok(node, step.role):
                return False
            if (not value) or _norm(value) == _norm(step.role):
                return True
            blob = " ".join(str(node.get(k) or "") for k in self.label_aliases)
            return _norm(value) in _norm(blob)
        return False

    def usable(self, nodes, capability_id):
        return _usable(nodes, capability_id)

    def assemble_hit(self, params, node, result, nodes):
        return _assemble_tree_hit(
            params, node, result, nodes,
            id_key=self.id_key, id_aliases=self.id_aliases, label_aliases=self.label_aliases,
        )

    def spec_from_hit(self, node, used_key, used_value):
        kind = used_key if used_key in {self.id_key, self.label_key} else ""
        if used_key in {"text", self.label_key}:
            kind = self.label_key
        if used_key == "role_label":
            kind = "role"
        if not kind or not used_value:
            return None
        name = _first_field(node, self.label_aliases) if kind == "role" else ""
        return _selector_spec(
            self.spec_cell, kind, used_value,
            exact=kind == self.label_key,
            name=name or (used_value if kind == "role" else ""),
        )

    def parse_value(self, value, *, source):
        text = str(value or "").strip()
        if not text or _css_like(text):
            return []
        if _looks_like_native_id(text):
            return [LocatorStep(key=self.id_key, value=text, source=source)]
        return [LocatorStep(key=self.label_key, value=text, source=source)]


class MacLocator(_TreeLocator):
    name = "mac"
    key_order = ("node_ref", "identifier", "title", "role_label")
    spec_cell = "ax"
    id_key = "identifier"
    id_aliases = _MAC_ID_KEYS
    label_key = "title"
    label_aliases = _MAC_TITLE_KEYS


class WinLocator(_TreeLocator):
    name = "win"
    key_order = ("node_ref", "automation_id", "name", "role_label")
    spec_cell = "uia"
    id_key = "automation_id"
    id_aliases = _WIN_ID_KEYS
    label_key = "name"
    label_aliases = _WIN_NAME_KEYS


_ANDROID = AndroidLocator()
_WEB = WebLocator()
_MAC = MacLocator()
_WIN = WinLocator()
_NATIVE = (_ANDROID, _WEB, _MAC, _WIN)


class SharedLocator:
    """Compile-time / unspecified platform: keep structural keys of all four platforms.

    Runtime always has ctx.platform. Shared only fills the chain when the row is
    a cross-platform block (privacy, login) compiled before a device is bound.
    """
    name = ""
    structural = True
    key_order = _ANDROID.key_order + tuple(
        k for plat in (_WEB, _MAC, _WIN) for k in plat.key_order if k not in _ANDROID.key_order
    )
    spec_cell = ""

    def ingest(self, chain, *, params, compiled, fingerprints, match_phrases, control):
        _ANDROID.ingest(chain, params=params, compiled=compiled, fingerprints=fingerprints,
                        match_phrases=match_phrases, control=control)
        seen = {(s.key, _norm(s.value)) for s in chain.steps}
        extra: list[LocatorStep] = []
        for plat in (_WEB, _MAC, _WIN):
            sub = LocatorChain(capability_id=chain.capability_id, snapshot_id=chain.snapshot_id)
            plat.ingest(sub, params=params, compiled=compiled, fingerprints=fingerprints,
                        match_phrases=match_phrases, control=control)
            for step in sub.steps:
                mark = (step.key, _norm(step.value))
                if mark in seen or step.key not in self.key_order:
                    continue
                seen.add(mark)
                extra.append(step)
        chain.steps = list(chain.steps) + extra
        return chain

    def match_node(self, node, step, snapshot_id):
        return any(plat.match_node(node, step, snapshot_id) for plat in _NATIVE)

    def usable(self, nodes, capability_id):
        return _ANDROID.usable(nodes, capability_id)

    def assemble_hit(self, params, node, result, nodes):
        used = str(getattr(result, "used_key", "") or "")
        for plat in _NATIVE:
            if used in plat.key_order:
                return plat.assemble_hit(params, node, result, nodes)
        return _ANDROID.assemble_hit(params, node, result, nodes)

    def spec_from_hit(self, node, used_key, used_value):
        for plat in _NATIVE:
            spec = plat.spec_from_hit(node, used_key, used_value)
            if spec:
                return spec
        return None

    def parse_value(self, value, *, source):
        for plat in _NATIVE:
            steps = plat.parse_value(value, source=source)
            if steps:
                return steps
        return []


_SHARED = SharedLocator()


def for_platform(platform: str | None) -> LocatorPlatform:
    raw = str(platform or "").strip()
    try:
        plat = target_platform(raw) if raw else ""
    except (TypeError, ValueError):
        plat = ""
    if plat == "android":
        return _ANDROID
    if plat == "web":
        return _WEB
    if plat == "mac":
        return _MAC
    if plat == "win":
        return _WIN
    return _SHARED


def _ingest_compiled(steps, seen, compiled, allowed):
    for raw in compiled.get("steps") or []:
        if not isinstance(raw, dict):
            continue
        _add(steps, seen, key=str(raw.get("key") or ""), value=str(raw.get("value") or ""),
             source=str(raw.get("source") or "compiled"), role=str(raw.get("role") or ""), allowed=allowed)


def _ingest_node_ref(steps, seen, params):
    ref = str(params.get("node_ref") or "").strip()
    if ref:
        _add(steps, seen, key="node_ref", value=ref, source="params", allowed=("node_ref",))


def _ingest_role(steps, seen, params, allowed, key="role_label"):
    target = params.get("target") if isinstance(params.get("target"), dict) else {}
    element = params.get("element") if isinstance(params.get("element"), dict) else {}
    rules = params.get("rules") if isinstance(params.get("rules"), dict) else {}
    role = str(element.get("role") or target.get("role") or "").strip().lower()
    label = str(rules.get("label") or element.get("name") or target.get("text") or "").strip()
    if role:
        _add(steps, seen, key=key, value=label or role, source="element", role=role, allowed=allowed)


def _label_key(platform: LocatorPlatform) -> str:
    for key in ("text", "label", "title", "name"):
        if key in platform.key_order:
            return key
    return ""


def _coord_text(value: str) -> bool:
    text = str(value or "").strip()
    if "," not in text and "，" not in text:
        return False
    left, _, right = text.replace("，", ",").partition(",")
    return left.strip().isdigit() and right.strip().isdigit()


def _looks_like_native_id(value: str) -> bool:
    text = str(value or "").strip()
    if not text or any(ch.isspace() for ch in text):
        return False
    if ":id/" in text:
        return True
    if any("\u4e00" <= ch <= "\u9fff" for ch in text):
        return False
    if "/" in text and "." in text.split("/")[0]:
        return True
    if any(mark in text for mark in "._-") and all(ch.isascii() and (ch.isalnum() or ch in "._-:/") for ch in text):
        return True
    return False


def _map_spec_key(platform: LocatorPlatform, kind: str) -> str:
    mapped = {
        "resource_id": "resource_id",
        "content_desc": "content_desc",
        "text": "text",
        "role": "role_name" if platform.name == "web" else "role_label",
        "test_id": "test_id",
        "label": "label",
        "css": "css",
        "identifier": "identifier",
        "automation_id": "automation_id",
        "title": "title",
        "name": "name",
    }.get(kind, "")
    if mapped == "name" and "name" not in platform.key_order and "title" in platform.key_order:
        mapped = "title"
    if mapped == "title" and "title" not in platform.key_order and "name" in platform.key_order:
        mapped = "name"
    if mapped == "text" and "text" not in platform.key_order:
        mapped = "title" if "title" in platform.key_order else ("name" if "name" in platform.key_order else mapped)
    if mapped == "identifier" and "identifier" not in platform.key_order and "automation_id" in platform.key_order:
        mapped = "automation_id"
    if mapped == "automation_id" and "automation_id" not in platform.key_order and "identifier" in platform.key_order:
        mapped = "identifier"
    return mapped


def _ingest_control(platform: LocatorPlatform, steps, seen, control):
    if not isinstance(control, dict):
        return
    from mino_nexus.contract.locator_spec import spec_selector

    sel = spec_selector(control.get("locator_spec"), platform.name)
    kind = str(sel.get("kind") or "").strip()
    value = str(sel.get("value") or "").strip()
    if kind and value:
        mapped = _map_spec_key(platform, kind)
        if mapped:
            _add(steps, seen, key=mapped, value=str(sel.get("name") or value), source="control",
                 role=value if kind == "role" else str(sel.get("name") or ""), allowed=platform.key_order)
    cell = str(platform.spec_cell or "")
    locator = control.get("locator_value") if isinstance(control.get("locator_value"), dict) else {}
    raw = str((locator or {}).get(cell) or "").strip()
    if raw:
        for step in platform.parse_value(raw, source="control"):
            _add(steps, seen, key=step.key, value=step.value, source=step.source, role=step.role,
                 allowed=platform.key_order)
    # Visible copy is already a locator. resource_id / typed spec are higher-priority
    # keys, not a prerequisite — Flutter / 自绘 / 未采集 id 时链上的文案和角色仍可用。
    label = str(control.get("target_desc") or (locator or {}).get("vlm_text") or "").strip()
    if label and not _coord_text(label):
        key = _label_key(platform)
        if key:
            _add(steps, seen, key=key, value=label, source="control", allowed=platform.key_order)
        role = str(control.get("element_type") or "").strip().lower()
        if role in {"page", "element", "text", "image", "dialog", ""}:
            role = ""
        if role == "input":
            role = "textbox"
        if role:
            role_key = "role_name" if platform.name == "web" else "role_label"
            _add(steps, seen, key=role_key, value=label, source="control", role=role,
                 allowed=platform.key_order)

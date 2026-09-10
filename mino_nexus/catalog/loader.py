"""只读 catalog_entries。

kind∈{prep,do,check,generic} → 可调用能力。
kind=recovery → 按 payload 形状同时支持 L0 规则与可派单原子能力（wake_screen 等）。
"""
from __future__ import annotations

import threading
from typing import Any, Optional

from mino_nexus.catalog.exec_classes import CAPABILITY_KINDS, LOCAL_ORCH_IDS, RECOVERY_KIND
from mino_nexus.catalog.models import Capability, LoadError, RecoveryRule
from mino_nexus.catalog.recovery_shape import is_recovery_atomic_payload, is_recovery_rule_payload
from mino_nexus.core.log import SLog

TAG = "PluginLoader"


def _strip_impl(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    row = dict(raw)
    row.pop("cost", None)
    row.pop("requires_caps", None)
    return row


def _load_capability_row(
    store: dict[str, Capability],
    errors: list[LoadError],
    row,
    *,
    kind: str,
    payload: dict[str, Any],
) -> None:
    impls = [_strip_impl(x) for x in (payload.get("implementations") or []) if isinstance(x, dict)]
    if not impls and row.id not in LOCAL_ORCH_IDS:
        return
    try:
        store[row.id] = Capability(
            id=row.id,
            kind=kind,
            display_name=row.display_name or row.id,
            event_kind=str(payload.get("event_kind") or row.id),
            category=row.category or "uncategorized",
            description=row.description or "",
            platforms=list(row.platforms_json or []),
            needs_vlm=bool(payload.get("needs_vlm")),
            implementations=impls,
            visible_to=list(row.visible_to_json or ["case", "system"]),
            params=list(payload.get("params") or []),
            ui=payload.get("ui") or {},
            enabled=bool(row.enabled),
            lifecycle=str(row.lifecycle or "active"),
        )
    except Exception as exc:
        errors.append(LoadError(
            path=f"catalog_entries/{kind}/{row.id}",
            kind=kind,
            message=str(exc)[:240],
        ))


def _load_recovery_rule_row(
    store: dict[str, RecoveryRule],
    errors: list[LoadError],
    row,
    payload: dict[str, Any],
) -> None:
    try:
        store[row.id] = RecoveryRule(
            id=row.id,
            title=row.display_name or row.id,
            enabled=bool(row.enabled),
            provider=row.provider or "",
            owner=row.owner or "",
            lifecycle=row.lifecycle or "active",
            priority=int((payload.get("priority") or 0)),
            when=str(payload.get("when") or ""),
            match=payload.get("match") or {},
            mode=str(payload.get("mode") or "advise"),
            actions=list(payload.get("actions") or []),
            verify=payload.get("verify") or {},
            forbid=payload.get("forbid") or {},
            prompt_snippet=str(payload.get("prompt_snippet") or ""),
            max_attempts=int(payload.get("max_attempts") or 1),
            platforms=list(row.platforms_json or []),
            evidence_notes=list(payload.get("evidence_notes") or []),
        )
    except Exception as exc:
        errors.append(LoadError(
            path=f"catalog_entries/recovery/{row.id}",
            kind=RECOVERY_KIND,
            message=str(exc)[:240],
        ))


class PluginLoader:
    def __init__(self):
        self.root = "catalog_entries"
        self._lock = threading.RLock()
        self._loaded: bool = False
        self._capabilities: dict[str, Capability] = {}
        self._recovery_rules: dict[str, RecoveryRule] = {}
        self._errors: list[LoadError] = []

    @property
    def capabilities(self) -> dict[str, Capability]:
        self._ensure_loaded()
        return self._capabilities

    @property
    def recovery_rules(self) -> dict[str, RecoveryRule]:
        self._ensure_loaded()
        return self._recovery_rules

    @property
    def errors(self) -> list[LoadError]:
        self._ensure_loaded()
        return list(self._errors)

    def _ensure_loaded(self) -> None:
        with self._lock:
            if not self._loaded:
                self._load_all()
                self._loaded = True

    def _load_all(self) -> None:
        from mino_nexus.core.database import ensure_db

        ensure_db()
        if not self._load_from_db():
            SLog.w(TAG, "catalog_entries empty")

    def _load_from_db(self) -> bool:
        from mino_nexus.core.database import SessionLocal
        from mino_nexus.models.catalog import CatalogEntry

        db = SessionLocal()
        try:
            rows = db.query(CatalogEntry).order_by(CatalogEntry.sort_order).all()
            self._capabilities = {}
            self._recovery_rules = {}
            self._errors = []
            if not rows:
                return False
            for row in rows:
                self._hydrate_row(row)
            SLog.i(
                TAG,
                f"loaded catalog db: capabilities={len(self._capabilities)}, "
                f"recovery={len(self._recovery_rules)}",
            )
            return True
        finally:
            db.close()

    def _hydrate_row(self, row) -> None:
        payload = dict(row.payload_json or {})
        kind = str(row.kind or "").strip()
        if kind in CAPABILITY_KINDS:
            _load_capability_row(self._capabilities, self._errors, row, kind=kind, payload=payload)
            return
        if kind == RECOVERY_KIND:
            rule = is_recovery_rule_payload(payload)
            atomic = is_recovery_atomic_payload(row.id, payload)
            if not rule and not atomic:
                self._errors.append(LoadError(
                    path=f"catalog_entries/recovery/{row.id}",
                    kind=RECOVERY_KIND,
                    message="recovery 行既非 L0 规则（match/actions）也非原子能力（implementations/caller）",
                ))
                return
            if rule:
                _load_recovery_rule_row(self._recovery_rules, self._errors, row, payload)
            if atomic:
                _load_capability_row(
                    self._capabilities, self._errors, row, kind=RECOVERY_KIND, payload=payload,
                )
            return


_LOADER: Optional[PluginLoader] = None
_LOADER_LOCK = threading.Lock()


def get_loader() -> PluginLoader:
    global _LOADER
    with _LOADER_LOCK:
        if _LOADER is None:
            _LOADER = PluginLoader()
    return _LOADER


def force_reload() -> PluginLoader:
    global _LOADER
    with _LOADER_LOCK:
        _LOADER = PluginLoader()
    return _LOADER

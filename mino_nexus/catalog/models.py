"""能力目录的内存模型。真源是 catalog_entries.kind。"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class Implementation(BaseModel):
    """一条执行路径。low_level 原样进 EXECUTE，给 Scout 用。"""

    model_config = ConfigDict(extra="allow")

    id: str
    display_name: str = ""
    executor: str
    needs_vlm: bool = False
    locate_prompt: Optional[str] = None
    prompt_template: Optional[str] = None
    low_level: dict[str, Any] = Field(default_factory=dict)
    expands_to_events: bool = False
    description: str = ""


class CapabilityUI(BaseModel):
    shown_in_settings: bool = True
    examples: list[str] = Field(default_factory=list)


class Capability(BaseModel):
    """可调用能力。kind = prep | do | check | generic。"""

    model_config = ConfigDict(extra="allow")

    id: str
    kind: str
    display_name: str
    event_kind: str = ""
    category: str = "uncategorized"
    description: str = ""
    platforms: list[str] = Field(default_factory=list)
    needs_vlm: bool = False
    implementations: list[Implementation] = Field(default_factory=list)
    ui: CapabilityUI = Field(default_factory=CapabilityUI)
    visible_to: list[str] = Field(default_factory=lambda: ["case", "system"])
    params: list[dict[str, Any]] = Field(default_factory=list)
    enabled: bool = True
    lifecycle: str = "active"


class RecoveryMatch(BaseModel):
    model_config = ConfigDict(extra="allow")

    evidence: dict[str, str] = Field(default_factory=dict)
    screen_text_any: list[str] = Field(default_factory=list)
    top_window_pkg_prefix: list[str] = Field(default_factory=list)


class RecoveryAction(BaseModel):
    model_config = ConfigDict(extra="allow")

    capability: str
    params: dict[str, Any] = Field(default_factory=dict)
    target: dict[str, str] = Field(default_factory=dict)
    fallback_xy: list[int] = Field(default_factory=list)


class RecoveryForbid(BaseModel):
    text_any: list[str] = Field(default_factory=list)


class RecoveryRule(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    kind: str = "recovery"
    title: str = ""
    enabled: bool = True
    provider: str = "platform"
    owner: str = ""
    lifecycle: str = "active"
    priority: int = 0
    when: str = ""
    match: RecoveryMatch = Field(default_factory=RecoveryMatch)
    mode: str = "advise"
    actions: list[RecoveryAction] = Field(default_factory=list)
    verify: RecoveryMatch = Field(default_factory=RecoveryMatch)
    forbid: RecoveryForbid = Field(default_factory=RecoveryForbid)
    prompt_snippet: str = ""
    max_attempts: int = 1
    platforms: list[str] = Field(default_factory=list)
    evidence_notes: list[str] = Field(default_factory=list)


class LoadError(BaseModel):
    path: str
    kind: str
    message: str

"""整词对照里程碑上的 match / exclude。文案在逻辑块步骤里，不在本文件。"""
from __future__ import annotations

import re
from typing import Any

_ZW_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]")
_OTP_RE = re.compile(r"^\d{4,8}$")
_LABEL_KEYS = ("text", "content_desc", "aria_label", "name", "label")


def _norm(text: str) -> str:
    s = _ZW_RE.sub("", str(text or ""))
    s = re.sub(r"\s+", " ", s).strip()
    return s.casefold()


def _phrases(row: dict[str, Any], key: str) -> list[str]:
    raw = row.get(key)
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        s = str(item or "").strip()
        if s and s not in out:
            out.append(s)
    return out


def phrase_equals(label: str, phrase: str) -> bool:
    a, b = _norm(label), _norm(phrase)
    return bool(a) and a == b


def _listed(label: str, phrases: list[str]) -> str:
    for phrase in phrases:
        if phrase_equals(label, phrase):
            return phrase
    return ""


def otp_value_present(nodes: list[dict[str, Any]] | None) -> bool:
    for node in nodes or []:
        if not isinstance(node, dict):
            continue
        for key in ("value", "text", "content_desc"):
            raw = re.sub(r"\s+", "", str(node.get(key) or ""))
            if _OTP_RE.fullmatch(raw):
                return True
    return False


def _match_phrases(row: dict[str, Any], nodes: list[dict[str, Any]] | None) -> list[str]:
    phrases = list(_phrases(row, "match"))
    when_rows = row.get("match_when")
    if not isinstance(when_rows, list):
        return phrases
    otp_ok = otp_value_present(nodes)
    for item in when_rows:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        cond = str(item.get("when") or "").strip()
        if not text:
            continue
        if cond == "otp_filled" and not otp_ok:
            continue
        if text not in phrases:
            phrases.append(text)
    return phrases


def classify_label(
    label: str,
    row: dict[str, Any],
    nodes: list[dict[str, Any]] | None = None,
) -> tuple[str, str]:
    """返回 (match|exclude|none, 命中的表面形式)。排除优先于可点。"""
    if not str(label or "").strip():
        return "none", ""
    excluded = _listed(label, _phrases(row, "exclude"))
    if excluded:
        return "exclude", excluded
    matched = _listed(label, _match_phrases(row, nodes))
    if matched:
        return "match", matched
    return "none", ""


def node_labels(node: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for key in _LABEL_KEYS:
        s = str(node.get(key) or "").strip()
        if s and s not in out:
            out.append(s)
    return out


def classify_node(
    node: dict[str, Any],
    row: dict[str, Any],
    nodes: list[dict[str, Any]] | None = None,
) -> tuple[str, str]:
    """节点上任一字符串命中排除则排除；否则任一命中可点则算可点。"""
    verdict = "none"
    hit = ""
    for label in node_labels(node):
        kind, phrase = classify_label(label, row, nodes)
        if kind == "exclude":
            return "exclude", phrase
        if kind == "match" and verdict != "match":
            verdict, hit = "match", phrase
    return verdict, hit


def landed_labels(params: dict[str, Any] | None, summary: str) -> list[str]:
    out: list[str] = []
    p = params or {}
    for key in ("selector_text", "text", "content_desc"):
        s = str(p.get(key) or "").strip()
        if s and s not in out:
            out.append(s)
    target = p.get("target")
    if isinstance(target, dict):
        for key in ("text", "content_desc"):
            s = str(target.get(key) or "").strip()
            if s and s not in out:
                out.append(s)
    m = re.search(r"「([^」]{1,80})」", str(summary or ""))
    if m:
        s = m.group(1).strip()
        if s and s not in out:
            out.append(s)
    return out


def row_has_surface(row: dict[str, Any]) -> bool:
    return bool(_phrases(row, "match") or _phrases(row, "exclude") or row.get("match_when"))

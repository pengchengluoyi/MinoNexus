"""技能读取的薄封装。正文在 prompts/skills。"""
from __future__ import annotations

import re
from typing import Any

from mino_nexus.platform.llm.repo import get_skill as _get_skill
from mino_nexus.platform.llm.repo import list_skills as _list_skills
from mino_nexus.catalog.exec_classes import ALL_KINDS

_SLUG = re.compile(r"[^a-z0-9_-]+")

ALIASES = {
    "agent-decide": "run-case",
    "agent-vision-exec": "run-case",
    "agent_do": "run-case",
    "explore-decide": "explore-app",
}


def enums() -> dict[str, Any]:
    return {
        "engines": ["agent_loop", "qa_job", "chat"],
        "pointers": ["case_columns", "none"],
        "views": ["case-three-column", "job-timeline", "flow-doc"],
        "input_types": ["case", "instruction", "requirement", "none"],
        "categories": ["flow", "device", "channel", "sync"],
        "tool_kinds": list(ALL_KINDS),
        "advance_on": ["milestones", "signal_done", "assert_pass"],
        "phase_jobs": [
            "agent-vision-plan",
            "agent-vision-exec",
            "agent-vision-assert",
            "agent-decide",
        ],
    }


def resolve_id(skill_id: str) -> str:
    raw = str(skill_id or "").strip()
    return ALIASES.get(raw, raw)


def list_skills() -> list[dict[str, Any]]:
    return _list_skills()


def get_skill(skill_id: str) -> dict[str, Any] | None:
    return _get_skill(resolve_id(skill_id))


def skill_prompt(skill_id: str) -> str:
    from mino_nexus.ai.job_store import job_system_text, resolve_job_id

    sid = resolve_id(skill_id)
    return job_system_text(resolve_job_id(sid))


def catalog_payload() -> dict[str, Any]:
    rows = list_skills()
    return {
        "skills": rows,
        "enums": enums(),
        "counts": {"skills": len(rows)},
    }


def _slug(raw: str) -> str:
    return _SLUG.sub("-", str(raw or "").strip().lower()).strip("-")

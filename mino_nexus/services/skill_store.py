"""技能读取的薄封装。正文在 prompts/skills。"""
from __future__ import annotations

import re
from typing import Any

from mino_nexus.ai.prompt_repo import get_skill as _get_skill
from mino_nexus.ai.prompt_repo import list_skills as _list_skills
from mino_nexus.ai.skill_defs import ALIASES, enums

_SLUG = re.compile(r"[^a-z0-9_-]+")


def resolve_id(skill_id: str) -> str:
    raw = str(skill_id or "").strip()
    return ALIASES.get(raw, raw)


def list_skills() -> list[dict[str, Any]]:
    return _list_skills()


def get_skill(skill_id: str) -> dict[str, Any] | None:
    return _get_skill(resolve_id(skill_id))


def skill_prompt(skill_id: str) -> str:
    from mino_nexus.services.job_store import job_system_text, resolve_job_id

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

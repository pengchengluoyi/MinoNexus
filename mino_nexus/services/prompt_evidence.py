"""调用用到的静态模板快照：只作证据，不回灌运行配置，不包含 slot 值。"""
import re


def archive_template(row: dict) -> str:
    sha = str(row.get("prompt_sha") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", sha):
        return ""
    from sqlalchemy.dialects.sqlite import insert
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.prompt_evidence import PromptEvidence
    # 原子 INSERT 保证并发不留下半份证据；JSON 中不存渲染后的用户变量。
    with session_scope() as db:
        db.execute(insert(PromptEvidence).values(sha=sha, job_id=str(row.get("id") or ""),
            version=int(row.get("prompt_version") or 1), template_json=row).on_conflict_do_nothing(index_elements=["sha"]))
    return sha

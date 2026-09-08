"""空库不再灌能力目录。只搬旧知识进 knowledge_entries。"""
from __future__ import annotations

from mino_nexus.core.log import SLog

TAG = "Bootstrap"


def bootstrap() -> None:
    from mino_nexus.core.database import SessionLocal

    db = SessionLocal()
    try:
        from mino_nexus.services.knowledge_store import migrate_legacy

        kn = migrate_legacy(db)
        if kn:
            SLog.i(TAG, f"knowledge migrated {kn} entries")
        from mino_nexus.services.skill_store import seed_skills

        n = seed_skills()
        if n:
            SLog.i(TAG, f"skills seeded {n}")
        from mino_nexus.catalog.recovery_seed import seed_recovery_rules

        rn = seed_recovery_rules()
        if rn:
            SLog.i(TAG, f"recovery rules seeded {rn}")
        from mino_nexus.catalog.recovery_seed import upgrade_recovery_rules

        un = upgrade_recovery_rules()
        if un:
            SLog.i(TAG, f"recovery rules upgraded {un}")
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

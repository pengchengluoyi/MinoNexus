"""Explicit configuration writes to the existing UiControl registry."""
from __future__ import annotations

from datetime import datetime
from sqlalchemy import func

from mino_nexus.platform.identity import require_user_id
from mino_nexus.knowledge.control_registry import _public, _assert_runtime_writable
from mino_nexus.contract.web_v5_contract import validate_locator


def audit(project_id):
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.ui_control import UiControl
    with session_scope() as db:
        return [{"control_id": cid, "count": count} for cid, count in
                db.query(UiControl.control_id, func.count(UiControl.pk)).filter_by(project_id=project_id)
                .group_by(UiControl.control_id).having(func.count(UiControl.pk) > 1).all()]


def list_controls(project_id):
    from mino_nexus.casework.control_page_binding import bind_project_controls
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.ui_control import UiControl
    bind_project_controls(project_id)
    with session_scope() as db:
        return [_public(row) for row in db.query(UiControl).filter_by(project_id=project_id).order_by(UiControl.pk)]


def configure(project_id, control_id, *, expected_revision, locator_spec, sensitive, publish=False, page_state_id=None, platform="web"):
    raise ValueError("logic_write_stopped")
    _assert_runtime_writable()
    actor = require_user_id()
    from mino_nexus.contract.locator_spec import validate_locator_spec

    plat = str(platform or "web").strip() or "web"
    if plat == "web":
        validate_locator(locator_spec)
    else:
        validate_locator_spec(locator_spec, plat)
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.ui_control import UiControl
    from sqlalchemy import text
    with session_scope() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        rows = db.query(UiControl).filter_by(project_id=project_id, control_id=control_id).all()
        if len(rows) != 1:
            raise ValueError("control_identity_conflict" if rows else "control_missing")
        row = rows[0]
        if int(row.revision) != int(expected_revision):
            raise ValueError("control_revision_conflict")
        if row.scope != "page":
            raise ValueError("unsupported_scope")
        if page_state_id is not None:
            locator = dict(row.locator_value or {})
            page = str(page_state_id or "").strip()
            if page:
                locator["nav_state_id"] = page
                locator["nav_state_source"] = "manual"
            else:
                locator.pop("nav_state_id", None)
                locator.pop("nav_state_source", None)
            row.locator_value = locator
        row.locator_spec = locator_spec
        row.sensitive = int(bool(sensitive))
        row.revision += 1
        # A valid saved locator is immediately current; no approval/publication stage.
        row.publish_state = "published"
        row.updated_by, row.updated_at = actor, datetime.now().astimezone().isoformat()
        db.flush()
        result = _public(row)
        return result

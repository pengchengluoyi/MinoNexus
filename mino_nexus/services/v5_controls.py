"""Explicit configuration writes to the existing UiControl registry."""
from __future__ import annotations

from datetime import datetime
from sqlalchemy import func

from mino_nexus.core.execution_user import require_user_id
from mino_nexus.services.control_registry import _public, _assert_runtime_writable
from mino_nexus.services.web_v5_contract import validate_locator


def audit(project_id):
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.ui_control import UiControl
    with session_scope() as db:
        return [{"control_id": cid, "count": count} for cid, count in
                db.query(UiControl.control_id, func.count(UiControl.pk)).filter_by(project_id=project_id)
                .group_by(UiControl.control_id).having(func.count(UiControl.pk) > 1).all()]


def list_controls(project_id):
    from mino_nexus.core.database import session_scope
    from mino_nexus.models.ui_control import UiControl
    with session_scope() as db:
        return [_public(row) for row in db.query(UiControl).filter_by(project_id=project_id).order_by(UiControl.pk)]


def configure(project_id, control_id, *, expected_revision, locator_spec, sensitive, publish=False):
    _assert_runtime_writable()
    actor = require_user_id()
    validate_locator(locator_spec)
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
        row.locator_spec = locator_spec
        row.sensitive = int(bool(sensitive))
        row.revision += 1
        # A valid saved locator is immediately current; no approval/publication stage.
        row.publish_state = "published"
        row.updated_by, row.updated_at = actor, datetime.now().astimezone().isoformat()
        db.flush()
        result = _public(row)
        return result


def probe(project_id, control_id, *, expected_revision, sn, url):
    """Observe a saved configuration in a fresh context; never click/fill or publish."""
    import time
    import uuid
    from urllib.parse import urlsplit
    from mino_nexus.core.schemas import PlanEvent, EventStatus
    from mino_nexus.loop.router_proxy import RouterProxy
    from mino_nexus.services.control_registry import registry_runtime
    from mino_nexus.services.v5_execution import ExecutionControl
    from mino_nexus.services import session_store
    from mino_nexus.services.session_atomic import append_batch
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise ValueError("invalid_probe_url")
    controls = [row for row in list_controls(project_id) if row["control_id"] == control_id]
    if len(controls) != 1:
        raise ValueError("control_missing" if not controls else "control_identity_conflict")
    control = controls[0]
    if control["revision"] != expected_revision:
        raise ValueError("control_revision_conflict")
    validate_locator(control["locator_spec"])
    run_id = "control-probe-" + uuid.uuid4().hex
    budget = ExecutionControl(18)
    router = RouterProxy(sn, run_id=run_id, playwright_headless=True, execution_mode="v5", execution_control=budget)
    from mino_nexus.services.execution_binding_runtime import control_probe_requirements, BindingAdmissionError
    requirements = control_probe_requirements(project_id, control)
    session_store.open_meta(session_id=run_id, run_id=run_id, case_id="", app_id=requirements['app_id'])
    result, records, stopped = {}, [], False
    opened_dispatched = False
    with registry_runtime("v5"):
        try:
            router.bind_execution(requirements, session_id=run_id, probe_url=url,
                                  deadline=min(budget.deadline, time.monotonic() + 2))
            for cap, params, timeout in [("open_web_v5", {"url": url}, 11),
                    ("read_web_target_facts", {"control": control, "requested_facts": ["count", "visible"]}, 3)]:
                opening = cap == 'open_web_v5'
                event = router.dispatch(PlanEvent(seq=0, capability_id=cap, params=params), timeout_sec=timeout,
                    operation_ref='system:open' if opening else 'control:read', binding_phase='prep' if opening else 'check')
                if opening and event.raw_response.get('action_ref'):
                    opened_dispatched = True
                result = {"status": event.status.value, "raw_response": event.raw_response, "summary": event.summary}
                records.append({"type": "control/probe", "payload": {"capability_id": cap, **result}})
                if event.status != EventStatus.PASS:
                    break
        except BindingAdmissionError as exc:
            result = {'status': 'fail', 'summary': str(exc), 'raw_response': {
                'execution': {'execution_started': 'no', 'phase': 'binding', 'reason_code': str(exc)}}}
            records.append({'type': 'control/probe', 'payload': result})
        finally:
            if opened_dispatched:
                closed = router.dispatch(PlanEvent(seq=0, capability_id="close_web_v5"), timeout_sec=1,
                                         operation_ref='system:close', binding_phase='cleanup')
                stopped = bool(closed.raw_response.get("executor_stopped"))
            else:
                stopped = True
            budget.terminate()
    refs = append_batch(run_id, records, {"status": "pass" if result.get("status") == "pass" and stopped else "fail",
        "summary": "控件只读探测，不代表用例通过", "finished_at": session_store._now(),
        "runtime_context": {"execution_mode": "v5", "purpose": "control_probe", "project_id": project_id,
                            "controls": {control_id: control}, "executor_stopped": stopped},
        "verdict": {"verification_status": "unverified", "reason_code": "configuration_probe"},
        "finalization_id": run_id}, time.monotonic() + 2, eligible=False)
    return {**result, "executor_stopped": stopped, "session_id": run_id, "event_refs": refs}

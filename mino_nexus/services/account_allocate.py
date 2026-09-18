"""按业务模板申请账号：匹配 template_id + facets，下发凭证（非唯一键）。"""
from __future__ import annotations

from typing import Any

from mino_nexus.services.account_facet_schema import merge_account_facets
from mino_nexus.services.account_pool_templates import (
    credentials_grant,
    facets_defaults_from_template,
    get_template,
    project_enabled_templates,
    template_facet_extensions,
)
from mino_nexus.services.account_lease import _mark_leased, _purge_expired_leases, apply_lease_to_ctx
from mino_nexus.services.project_env import list_test_accounts, pick_test_accounts
from mino_nexus.services.resource_pool import empty_requirements


def _requirements_for_template(
    template: dict[str, Any],
    *,
    env: str,
    extra_requirements: dict[str, Any] | None = None,
) -> dict[str, Any]:
    req = empty_requirements()
    if env:
        req["env"] = env
    defaults = facets_defaults_from_template(template)
    for key, val in defaults.items():
        req["all"].append({"facet": key, "op": "eq", "value": str(val)})
    if isinstance(extra_requirements, dict):
        for clause in extra_requirements.get("all") or []:
            if isinstance(clause, dict):
                req["all"].append(clause)
        for clause in extra_requirements.get("prefer") or []:
            if isinstance(clause, dict):
                req["prefer"].append(clause)
    return req


def allocate_by_template(
    ctx: Any,
    *,
    project_id: str,
    env_doc: dict[str, Any],
    template_id: str,
    env_profile: str = "",
    run_id: str = "",
    requirements: dict[str, Any] | None = None,
    wait_ms: int | None = None,
) -> tuple[dict[str, Any] | None, str]:
    tpl = get_template(template_id, env_doc)
    if not tpl:
        return None, f"未知模板 {template_id}"
    enabled = project_enabled_templates(env_doc)
    if enabled and template_id not in enabled:
        return None, f"项目未启用模板 {template_id}"

    ext = template_facet_extensions(tpl)
    rows = list_test_accounts(env_doc, project_id=project_id)
    req = _requirements_for_template(tpl, env=env_profile, extra_requirements=requirements)

    ranked = pick_test_accounts(
        rows,
        env=env_profile,
        env_doc=env_doc,
        requirements=req,
        run_id=run_id,
    )
    if not ranked:
        return None, f"没有符合模板「{tpl.get('label')}」的可用账号"

    row = ranked[0]
    facets = merge_account_facets(row.get("facets"), ext)
    row = {**row, "facets": facets}

    if ctx is not None:
        run_id = str(run_id or getattr(ctx, "run_id", "") or "").strip()
        aid = str(row.get("id") or row.get("account_id") or "")
        if aid and run_id:
            _purge_expired_leases(project_id)
            try:
                _mark_leased(project_id, aid, run_id)
            except Exception:
                pass
        apply_lease_to_ctx(ctx, row, project_id=project_id)

    grant = credentials_grant(row, tpl)
    grant["template_label"] = str(tpl.get("label") or "")
    grant["reason"] = str(row.get("reason") or "")
    return grant, ""


def allocate_for_context(
    ctx: Any,
    *,
    template_id: str,
    requirements: dict[str, Any] | None = None,
    wait_ms: int | None = None,
) -> tuple[dict[str, Any] | None, str]:
    from mino_nexus.services import project_store as ps

    app_id = str(getattr(ctx, "app_id", "") or "").strip()
    if not app_id:
        return None, "缺少 app_id"
    app = ps.require_app(app_id)
    project_id = str(app.get("project_id") or "").strip()
    env_doc = ps.project_env(project_id)
    return allocate_by_template(
        ctx,
        project_id=project_id,
        env_doc=env_doc,
        template_id=template_id,
        env_profile=str(getattr(ctx, "env_profile", "") or "test"),
        run_id=str(getattr(ctx, "run_id", "") or ""),
        requirements=requirements,
        wait_ms=wait_ms,
    )

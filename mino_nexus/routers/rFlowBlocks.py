"""通用逻辑块 catalog 与应用 override（Console / Studio）。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from mino_nexus.core.http_util import ok
from mino_nexus.routers.deps import current_session, require_packs_writer
from mino_nexus.services.nav_flow_block_catalog import (
    get_overrides,
    list_catalog,
    load_block_row,
    save_overrides,
    upsert_global_block,
)
from mino_nexus.models.nav_flow_block_catalog import GLOBAL_APP_ID

router = APIRouter(prefix="/flow-blocks", tags=["FlowBlocks"])


class FlowBlockOverrideRow(BaseModel):
    global_block_id: str = ""
    mode: str = "inherit"
    steps: list[dict[str, Any]] = Field(default_factory=list)
    skip_step_ids: list[str] = Field(default_factory=list)


class FlowBlockOverridesBody(BaseModel):
    overrides: list[FlowBlockOverrideRow] = Field(default_factory=list)


class FlowBlockCatalogWriteBody(BaseModel):
    display_name: str = ""
    description: str = ""
    steps_json: list[dict[str, Any]] = Field(default_factory=list)
    version: str = "v1"
    enabled: bool = True
    key_ref: str = ""


@router.get("/catalog")
def get_flow_block_catalog(
    channel: str = "",
    _sess: dict = Depends(current_session),
):
    """通用逻辑块库（`app_id=__global__`）；`channel=web|android|ios` 可筛。"""
    return ok({
        "items": list_catalog(channel=str(channel or "").strip()),
        "app_id": GLOBAL_APP_ID,
    })


@router.get("/catalog/{block_id}")
def get_flow_block_catalog_one(block_id: str, _sess: dict = Depends(current_session)):
    row = load_block_row(app_id=GLOBAL_APP_ID, block_id=str(block_id or "").strip())
    if not row:
        raise HTTPException(status_code=404, detail="block not found")
    return ok(row)


@router.put("/catalog/{block_id}")
def put_flow_block_catalog(
    block_id: str,
    body: FlowBlockCatalogWriteBody,
    _sess: dict = Depends(require_packs_writer),
):
    """更新或创建全局逻辑块（Console FSM 编排页，P3）。"""
    row = upsert_global_block(
        block_id=str(block_id or "").strip(),
        display_name=body.display_name,
        description=body.description,
        steps_json=list(body.steps_json or []),
        version=str(body.version or "v1"),
        enabled=bool(body.enabled),
        key_ref=str(body.key_ref or "").strip(),
    )
    if not row:
        raise HTTPException(status_code=400, detail="invalid block_id")
    return ok(row)


@router.get("/apps/{app_id}/overrides")
def get_app_flow_block_overrides(app_id: str, _sess: dict = Depends(current_session)):
    return ok({"app_id": app_id, "overrides": get_overrides(app_id)})


@router.put("/apps/{app_id}/overrides")
def put_app_flow_block_overrides(
    app_id: str,
    body: FlowBlockOverridesBody,
    sess: dict = Depends(current_session),
):
    rows = [r.model_dump() for r in (body.overrides or [])]
    saved = save_overrides(
        app_id,
        rows,
        updated_by=str(sess.get("username") or sess.get("user_id") or ""),
    )
    return ok({"app_id": app_id, "overrides": saved})

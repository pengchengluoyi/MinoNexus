"""任务列表。对齐上游 `rTask`。"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from mino_nexus.routers.deps import current_session
from mino_nexus import task_store as ts

router = APIRouter(prefix="/task", tags=["Task"])


class TaskCreate(BaseModel):
    app_id: str
    name: str
    type: str


@router.post("/create")
def create_task(item: TaskCreate, _sess: dict = Depends(current_session)):
    return ts.create_task(item.app_id, item.name, item.type)


@router.get("/list")
def list_tasks(
    app_id: Optional[str] = Query(None, alias="appId"),
    type: Optional[str] = None,
    keyword: Optional[str] = None,
    _sess: dict = Depends(current_session),
):
    return ts.list_tasks(app_id or "", type or "", keyword or "")

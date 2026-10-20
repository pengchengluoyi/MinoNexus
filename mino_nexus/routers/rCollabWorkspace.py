"""V7 workspaces: module-level contracts, project-scoped authorization."""
from __future__ import annotations
from typing import Any
import io
import json
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from mino_nexus.routers.deps import project_maintainer
from mino_nexus.core.http_util import ok
from mino_nexus.core.database import session_scope
from mino_nexus.services import collab_workspace as ws
from mino_nexus.models.mino_bots import CollabMessage, CollabActivity, CollabTopicMember
from mino_nexus.models.collab_read import CollabWorkspaceRead

router = APIRouter(prefix='/project/{project_id}/collab/workspaces', tags=['MinoBots workspaces'])


class WorkspaceCreate(BaseModel):
    title: str = ''
    app_id: str = ''
    requirement_id: str = ''
    modules: list[str] = Field(default_factory=list)
    env_profile: str = ''
    env_surface: str = ''


class WorkspacePatch(BaseModel):
    title: str | None = None
    app_id: str | None = None
    requirement_id: str | None = None
    modules: list[str] | None = None
    scope_case_ids: list[str] | None = None
    env_profile: str | None = None
    env_surface: str | None = None
    platform: str | None = None
    expected_build: str | None = None
    paused: bool | None = None
    archived: bool | None = None


class ChatBody(BaseModel):
    thread_id: str = ''
    body: str = Field(min_length=1, max_length=8000)


class PinBody(BaseModel):
    case_id: str


def call(fn, *args, **kwargs):
    try:
        return ok(fn(*args, **kwargs))
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get('/options')
def options(project_id: str, sess: dict = Depends(project_maintainer)):
    return call(ws.scope_options, project_id, sess)


@router.get('')
def listing(project_id: str, archived: bool = False, sess: dict = Depends(project_maintainer)):
    return call(ws.list_workspaces, project_id, sess, archived)


@router.post('')
def create(project_id: str, body: WorkspaceCreate, sess: dict = Depends(project_maintainer)):
    return call(ws.create_workspace, project_id, sess, body.model_dump())


@router.get('/{topic_id}')
def detail(project_id: str, topic_id: str, sess: dict = Depends(project_maintainer)):
    return call(ws.detail, project_id, topic_id, sess)


@router.patch('/{topic_id}')
def patch(project_id: str, topic_id: str, body: WorkspacePatch, sess: dict = Depends(project_maintainer)):
    return call(ws.update_workspace, project_id, topic_id, sess, body.model_dump(exclude_none=True))


@router.post('/{topic_id}/cases')
def pin(project_id: str, topic_id: str, body: PinBody, sess: dict = Depends(project_maintainer)):
    return call(ws.pin_case, project_id, topic_id, sess, body.case_id)


@router.post('/{topic_id}/messages')
def chat(project_id: str, topic_id: str, body: ChatBody, sess: dict = Depends(project_maintainer)):
    from mino_nexus.services.collab_chat import chat as send
    return call(send, project_id, topic_id, sess, body.body, body.thread_id)


@router.delete('/{topic_id}')
def delete(project_id: str, topic_id: str, sess: dict = Depends(project_maintainer)):
    ws.authorize(project_id, sess)
    with session_scope() as db:
        db.execute(text('BEGIN IMMEDIATE'))
        topic = ws.owned(db, project_id, topic_id)
        from mino_nexus.models.collab_message_job import CollabMessageJob
        for model in (CollabWorkspaceRead, CollabMessageJob, CollabActivity, CollabMessage, CollabTopicMember):
            db.query(model).filter_by(topic_id=topic_id).delete(synchronize_session=False)
        db.delete(topic)
    return ok({'deleted': True, 'topic_id': topic_id})


@router.post('/{topic_id}/attachments')
async def attachment(project_id: str, topic_id: str, file: UploadFile = File(...), sess: dict = Depends(project_maintainer)):
    owner = ws.authorize(project_id, sess)
    with session_scope() as db:
        ws.owned(db, project_id, topic_id, write=True)
    raw = await file.read(8 * 1024 * 1024 + 1)
    if len(raw) > 8 * 1024 * 1024:
        raise HTTPException(413, '截图最大 8MB')
    from PIL import Image
    try:
        image = Image.open(io.BytesIO(raw))
        if image.width * image.height > 20_000_000:
            raise ValueError('图片过大')
        image.load()
        image = image.convert('RGB')
    except Exception as exc:
        raise HTTPException(400, '请上传有效的截图图片') from exc
    from mino_nexus.core.paths import data_dir
    ref = ws.uid()
    directory = data_dir() / 'collab-attachments'
    directory.mkdir(exist_ok=True)
    target = directory / (ref + '.png')
    with target.open('xb') as handle:
        image.save(handle, format='PNG')
    with session_scope() as db:
        topic = ws.owned(db, project_id, topic_id, write=True)
        row = ws.message(db, topic, str(file.filename or '截图')[:200], card='attachment', ref=ref, author='user', owner=owner)
        db.flush()
        result = ws.public(row)
    return ok(result)


@router.get('/{topic_id}/attachments/{ref}')
def read_attachment(project_id: str, topic_id: str, ref: str, sess: dict = Depends(project_maintainer)):
    ws.authorize(project_id, sess)
    with session_scope() as db:
        ws.owned(db, project_id, topic_id)
        row = db.query(CollabMessage).filter_by(topic_id=topic_id, card_kind='attachment', ref_id=ref).first()
        if row is None:
            raise HTTPException(404, '附件不存在')
    import uuid
    try:
        ref = str(uuid.UUID(ref))
    except ValueError as exc:
        raise HTTPException(404, '附件不存在') from exc
    from mino_nexus.core.paths import data_dir
    return FileResponse(data_dir() / 'collab-attachments' / (ref + '.png'), media_type='image/png')


@router.post('/{topic_id}/read')
def read_workspace(project_id: str, topic_id: str, sess: dict = Depends(project_maintainer)):
    return call(ws.mark_read, project_id, topic_id, sess)


class MemberBody(BaseModel):
    member: str


class DefaultMembersBody(BaseModel):
    members: list[str]


@router.get('/{topic_id}/members')
def members(project_id: str, topic_id: str, sess: dict = Depends(project_maintainer)):
    from mino_nexus.services.workspace_members import list_members, catalog, role_directory
    return call(lambda: {'members': list_members(project_id, topic_id, sess), 'available': list(catalog().values()), 'roles': role_directory()})


@router.post('/{topic_id}/members')
def add_member(project_id: str, topic_id: str, body: MemberBody, sess: dict = Depends(project_maintainer)):
    from mino_nexus.services.workspace_members import change_member
    return call(change_member, project_id, topic_id, sess, body.member)


@router.delete('/{topic_id}/members/{member}')
def remove_member(project_id: str, topic_id: str, member: str, sess: dict = Depends(project_maintainer)):
    from mino_nexus.services.workspace_members import change_member
    return call(change_member, project_id, topic_id, sess, member, True)


@router.get('/settings/default-members')
def get_default_members(project_id: str, sess: dict = Depends(project_maintainer)):
    from mino_nexus.services.workspace_members import default_settings
    return call(default_settings)


@router.put('/settings/default-members')
def set_default_members(project_id: str, body: DefaultMembersBody, sess: dict = Depends(project_maintainer)):
    from mino_nexus.routers.deps import require_packs_writer
    from mino_nexus.services.workspace_members import default_settings
    require_packs_writer(sess)
    return call(default_settings, body.members)


@router.post('/{topic_id}/members/defaults')
def restore_default_members(project_id: str, topic_id: str, sess: dict = Depends(project_maintainer)):
    from mino_nexus.services.workspace_members import restore_defaults
    return call(restore_defaults, project_id, topic_id, sess)

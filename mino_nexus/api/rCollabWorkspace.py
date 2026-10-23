"""V7 workspaces: module-level contracts, project-scoped authorization."""
from __future__ import annotations
from typing import Any
import io
import json
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Request, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from mino_nexus.api.deps import project_maintainer, current_session
from mino_nexus.core.http_util import ok
from mino_nexus.core.database import session_scope
from mino_nexus.assistant.workspace import collab_workspace as ws
from mino_nexus.models.mino_bots import CollabMessage

def require_workspace_owner(request: Request, sess: dict = Depends(current_session)):
    topic_id = request.path_params.get('topic_id')
    if topic_id:
        with session_scope() as db:
            try:
                ws.owned(db, request.path_params['project_id'], topic_id)
            except KeyError as exc:
                raise HTTPException(404, '工作区不存在') from exc


router = APIRouter(prefix='/project/{project_id}/collab/workspaces', tags=['MinoBots workspaces'],
                   dependencies=[Depends(require_workspace_owner)])


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
    archived: bool | None = None


class ChatBody(BaseModel):
    thread_id: str = ''
    body: str = Field(min_length=1, max_length=8000)


class HandoffBody(BaseModel):
    work_id: str = Field(min_length=1, max_length=80)
    member_id: str = Field(min_length=1, max_length=80)
    note: str = Field(default='', max_length=1000)


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
    return call(ws.create_workspace, project_id, sess, body.model_dump(exclude_unset=True))


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
    from mino_nexus.assistant.workspace.collab_chat import chat as send
    return call(send, project_id, topic_id, sess, body.body, body.thread_id)


@router.post('/{topic_id}/handoff')
def handoff(project_id: str, topic_id: str, body: HandoffBody, sess: dict = Depends(project_maintainer)):
    return call(ws.handoff_work, project_id, topic_id, sess, body.work_id, body.member_id, body.note)


@router.post('/{topic_id}/attachments')
async def attachment(project_id: str, topic_id: str, file: UploadFile = File(...), sess: dict = Depends(project_maintainer)):
    owner = ws.authorize(project_id, sess)
    with session_scope() as db:
        ws.owned(db, project_id, topic_id, write=True)
    filename = str(file.filename or '附件').replace('\\', '/').rsplit('/', 1)[-1]
    filename = ''.join(c for c in filename if ord(c) >= 32 and ord(c) != 127).strip()[:200] or '附件'
    is_image = file.content_type in ('image/png', 'image/jpeg', 'image/webp') or filename.lower().endswith(('.png', '.jpg', '.jpeg', '.webp'))
    limit = (8 if is_image else 20) * 1024 * 1024
    raw = await file.read(limit + 1)
    if len(raw) > limit:
        raise HTTPException(413, '截图最大 8MB' if is_image else '附件最大 20MB')
    if not raw:
        raise HTTPException(400, '不能发送空文件')
    image = None
    if is_image:
        from PIL import Image
        try:
            image = Image.open(io.BytesIO(raw))
            if image.format not in ('PNG', 'JPEG', 'WEBP') or image.width * image.height > 20_000_000:
                raise ValueError('图片格式不支持或图片过大')
            image.load()
            image = image.convert('RGB')
        except Exception as exc:
            raise HTTPException(400, '请上传有效的 PNG、JPEG 或 WebP 截图') from exc
    from mino_nexus.core.paths import data_dir
    ref = ws.uid()
    directory = data_dir() / 'collab-attachments'
    directory.mkdir(exist_ok=True)
    target = directory / (ref + ('.png' if is_image else '.bin'))
    with target.open('xb') as handle:
        if image is not None:
            image.save(handle, format='PNG')
        else:
            handle.write(raw)
    with session_scope() as db:
        topic = ws.owned(db, project_id, topic_id, write=True)
        row = ws.message(db, topic, filename, card='attachment', ref=ref, author='user', owner=owner)
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
        filename = row.body
    import uuid
    try:
        ref = str(uuid.UUID(ref))
    except ValueError as exc:
        raise HTTPException(404, '附件不存在') from exc
    from mino_nexus.core.paths import data_dir
    directory = data_dir() / 'collab-attachments'
    image = directory / (ref + '.png')
    if image.is_file():
        return FileResponse(image, media_type='image/png')
    target = directory / (ref + '.bin')
    if not target.is_file():
        raise HTTPException(404, '附件文件不存在')
    return FileResponse(target, media_type='application/octet-stream', filename=filename,
                        headers={'X-Content-Type-Options': 'nosniff'})


@router.post('/{topic_id}/read')
def read_workspace(project_id: str, topic_id: str, sess: dict = Depends(project_maintainer)):
    return call(ws.mark_read, project_id, topic_id, sess)


class MemberBody(BaseModel):
    member: str


class DefaultMembersBody(BaseModel):
    members: list[str]


@router.get('/{topic_id}/members')
def members(project_id: str, topic_id: str, sess: dict = Depends(project_maintainer)):
    from mino_nexus.assistant.workspace.workspace_members import list_members, catalog, role_directory
    return call(lambda: {'members': list_members(project_id, topic_id, sess), 'available': list(catalog().values()), 'roles': role_directory()})


@router.post('/{topic_id}/members')
def add_member(project_id: str, topic_id: str, body: MemberBody, sess: dict = Depends(project_maintainer)):
    from mino_nexus.assistant.workspace.workspace_members import change_member
    return call(change_member, project_id, topic_id, sess, body.member)


@router.delete('/{topic_id}/members/{member}')
def remove_member(project_id: str, topic_id: str, member: str, sess: dict = Depends(project_maintainer)):
    from mino_nexus.assistant.workspace.workspace_members import change_member
    return call(change_member, project_id, topic_id, sess, member, True)


@router.get('/settings/default-members')
def get_default_members(project_id: str, sess: dict = Depends(project_maintainer)):
    from mino_nexus.assistant.workspace.workspace_members import default_settings
    return call(default_settings)


@router.put('/settings/default-members')
def set_default_members(project_id: str, body: DefaultMembersBody, sess: dict = Depends(project_maintainer)):
    from mino_nexus.api.deps import require_packs_writer
    from mino_nexus.assistant.workspace.workspace_members import default_settings
    require_packs_writer(sess)
    return call(default_settings, body.members)


@router.post('/{topic_id}/members/defaults')
def restore_default_members(project_id: str, topic_id: str, sess: dict = Depends(project_maintainer)):
    from mino_nexus.assistant.workspace.workspace_members import restore_defaults
    return call(restore_defaults, project_id, topic_id, sess)


class PinCasesBody(BaseModel):
    case_ids: list[str] = Field(min_length=1, max_length=1000)


from mino_nexus.assistant.workspace.workspace_automation import Policy


class AutomationBody(BaseModel):
    config: Policy
    revision: int = Field(ge=0)


class ReportBody(BaseModel):
    run_id: str = ''


@router.get('/{topic_id}/scope')
def scope_get(project_id: str, topic_id: str, sess: dict = Depends(project_maintainer)):
    ws.authorize(project_id, sess)
    with session_scope() as db:
        topic = ws.owned(db, project_id, topic_id)
        return ok({'topic': ws.public(topic), 'case_ids': ws.scope_ids(db, topic)})


@router.post('/{topic_id}/pin-cases')
def pin_cases(project_id: str, topic_id: str, body: PinCasesBody, sess: dict = Depends(project_maintainer)):
    return call(ws.pin_cases, project_id, topic_id, sess, body.case_ids)


@router.get('/{topic_id}/automation')
def automation_get(project_id: str, topic_id: str, sess: dict = Depends(project_maintainer)):
    from mino_nexus.assistant.workspace.workspace_automation import get
    return call(get, project_id, topic_id, sess)


@router.put('/{topic_id}/automation')
def automation_save(project_id: str, topic_id: str, body: AutomationBody, sess: dict = Depends(project_maintainer)):
    from mino_nexus.assistant.workspace.workspace_automation import save
    return call(save, project_id, topic_id, sess, body.config.model_dump(), body.revision)


@router.get('/{topic_id}/report')
def report_get(project_id: str, topic_id: str, run_id: str = '', sess: dict = Depends(project_maintainer)):
    from mino_nexus.assistant.workspace.workspace_reports import facts, markdown
    result = call(facts, project_id, topic_id, sess, run_id)
    result['data']['markdown'] = markdown(result['data'])
    return result


@router.post('/{topic_id}/report')
def report_create(project_id: str, topic_id: str, body: ReportBody, sess: dict = Depends(project_maintainer)):
    import time
    from mino_nexus.assistant.workspace.workspace_assign import assign
    return call(assign, project_id, topic_id, sess,
        {'member': 'report-writer', 'job': 'draft_test_report', 'target': {'run_id': body.run_id} if body.run_id else {}},
        context={}, deadline=time.monotonic() + 60)


class DecisionBody(BaseModel):
    body: str = Field(min_length=1, max_length=4000)
    run_id: str = ''
    request_id: str = Field(min_length=1, max_length=160)


@router.get('/{topic_id}/review')
def review(project_id: str, topic_id: str, sess: dict = Depends(project_maintainer)):
    from mino_nexus.assistant.workspace.workspace_review import overview
    return call(overview, project_id, topic_id, sess)


@router.post('/{topic_id}/decisions')
def decision(project_id: str, topic_id: str, body: DecisionBody, sess: dict = Depends(project_maintainer)):
    from mino_nexus.assistant.workspace.workspace_review import decide
    return call(decide, project_id, topic_id, sess, body.body, body.run_id, body.request_id)


@router.get('/{topic_id}/runs')
def workspace_runs(project_id: str, topic_id: str, offset: int = Query(default=0, ge=0),
                   limit: int = Query(default=20, ge=1, le=100), sess: dict = Depends(project_maintainer)):
    return call(ws.list_runs, project_id, topic_id, sess, offset, limit)

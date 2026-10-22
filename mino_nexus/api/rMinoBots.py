"""Workspace API; retired parallel collaboration endpoints stay unavailable."""
from fastapi import APIRouter, Depends, HTTPException
from mino_nexus.api.deps import current_session
from mino_nexus.api.rCollabWorkspace import router as workspace_router
router = APIRouter()
router.include_router(workspace_router)

@router.api_route('/project/{project_id}/collab/{legacy_path:path}', methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE'])
def retired(project_id: str, legacy_path: str, sess: dict = Depends(current_session)):
    if legacy_path.startswith('workspaces'):
        raise HTTPException(404, '工作区资源不存在')
    raise HTTPException(410, '旧议题接口已撤除，历史数据保留；请使用测试工作区。')

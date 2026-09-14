"""文档库：上传 PDF/Markdown、全文检索。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from mino_nexus.core.http_util import http_error, ok
from mino_nexus.routers.deps import current_session
from mino_nexus.services import doc_learn as dl
from mino_nexus.services import doc_store as ds
from mino_nexus.services import doc_sync as dsync
from mino_nexus.services import feishu_doc_service as fds

router = APIRouter(prefix="/settings/docs", tags=["DocLibrary"])


class FeishuSyncBody(BaseModel):
    url: str = ""
    app_id: str = ""
    project_id: str = ""
    bot_id: str = ""
    title: str = ""


class DocSyncSettingsBody(BaseModel):
    auto_sync: bool = True
    sync_interval_sec: int = 3600


@router.get("")
def list_docs(
    app_id: str = "",
    project_id: str = "",
    _sess: dict = Depends(current_session),
):
    aid = str(app_id or "").strip()
    if not aid:
        raise HTTPException(status_code=400, detail="app_id 不能为空")
    return ok({"items": ds.list_sources(app_id=aid, project_id=project_id)})


@router.get("/search")
def search_docs(
    q: str = "",
    app_id: str = "",
    limit: int = 20,
    vector: int = 0,
    _sess: dict = Depends(current_session),
):
    aid = str(app_id or "").strip()
    if not aid:
        raise HTTPException(status_code=400, detail="app_id 不能为空")
    query = str(q or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="q 不能为空")
    return ok({
        "items": ds.search_documents(
            q=query,
            app_id=aid,
            limit=limit,
            use_query_embed=bool(int(vector or 0)),
        ),
    })


@router.post("/upload")
async def upload_doc(
    file: UploadFile = File(...),
    app_id: str = Form(""),
    project_id: str = Form(""),
    title: str = Form(""),
    _sess: dict = Depends(current_session),
):
    data = await file.read()
    try:
        row = ds.ingest_upload(
            data=data,
            filename=str(file.filename or ""),
            app_id=app_id,
            project_id=project_id,
            title=title,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        http_error(e)
    return ok(row, msg="文档已入库")


@router.post("/sync-feishu")
def sync_feishu_doc(body: FeishuSyncBody, _sess: dict = Depends(current_session)):
    """从飞书 wiki/docx 链接同步文档到文档库。"""
    url = str(body.url or "").strip()
    aid = str(body.app_id or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="url 不能为空")
    if not aid:
        raise HTTPException(status_code=400, detail="app_id 不能为空")
    try:
        row = fds.sync_feishu_document(
            url=url,
            app_id=aid,
            project_id=str(body.project_id or "").strip(),
            bot_id=str(body.bot_id or "").strip(),
            title=str(body.title or "").strip(),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        http_error(e)
    return ok(row, msg="飞书文档已同步")


@router.patch("/{source_id}/sync")
def patch_doc_sync(
    source_id: str,
    body: DocSyncSettingsBody,
    _sess: dict = Depends(current_session),
):
    row = ds.set_sync_settings(
        source_id,
        auto_sync=body.auto_sync,
        sync_interval_sec=body.sync_interval_sec,
    )
    if not row:
        raise HTTPException(status_code=404, detail="文档不存在")
    return ok(row, msg="同步设置已更新")


@router.post("/{source_id}/reindex")
def reindex_doc(source_id: str, _sess: dict = Depends(current_session)):
    try:
        row = ds.reindex_source(source_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        http_error(e)
    return ok(row, msg="索引已重建")


@router.post("/{source_id}/sync-now")
def sync_doc_now(source_id: str, _sess: dict = Depends(current_session)):
    try:
        row = dsync.sync_source_now(source_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        http_error(e)
    return ok(row, msg="已同步")


@router.get("/{source_id}")
def get_doc(source_id: str, _sess: dict = Depends(current_session)):
    row = ds.get_source(source_id)
    if not row:
        raise HTTPException(status_code=404, detail="文档不存在")
    return ok(row)


@router.get("/{source_id}/chunks")
def get_doc_chunks(
    source_id: str,
    offset: int = 0,
    limit: int = 50,
    _sess: dict = Depends(current_session),
):
    if not ds.get_source(source_id):
        raise HTTPException(status_code=404, detail="文档不存在")
    return ok({"items": ds.list_chunks(source_id, offset=offset, limit=limit)})


@router.delete("/{source_id}")
def remove_doc(source_id: str, _sess: dict = Depends(current_session)):
    if not ds.delete_source(source_id):
        raise HTTPException(status_code=404, detail="文档不存在")
    return ok({"id": source_id}, msg="已删除")


@router.post("/{source_id}/extract")
def extract_doc_knowledge(
    source_id: str,
    app_id: str = "",
    project_id: str = "",
    _sess: dict = Depends(current_session),
):
    """DOC_LEARN：从文档分片抽取执行知识条（review_status=pending）。"""
    aid = str(app_id or "").strip()
    if not aid:
        meta = ds.get_source(source_id)
        aid = str((meta or {}).get("app_id") or "").strip()
    if not aid:
        raise HTTPException(status_code=400, detail="app_id 不能为空")
    try:
        result = dl.extract_from_source(
            source_id,
            app_id=aid,
            project_id=str(project_id or "").strip(),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        http_error(e)
    n = int(result.get("saved") or 0)
    return ok(result, msg=f"已抽取 {n} 条，待审核")

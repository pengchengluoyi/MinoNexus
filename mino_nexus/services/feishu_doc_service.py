"""飞书 docx / 知识库文档 → 文档库 ingest。"""
from __future__ import annotations

import hashlib
import re
import time
from typing import Any
from urllib.parse import urlparse

import requests

from mino_nexus.core.log import SLog
from mino_nexus.services import doc_store as ds
from mino_nexus.services import plugins_store as ps

TAG = "FeishuDoc"
_FEISHU_BASE = "https://open.feishu.cn/open-apis"
_TOKEN_CACHE: dict[str, dict[str, Any]] = {}


def parse_feishu_doc_url(url: str) -> dict[str, str]:
    raw = str(url or "").strip()
    out = {"url": raw, "wiki_token": "", "doc_token": "", "link_type": ""}
    if not raw:
        return out
    wiki_m = re.search(r"/wiki/([A-Za-z0-9]+)", raw, re.I)
    if wiki_m:
        out["wiki_token"] = wiki_m.group(1)
        out["link_type"] = "wiki"
        return out
    doc_m = re.search(r"/docx/([A-Za-z0-9]+)", raw, re.I)
    if doc_m:
        out["doc_token"] = doc_m.group(1)
        out["link_type"] = "docx"
        return out
    path = urlparse(raw).path or ""
    if path.strip("/").endswith("docx"):
        out["link_type"] = "unknown"
    return out


def _lark_credentials(bot_id: str = "") -> tuple[str, str]:
    creds = ps.resolve_lark_credentials(bot_id=bot_id)
    app_id = str(creds.get("app_id") or "").strip()
    app_secret = str(creds.get("app_secret") or "").strip()
    if not app_id or not app_secret:
        raise RuntimeError("未配置飞书机器人：请在 Console 设置 → 机器人 中添加 app_id / app_secret")
    return app_id, app_secret


def get_tenant_access_token(bot_id: str = "") -> str:
    cache_key = str(bot_id or "__default__")
    cached = _TOKEN_CACHE.get(cache_key) or {}
    now = time.time()
    if cached.get("token") and float(cached.get("expire_at") or 0) > now + 60:
        return str(cached["token"])
    app_id, app_secret = _lark_credentials(bot_id)
    resp = requests.post(
        f"{_FEISHU_BASE}/auth/v3/tenant_access_token/internal",
        json={"app_id": app_id, "app_secret": app_secret},
        timeout=30,
    )
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"飞书鉴权失败: {data.get('msg', data)}")
    token = str(data.get("tenant_access_token") or "")
    expire = int(data.get("expire") or 7200)
    _TOKEN_CACHE[cache_key] = {"token": token, "expire_at": now + expire}
    return token


def wiki_get_node(node_token: str, *, bot_id: str = "") -> dict[str, Any]:
    token = get_tenant_access_token(bot_id)
    resp = requests.get(
        f"{_FEISHU_BASE}/wiki/v2/spaces/get_node",
        params={"token": node_token},
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    body = resp.json()
    if body.get("code") != 0:
        raise RuntimeError(
            f"解析知识库节点失败: {body.get('msg', body)}。"
            "请确认机器人有 wiki 权限且能访问该文档。"
        )
    return (body.get("data") or {}).get("node") or {}


def fetch_doc_raw_content(document_id: str, *, bot_id: str = "") -> str:
    doc_id = str(document_id or "").strip()
    if not doc_id:
        raise ValueError("document_id 为空")
    token = get_tenant_access_token(bot_id)
    resp = requests.get(
        f"{_FEISHU_BASE}/docx/v1/documents/{doc_id}/raw_content",
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
    )
    body = resp.json()
    if body.get("code") != 0:
        raise RuntimeError(f"读取飞书文档正文失败: {body.get('msg', body)}")
    content = str((body.get("data") or {}).get("content") or "")
    if not content.strip():
        raise RuntimeError("飞书文档正文为空")
    return content


def resolve_document_id(url: str, *, bot_id: str = "") -> tuple[str, str]:
    """返回 (document_id, title)。"""
    parsed = parse_feishu_doc_url(url)
    if parsed.get("doc_token"):
        return str(parsed["doc_token"]), ""
    wiki_token = str(parsed.get("wiki_token") or "").strip()
    if wiki_token:
        node = wiki_get_node(wiki_token, bot_id=bot_id)
        obj_type = str(node.get("obj_type") or "").strip().lower()
        obj_token = str(node.get("obj_token") or "").strip()
        title = str(node.get("title") or "").strip()
        if obj_type != "docx" or not obj_token:
            raise RuntimeError(f"该知识库节点类型为「{obj_type or '未知'}」，目前仅支持 docx 文档")
        return obj_token, title
    raise RuntimeError("无法识别飞书链接，请使用 /wiki/ 或 /docx/ 地址")


def sync_feishu_document(
    *,
    url: str,
    app_id: str,
    project_id: str = "",
    bot_id: str = "",
    title: str = "",
    enable_auto_sync: bool = True,
) -> dict[str, Any]:
    """拉取飞书 docx 正文并写入文档库。"""
    src_url = str(url or "").strip()
    aid = str(app_id or "").strip()
    doc_id, node_title = resolve_document_id(src_url, bot_id=bot_id)
    body = fetch_doc_raw_content(doc_id, bot_id=bot_id)
    display = str(title or node_title or f"飞书文档 {doc_id[:8]}").strip()
    data = body.encode("utf-8")
    content_hash = hashlib.sha256(data).hexdigest()
    existing = ds.find_source_by_url(app_id=aid, source_url=src_url)
    if existing and str(existing.get("content_hash") or "") == content_hash:
        ds.touch_source(str(existing.get("id") or ""))
        if enable_auto_sync:
            ds.set_sync_settings(str(existing.get("id") or ""), auto_sync=1)
        SLog.i(TAG, f"feishu unchanged {doc_id} -> {existing.get('id')}")
        return ds.get_source(str(existing.get("id") or "")) or existing
    row = ds.ingest_upload(
        data=data,
        filename=f"{display}.md",
        app_id=aid,
        project_id=project_id,
        title=display,
        source_kind="feishu",
        source_url=src_url,
        feishu_bot_id=str(bot_id or "").strip(),
    )
    if enable_auto_sync and row.get("id"):
        ds.set_sync_settings(str(row["id"]), auto_sync=1)
    SLog.i(TAG, f"synced feishu doc {doc_id} -> {row.get('id')}")
    return row

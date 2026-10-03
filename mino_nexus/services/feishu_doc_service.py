"""飞书 docx / 知识库文档 → 文档库 ingest。"""
from __future__ import annotations

import hashlib
import re
from typing import Any
from urllib.parse import urlparse

from mino_nexus.core.log import SLog
from mino_nexus.services import doc_store as ds

TAG = "FeishuDoc"


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


def sync_feishu_document(
    *,
    url: str,
    app_id: str,
    project_id: str = "",
    bot_id: str = "",
    title: str = "",
    enable_auto_sync: bool = True,
    node_id: str = "",
) -> dict[str, Any]:
    """让已配置飞书的 Scout 读取正文，再写入文档库。密钥不经过这里。"""
    from mino_nexus.services.node_plugin_call import PluginCallError, call_plugin

    src_url = str(url or "").strip()
    aid = str(app_id or "").strip()
    del bot_id
    try:
        fetched = call_plugin(
            kind="cli",
            plugin_id="feishu",
            node_id=node_id,
            capability_id="plugin.cli.feishu",
            params={"action": "read_doc", "url": src_url},
            timeout=90,
        )
    except PluginCallError as exc:
        raise RuntimeError(str(exc)) from exc
    body = str(fetched.get("content") or "")
    if not body.strip():
        raise RuntimeError("飞书文档正文为空")
    doc_id = str(fetched.get("document_id") or "feishu")
    node_title = str(fetched.get("title") or "")
    display = str(title or node_title or "飞书文档").strip()
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

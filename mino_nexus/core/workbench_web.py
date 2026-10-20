"""Packaged Workbench, registered after API and WebSocket routes."""
from __future__ import annotations
import json
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse


def mount_workbench(app: FastAPI) -> None:
    root = Path(__file__).resolve().parents[1] / "web"
    if not (root / "index.html").is_file():
        return  # Source-only development keeps the independent Vite server.
    root = root.resolve()
    build = json.loads((root / "BUILD.json").read_text(encoding="utf-8"))
    index = (root / "index.html").read_text(encoding="utf-8")
    # Resolve relative Vite assets at / even on a deep SPA navigation.
    index = index.replace("<head>", '<head><base href="/">', 1)
    build_id = json.dumps(str(build.get("build_id", build.get("version", "")))).replace("<", r"\u003c")
    config = '<script>window.__MINO_CONFIG__={nexusUrl:location.origin,buildId:' + build_id + '};</script>'
    index = index.replace("</head>", config + "</head>", 1)
    reserved = {"ws", "node", "static", "assets", "health"}

    @app.get("/{web_path:path}", include_in_schema=False)
    async def workbench(request: Request, web_path: str):
        path = (root / web_path).resolve()
        if not path.is_relative_to(root):
            raise HTTPException(404)
        if path.is_file() and path.name != "index.html":
            return FileResponse(path)
        if web_path and (web_path.split("/")[0] in reserved or Path(web_path).suffix):
            raise HTTPException(404)
        if web_path and "text/html" not in request.headers.get("accept", ""):
            raise HTTPException(404)
        return HTMLResponse(index, headers={"Cache-Control": "no-store"})

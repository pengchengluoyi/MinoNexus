"""Local credential provisioning and bounded health_contract_v1 verification."""
from __future__ import annotations
import argparse
import json
import os
import sys
from pathlib import Path
import urllib.request
from urllib.parse import urlsplit

def main(argv=None):
    ap = argparse.ArgumentParser(prog="mino-nexus probe")
    sub = ap.add_subparsers(dest="command", required=True)
    issue = sub.add_parser("issue", help="Provision on an offline, explicitly selected data root")
    issue.add_argument("--data-dir", required=True)
    issue.add_argument("--output", required=True)
    check = sub.add_parser("check")
    check.add_argument("--url", required=True)
    check.add_argument("--token-file", required=True)
    check.add_argument("--instance-id", required=True)
    check.add_argument("--build-id", required=True)
    args = ap.parse_args(argv)
    if args.command == "issue":
        if not args.data_dir.strip():
            ap.error("empty data root")
        target = Path(args.output).expanduser().resolve()
        # Reserve output before creating a credential; never overwrite a secret.
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.environ["MINO_NEXUS_DEV_DATA_DIR" if not getattr(sys, "frozen", False) else "MINO_NEXUS_DATA_DIR"] = str(Path(args.data_dir).expanduser().resolve())
            from mino_nexus.core.database import ensure_db
            ensure_db()
            from mino_nexus.services.assistant_store import create_probe_token
            with os.fdopen(fd, "w") as stream:
                fd = None
                stream.write(create_probe_token()["token"])
        finally:
            if fd is not None:
                os.close(fd)
        print("probe credential provisioned")
        return 0
    parsed = urlsplit(args.url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        ap.error("expected an HTTP(S) origin")
    if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        ap.error("non-loopback probe requires HTTPS")
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    base = args.url.rstrip("/")
    token = Path(args.token_file).read_text().strip()
    def fetch(path, payload=None):
        req = urllib.request.Request(base + path,
            data=json.dumps(payload).encode() if payload else None,
            headers={"Content-Type":"application/json", **({"Authorization":"Bearer " + token} if payload else {})})
        with opener.open(req, timeout=10) as response:
            return json.load(response)
    fetch("/health")
    info = fetch("/sys/server_info")["data"]
    if (info.get("instance_id"), info.get("build_id")) != (args.instance_id, args.build_id):
        raise RuntimeError("probe target identity mismatch")
    for i, method in enumerate(("initialize", "ping", "tools/list")):
        result = fetch("/mcp", {"jsonrpc":"2.0", "id":i, "method":method, "params":{}})
        if result.get("id") != i or "result" not in result or "error" in result:
            raise RuntimeError("MCP probe failed: " + method)
    print(json.dumps({"contract":"health_contract_v1", "instance_id":args.instance_id,
                      "build_id":args.build_id, "status":"passed"}))
    return 0

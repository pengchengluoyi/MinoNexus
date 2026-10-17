"""Schema 1 manifest; only validated native binary items become installable."""
import argparse
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dist", type=Path, required=True)
    ap.add_argument("--prefix", required=True)
    args = ap.parse_args()
    url = urlsplit(args.prefix)
    if url.scheme != "https" or url.hostname != "github.com" or "/releases/download/" not in url.path or "/latest" in url.path:
        raise ValueError("release URL must use a fixed GitHub tag")
    rows = [json.loads(p.read_text()) for p in sorted(args.dist.glob("item-*.json"))]
    if not rows:
        raise ValueError("no successful binary packages")
    first = rows[0]
    identities = set()
    for row in rows:
        if row.get("dirty") or row.get("payload") != "binary":
            raise ValueError("dirty/source packages are not installable releases")
        if any(row[k] != first[k] for k in ("component", "build_id", "source_commit", "version")):
            raise ValueError("mixed release identities")
        if not re.fullmatch(r"[0-9a-f]{40}", row["source_commit"]):
            raise ValueError("invalid source commit")
        key = (row["os"], row["arch"])
        if key in identities:
            raise ValueError("duplicate platform")
        identities.add(key)
        p = args.dist / row["filename"]
        if p.resolve().parent != args.dist.resolve() or p.stat().st_size != row["bytes"] or hashlib.sha256(p.read_bytes()).hexdigest() != row["sha256"]:
            raise ValueError("package bytes/digest mismatch")
        row["url"] = args.prefix.rstrip("/") + "/" + row["filename"]
    manifest = {"manifest_schema_version":1, "component":first["component"], "version":first["version"],
                "build_id":first["build_id"], "source_commit":first["source_commit"],
                "compatibility":{'runtime': {'bundled': True, 'python': '3.12'}, 'protocol': {'node': 2, 'mcp': '2025-03-26'}, 'api': {'admission': 'explicit-tested-build-pair', 'tested_build_pairs': []}, 'schema': {'migration': 'ensure_db-and-bootstrap', 'rollback_compatibility': 'unverified'}, 'automatic_rollback': False, 'installation': 'baseline-v1', 'health': 'health_contract_v1'}, "items":rows}
    (args.dist / "manifest.json").write_text(json.dumps(manifest, indent=2))

if __name__ == "__main__":
    main()

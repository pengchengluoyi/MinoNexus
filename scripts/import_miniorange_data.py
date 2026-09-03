#!/usr/bin/env python3
"""把 MiniOrangeServer 本机数据目录导入 MinoNexus。

只读上游目录，不改 MiniOrangeServer。Nexus 侧写：
  projects.json          项目 + 应用（用例在 app.env.automation.qa_process）
  icon_targets.json      登录图标槽
  settings.json          knowledge / 集成插件 / 机器人 / Figma（与现有 mail/ai 合并）
  data/dispatch/calls.jsonl + uploads/dispatch/   调用记录
  packs/learned/         只读扩展包副本（不发明写 YAML）

用法：
    python scripts/import_miniorange_data.py
    python scripts/import_miniorange_data.py --dry-run
    python scripts/import_miniorange_data.py --force   # 覆盖已有同 id
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mino_nexus.paths import data_dir  # noqa: E402


def default_source() -> Path:
    env = str(os.environ.get("MINO_ORANGE_DATA") or "").strip()
    if env:
        return Path(env).expanduser()
    if os.name == "nt":
        return Path(os.environ.get("APPDATA") or Path.home()) / "MiniOrangeServer"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "MiniOrangeServer"
    return Path.home() / ".local" / "share" / "MiniOrangeServer"


def _parse_json(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    if isinstance(raw, str) and raw.strip():
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}
    return {}


def _count_cases(env: dict) -> int:
    auto = env.get("automation") if isinstance(env.get("automation"), dict) else {}
    qp = auto.get("qa_process") if isinstance(auto.get("qa_process"), dict) else {}
    n = 0
    for req in qp.get("requirements") or []:
        if not isinstance(req, dict):
            continue
        drafts = req.get("draft_cases") or []
        if isinstance(drafts, list):
            n += sum(1 for x in drafts if isinstance(x, dict) and str(x.get("case_id") or "").strip())
    return n


def load_sqlite(db: Path) -> tuple[list[dict], list[dict], dict[str, list]]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    projects = []
    for r in con.execute("SELECT id, uid, name, description, env FROM projects"):
        projects.append({
            "id": r["id"],
            "uid": r["uid"] or r["id"],
            "name": r["name"] or "",
            "description": r["description"] or "",
            "env": _parse_json(r["env"]),
        })
    apps = []
    for r in con.execute("SELECT id, uid, name, description, platforms, env, project_id FROM apps"):
        apps.append({
            "id": r["id"],
            "uid": r["uid"] or r["id"],
            "name": r["name"] or "",
            "description": r["description"] or "",
            "platforms": r["platforms"] or "",
            "env": _parse_json(r["env"]),
            "project_id": r["project_id"],
        })
    icons: dict[str, list] = {}
    try:
        rows = con.execute(
            "SELECT id, app_id, name, aliases, x, y, w, h, image_url, note, component_uid "
            "FROM app_icon_targets"
        )
    except sqlite3.OperationalError:
        rows = []
    for r in rows:
        aliases = r["aliases"]
        if isinstance(aliases, str):
            try:
                aliases = json.loads(aliases)
            except json.JSONDecodeError:
                aliases = [aliases] if aliases.strip() else []
        if not isinstance(aliases, list):
            aliases = []
        aid = str(r["app_id"] or "")
        icons.setdefault(aid, []).append({
            "id": r["id"],
            "name": r["name"] or "",
            "x": int(r["x"] or 0),
            "y": int(r["y"] or 0),
            "w": int(r["w"] or 0),
            "h": int(r["h"] or 0),
            "image_url": r["image_url"] or "",
            "aliases": [str(a) for a in aliases if str(a).strip()][:24],
            "note": (r["note"] or "")[:200],
            "component_uid": r["component_uid"] or "",
        })
    con.close()
    return projects, apps, icons


def load_knowledge(src: Path) -> tuple[list[dict], dict]:
    by_id: dict[str, dict] = {}
    jobs = {"capture_enabled": True, "review_enabled": True}

    yaml_dir = src / "packs" / "learned" / "knowledge" / "entries"
    if yaml_dir.is_dir():
        try:
            import yaml  # type: ignore
        except ImportError:
            yaml = None
        if yaml is not None:
            for path in sorted(yaml_dir.glob("*.yaml")) + sorted(yaml_dir.glob("*.yml")):
                try:
                    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if not isinstance(raw, dict):
                    continue
                kid = str(raw.get("id") or path.stem).strip()
                if not kid:
                    continue
                raw["id"] = kid
                by_id[kid] = raw

    cfg = src / "config.json"
    if cfg.is_file():
        try:
            root = json.loads(cfg.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            root = {}
        testing = root.get("testing") if isinstance(root, dict) else {}
        if not isinstance(testing, dict):
            testing = {}
        for raw in testing.get("knowledge") or []:
            if not isinstance(raw, dict):
                continue
            kid = str(raw.get("id") or "").strip()
            if kid and kid not in by_id:
                by_id[kid] = raw
        kj = testing.get("knowledge_jobs")
        if isinstance(kj, dict):
            jobs = {
                "capture_enabled": kj.get("capture_enabled", True) is not False,
                "review_enabled": kj.get("review_enabled", True) is not False,
            }
    return list(by_id.values()), jobs


def _stamp_owner(row: dict, owner: dict) -> dict:
    """导入 MiniOrange 数据没有创建人：回填 Nexus 种子账号 `admin`。"""
    if not isinstance(row, dict):
        return row
    if str(row.get("created_by") or "").strip():
        if not str(row.get("created_by_name") or "").strip():
            row["created_by_name"] = owner.get("created_by_name") or "管理员"
        return row
    row["created_by"] = owner.get("created_by") or ""
    row["created_by_name"] = owner.get("created_by_name") or "管理员"
    return row


def _import_owner() -> dict:
    from mino_nexus.auth_store import seed_admin_user

    admin = seed_admin_user()
    return {
        "created_by": str(admin.get("user_id") or ""),
        "created_by_name": str(admin.get("name") or admin.get("username") or "管理员"),
    }


def merge_projects(dest: Path, projects: list[dict], apps: list[dict], *, force: bool) -> dict:
    path = dest / "projects.json"
    cur = {"projects": [], "apps": []}
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                cur["projects"] = list(raw.get("projects") or [])
                cur["apps"] = list(raw.get("apps") or [])
        except json.JSONDecodeError:
            pass
    p_ids = {str(p.get("id")) for p in cur["projects"] if isinstance(p, dict)}
    a_ids = {str(a.get("id")) for a in cur["apps"] if isinstance(a, dict)}
    added_p = added_a = replaced = 0
    owner = _import_owner()
    for p in projects:
        pid = str(p.get("id") or "")
        if pid in p_ids:
            if force:
                cur["projects"] = [x for x in cur["projects"] if str(x.get("id")) != pid]
                cur["projects"].append(p)
                replaced += 1
            continue
        cur["projects"].append(_stamp_owner(p, owner))
        p_ids.add(pid)
        added_p += 1
    for a in apps:
        aid = str(a.get("id") or "")
        if aid in a_ids:
            if force:
                cur["apps"] = [x for x in cur["apps"] if str(x.get("id")) != aid]
                cur["apps"].append(a)
                replaced += 1
            continue
        cur["apps"].append(_stamp_owner(a, owner))
        a_ids.add(aid)
        added_a += 1
    for p in cur["projects"]:
        if isinstance(p, dict):
            _stamp_owner(p, owner)
    for a in cur["apps"]:
        if isinstance(a, dict):
            _stamp_owner(a, owner)
    return {"doc": cur, "added_projects": added_p, "added_apps": added_a, "replaced": replaced}


def merge_icons(dest: Path, icons: dict[str, list], *, force: bool) -> dict:
    path = dest / "icon_targets.json"
    cur: dict = {}
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                cur = raw
        except json.JSONDecodeError:
            pass
    added = 0
    for aid, rows in icons.items():
        if cur.get(aid) and not force:
            continue
        existing_ids = {str(x.get("id")) for x in (cur.get(aid) or []) if isinstance(x, dict)}
        bucket = list(cur.get(aid) or []) if isinstance(cur.get(aid), list) else []
        for row in rows:
            if str(row.get("id")) in existing_ids and not force:
                continue
            if force:
                bucket = [x for x in bucket if str(x.get("id")) != str(row.get("id"))]
            bucket.append(row)
            existing_ids.add(str(row.get("id")))
            added += 1
        cur[aid] = bucket
    return {"doc": cur, "added": added}


def merge_knowledge(dest: Path, items: list[dict], jobs: dict, *, force: bool) -> dict:
    path = dest / "settings.json"
    cur: dict = {}
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                cur = raw
        except json.JSONDecodeError:
            pass
    existing = [x for x in (cur.get("knowledge") or []) if isinstance(x, dict)]
    have = {str(x.get("id")) for x in existing}
    added = 0
    for raw in items:
        kid = str(raw.get("id") or "").strip()
        if not kid:
            continue
        if kid in have:
            if not force:
                continue
            existing = [x for x in existing if str(x.get("id")) != kid]
        existing.append(raw)
        have.add(kid)
        added += 1
    cur["knowledge"] = existing
    # 沉淀/机审开关跟登录用户走，不写进共享 settings.json
    cur.pop("knowledge_jobs", None)
    return {"doc": cur, "added": added, "total": len(existing)}


def _write(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and str(row.get("id") or "").strip():
            rows.append(row)
    return rows


def load_integrations(src: Path) -> dict:
    cfg = src / "config.json"
    if not cfg.is_file():
        return {"figma": {}, "integrations": {}, "robots": []}
    try:
        root = json.loads(cfg.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"figma": {}, "integrations": {}, "robots": []}
    if not isinstance(root, dict):
        return {"figma": {}, "integrations": {}, "robots": []}
    testing = root.get("testing") if isinstance(root.get("testing"), dict) else {}
    figma = testing.get("figma") if isinstance(testing.get("figma"), dict) else {}
    integrations = testing.get("integrations") if isinstance(testing.get("integrations"), dict) else {}
    robots = []
    testing_robots = testing.get("robots") if isinstance(testing.get("robots"), dict) else {}
    items = testing_robots.get("items")
    if isinstance(items, list):
        robots = [dict(x) for x in items if isinstance(x, dict)]
    if not robots:
        feishu = root.get("feishu") if isinstance(root.get("feishu"), dict) else {}
        for bot in feishu.get("bots") or []:
            if not isinstance(bot, dict):
                continue
            robots.append({
                "id": str(bot.get("id") or "").strip() or None,
                "platform": "lark",
                "name": str(bot.get("name") or "飞书机器人").strip(),
                "credentials": {
                    "app_id": str(bot.get("app_id") or "").strip(),
                    "app_secret": str(bot.get("app_secret") or "").strip(),
                },
            })
    return {"figma": figma, "integrations": integrations, "robots": robots}


def merge_integrations(settings_doc: dict, payload: dict, *, force: bool) -> dict:
    added = {"figma": 0, "integrations": 0, "robots": 0}
    figma = payload.get("figma") if isinstance(payload.get("figma"), dict) else {}
    cur_figma = settings_doc.get("figma") if isinstance(settings_doc.get("figma"), dict) else {}
    if figma and (force or not str(cur_figma.get("access_token") or "").strip()):
        nxt = dict(cur_figma)
        if str(figma.get("access_token") or "").strip():
            nxt["access_token"] = str(figma["access_token"]).strip()
            added["figma"] = 1
        if "default_file_url" in figma:
            nxt["default_file_url"] = str(figma.get("default_file_url") or "").strip()
        settings_doc["figma"] = nxt

    integ = settings_doc.get("integrations") if isinstance(settings_doc.get("integrations"), dict) else {}
    incoming = payload.get("integrations") if isinstance(payload.get("integrations"), dict) else {}
    for pid, cfg in incoming.items():
        if not isinstance(cfg, dict):
            continue
        if integ.get(pid) and not force:
            continue
        integ[str(pid)] = dict(cfg)
        added["integrations"] += 1
    settings_doc["integrations"] = integ

    robots = settings_doc.get("robots") if isinstance(settings_doc.get("robots"), dict) else {}
    items = [dict(x) for x in (robots.get("items") or []) if isinstance(x, dict)]
    have = {str(x.get("id")) for x in items if x.get("id")}
    for raw in payload.get("robots") or []:
        if not isinstance(raw, dict):
            continue
        rid = str(raw.get("id") or "").strip()
        if not rid:
            continue
        if rid in have and not force:
            continue
        if rid in have:
            items = [x for x in items if str(x.get("id")) != rid]
        items.append(raw)
        have.add(rid)
        added["robots"] += 1
    settings_doc["robots"] = {"items": items}
    return {"doc": settings_doc, **added}


def merge_dispatch(src: Path, dest: Path, *, force: bool) -> dict:
    src_file = src / "data" / "dispatch" / "calls.jsonl"
    dest_file = dest / "data" / "dispatch" / "calls.jsonl"
    src_rows = _read_jsonl(src_file)
    dest_rows = [] if force else _read_jsonl(dest_file)
    by_id = {str(r.get("id")): r for r in dest_rows}
    added = 0
    for row in src_rows:
        rid = str(row.get("id"))
        if rid in by_id and not force:
            continue
        by_id[rid] = row
        added += 1
    merged = sorted(by_id.values(), key=lambda r: str(r.get("at") or ""))
    media_src = src / "uploads" / "dispatch"
    media_dest = dest / "uploads" / "dispatch"
    media_added = 0
    media_total = 0
    if media_src.is_dir():
        for path in media_src.iterdir():
            if not path.is_file():
                continue
            media_total += 1
            target = media_dest / path.name
            if target.exists() and not force:
                continue
            media_added += 1
    return {
        "rows": merged,
        "added": added,
        "total": len(merged),
        "src": len(src_rows),
        "media_added": media_added,
        "media_total": media_total,
        "src_file": src_file,
        "dest_file": dest_file,
        "media_src": media_src,
        "media_dest": media_dest,
    }


def copy_learned_packs(src: Path, dest: Path, *, force: bool) -> dict:
    src_dir = src / "packs" / "learned"
    dest_dir = dest / "packs" / "learned"
    copied = 0
    if not src_dir.is_dir():
        return {"copied": 0, "src": str(src_dir), "dest": str(dest_dir)}
    for path in src_dir.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(src_dir)
        target = dest_dir / rel
        if target.exists() and not force:
            continue
        copied += 1
    return {"copied": copied, "src": str(src_dir), "dest": str(dest_dir)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Import MiniOrangeServer project/case/knowledge/dispatch/plugins into MinoNexus")
    ap.add_argument("--source", default="", help="MiniOrangeServer data dir")
    ap.add_argument("--dest", default="", help="Nexus data dir (default MINO_NEXUS_DATA_DIR / ~/.mino-nexus)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="overwrite same ids")
    args = ap.parse_args(argv)

    src = Path(args.source).expanduser() if args.source else default_source()
    dest = Path(args.dest).expanduser() if args.dest else data_dir()
    db = src / "data" / "autobots.db"
    if not db.is_file():
        print(f"找不到上游库：{db}", file=sys.stderr)
        return 2

    projects, apps, icons = load_sqlite(db)
    knowledge, jobs = load_knowledge(src)
    integrations = load_integrations(src)
    cases = sum(_count_cases(a.get("env") or {}) for a in apps)

    print(f"source  {src}")
    print(f"dest    {dest}")
    print(f"read    projects={len(projects)} apps={len(apps)} cases={cases} "
          f"icons={sum(len(v) for v in icons.values())} knowledge={len(knowledge)} "
          f"robots={len(integrations['robots'])} integrations={list(integrations['integrations'].keys())}")

    pmerge = merge_projects(dest, projects, apps, force=args.force)
    imerge = merge_icons(dest, icons, force=args.force)
    kmerge = merge_knowledge(dest, knowledge, jobs, force=args.force)
    smerge = merge_integrations(kmerge["doc"], integrations, force=args.force)
    dmerge = merge_dispatch(src, dest, force=args.force)
    pcopy = copy_learned_packs(src, dest, force=args.force)
    print(f"merge   +projects={pmerge['added_projects']} +apps={pmerge['added_apps']} "
          f"+icons={imerge['added']} +knowledge={kmerge['added']} "
          f"+figma={smerge['figma']} +integrations={smerge['integrations']} +robots={smerge['robots']}")
    print(f"dispatch src={dmerge['src']} +rows={dmerge['added']} total={dmerge['total']} "
          f"+media={dmerge['media_added']}/{dmerge['media_total']}")
    print(f"packs   +files={pcopy['copied']} -> {pcopy['dest']}")

    if args.dry_run:
        print("dry-run，没有写盘")
        return 0

    dest.mkdir(parents=True, exist_ok=True)
    _write(dest / "projects.json", pmerge["doc"])
    _write(dest / "icon_targets.json", imerge["doc"])
    _write(dest / "settings.json", smerge["doc"])

    dmerge["dest_file"].parent.mkdir(parents=True, exist_ok=True)
    tmp = dmerge["dest_file"].with_suffix(".jsonl.tmp")
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in dmerge["rows"]), encoding="utf-8")
    tmp.replace(dmerge["dest_file"])
    if dmerge["media_src"].is_dir():
        dmerge["media_dest"].mkdir(parents=True, exist_ok=True)
        for path in dmerge["media_src"].iterdir():
            if not path.is_file():
                continue
            target = dmerge["media_dest"] / path.name
            if target.exists() and not args.force:
                continue
            shutil.copy2(path, target)

    src_packs = src / "packs" / "learned"
    dest_packs = dest / "packs" / "learned"
    if src_packs.is_dir():
        for path in src_packs.rglob("*"):
            if not path.is_file():
                continue
            target = dest_packs / path.relative_to(src_packs)
            if target.exists() and not args.force:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)

    print("wrote   projects.json icon_targets.json settings.json data/dispatch/calls.jsonl packs/learned")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

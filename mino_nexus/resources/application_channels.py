"""Project applications own runtime channels through explicit app_id bindings."""
from copy import deepcopy


def app_platforms(app):
    raw = app.get("platforms") or []
    return raw.split(",") if isinstance(raw, str) else list(raw)


def application_channels(document, apps):
    doc = deepcopy(document)
    channels = doc.setdefault("channels", [])
    presets = {"android": ("package", "com.example.app"), "ios": ("bundle", "com.example.ios"),
               "web": ("base_url", "https://"),
               "mac": ("bundle", "com.example.desktop"), "win": ("path", "完整 EXE 路径或 AUMID")}
    for app in apps:
        aid = str(app["id"])
        for platform in app_platforms(app):
            if platform not in presets:
                continue
            owned = [c for c in channels if c.get("app_id") == aid and c.get("platform") == platform]
            if owned:
                continue
            field, placeholder = presets[platform]
            stem = f"{platform}.app_{aid.replace('-', '_')}"
            cid = stem
            n = 2
            while any(c.get("id") == cid for c in channels):
                cid = f"{stem}_{n}"
                n += 1
            channels.append({"id": cid, "app_id": aid, "kind": "web" if platform == "web" else "app",
                             "platform": platform, "alias": "", "app_identifier": cid.split('.', 1)[1],
                             "third_party": False, "label": app.get("name") or aid,
                             "field": field, "placeholder": placeholder})
            for env in doc.get("environments") or []:
                doc.setdefault("profiles", {}).setdefault(env["key"], {})[cid] = {field: ""}
    return doc


def validate_application_channels(doc, apps):
    by_id = {str(a["id"]): a for a in apps}
    seen = set()
    for ch in doc.get("channels") or []:
        aid = ch.get("app_id")
        if not aid:
            continue
        if aid not in by_id or ch.get("platform") not in app_platforms(by_id[aid]):
            raise ValueError("运行配置必须关联本项目已有应用及其平台")
        key = (aid, ch.get("platform"))
        if key in seen:
            raise ValueError("同一应用平台不能重复配置运行入口")
        seen.add(key)

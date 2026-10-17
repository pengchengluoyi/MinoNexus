"""Local Workbench maintenance bridge; the installed helper owns update state.

Only a host-configured installation is executable. HTTP/MCP callers never supply
paths, commands, download URLs or the local-verification bypass.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading


class WorkbenchUpdateError(RuntimeError):
    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


def identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,100}", value):
        raise WorkbenchUpdateError("无效的安装、版本或操作标识", 422)
    return value


class WorkbenchInstallation:
    def __init__(self, installation_id: str | None = None):
        configured = os.environ.get("MINO_WORKBENCH_INSTALL_ROOT", "")
        if not configured:
            from mino_nexus.core.deployment import Deployment, deployment_root
            nexus_root = deployment_root()
            if nexus_root is not None:
                configured = Deployment(nexus_root).config.get("workbench_install_root", "")
        if not configured:
            raise WorkbenchUpdateError("尚未登记本机 Workbench 安装根；跨主机更新暂不支持")
        if sys.platform not in {"darwin", "linux", "win32"}:
            raise WorkbenchUpdateError("Workbench MCP 更新目前仅支持 macOS 同机安装")
        path = Path(configured).expanduser()
        if not path.is_absolute() or path.resolve(strict=True) != path:
            raise WorkbenchUpdateError("Workbench 安装根必须是已解析的绝对路径")
        self.root = path
        for name in ("config", "launcher", "updates", "releases"):
            folder = self.root / name
            if folder.is_symlink() or not folder.is_dir():
                raise WorkbenchUpdateError("Workbench 安装目录边界无效")
        self.conf = self.read("config/installation.json")
        if self.conf.get("component") != "workbench" or not self.conf.get("installation_id"):
            raise WorkbenchUpdateError("安装目录不是支持更新身份的 Workbench 基线")
        self.installation_id = identifier(self.conf["installation_id"])
        if installation_id is not None and identifier(installation_id) != self.installation_id:
            raise WorkbenchUpdateError("Workbench installation_id 不匹配，拒绝操作其他安装")
        self.executable = self.file("launcher/mino-maintain.exe" if sys.platform == "win32" else "launcher/mino-maintain")
        if not os.access(self.executable, os.X_OK):
            raise WorkbenchUpdateError("Workbench 独立维护入口不可执行")

    def file(self, relative: str) -> Path:
        from mino_nexus.core.deployment import legacy_path
        path = legacy_path(self.root / relative)
        if path.resolve(strict=True) != path or not path.is_file() or path.stat().st_nlink != 1:
            raise WorkbenchUpdateError("Workbench 文件必须位于登记目录内且不是链接")
        return path

    def read(self, relative: str) -> dict:
        value = json.loads(self.file(relative).read_text())
        if not isinstance(value, dict):
            raise WorkbenchUpdateError("Workbench 维护记录格式无效")
        return value

    def info(self) -> dict:
        from mino_nexus.core.platform_runtime import current_release
        release = current_release(self.root)
        if release.parent != self.root / "releases":
            raise WorkbenchUpdateError("Workbench current 指向登记版本以外的位置")
        build = identifier(release.name)
        info = self.read(f"releases/{build}/release.json")
        if info.get("component") != "workbench" or info.get("build_id") != build:
            raise WorkbenchUpdateError("Workbench 当前版本身份不一致")
        return {"installation_id": self.installation_id, "current_build_id": build,
                "runtime_id": info.get("runtime_id"), "dirty": bool(info.get("dirty")),
                "scope": "local-installation", "live_health_verified": False}

    def operation(self, operation_id: str) -> dict:
        row = self.read(f"updates/mino-{identifier(operation_id)}.json")
        if row.get("operation") != operation_id:
            raise WorkbenchUpdateError("持久操作身份不一致")
        return row

    def public(self, row: dict) -> dict:
        # No package paths, command output or complete manifests in MCP results.
        result = {key: row[key] for key in (
            "kind", "state", "before", "restart", "updated_at", "history",
            "api_evidence", "verification", "local_verification", "error", "recovery_error"
        ) if key in row}
        return {**result, "installation_id": self.installation_id,
                "operation_id": row["operation"], "build_id": row["release"]["build_id"]}

    def status(self, operation_id: str) -> dict:
        return self.public(self.operation(operation_id))

    def prepared(self, build_id: str, package: bool = False) -> tuple[Path, Path | None]:
        base = f"updates/prepared/{identifier(build_id)}"
        manifest = self.file(f"{base}/mino-manifest.json")
        if self.read(f"{base}/mino-manifest.json").get("build_id") != build_id:
            raise WorkbenchUpdateError("暂存清单与指定 build_id 不匹配")
        return manifest, self.file(f"{base}/mino-package.zip") if package else None

    def _invoke(self, command: str, arguments: list[str], operation_id: str = "", expected_build: str = "") -> dict:
        def receipt(row):
            if expected_build and (row.get("kind") != command or row["release"]["build_id"] != expected_build
                                   or row.get("local_verification")):
                raise WorkbenchUpdateError("operation_id 已绑定其他参数或本地验收操作")
            return self.public(row)

        process = subprocess.Popen(
            [str(self.executable), command, "--root", str(self.root), *arguments],
            cwd=self.root, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, close_fds=True, start_new_session=True,
        )
        try:
            output, error = process.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            # Never kill admission or create another operation after losing an ACK.
            threading.Thread(target=process.communicate, daemon=True).start()
            result = {"installation_id": self.installation_id, "operation_id": operation_id,
                      "observation": "unknown", "recovery": "先查询原 operation_id，不创建新操作重试"}
            if operation_id:
                try:
                    result["last_confirmed"] = receipt(self.operation(operation_id))
                except FileNotFoundError:
                    pass
            if command == "github-fetch":
                result.update(release_tag=arguments[arguments.index("--tag") + 1],
                              build_id=arguments[arguments.index("--build") + 1],
                              recovery="下载可能仍在进行；稍后用相同 tag/build 再次 prepare，复用已校验文件")
            return result
        if process.returncode:
            # Scheduling may fail AFTER the durable request was accepted.
            if operation_id and command in {"update", "rollback", "recover"}:
                try:
                    return {**receipt(self.operation(operation_id)), "helper_started": False,
                            "recovery": "操作已登记；查询原 operation_id，修复本机助手后恢复原操作"}
                except FileNotFoundError:
                    pass
            # PyInstaller appends a generic crash footer after the useful exception.
            reasons = [line for line in error.splitlines() if re.match(
                r"^(ValueError|RuntimeError|FileNotFoundError|PermissionError|BlockingIOError|OSError):", line)]
            detail = reasons[-1] if reasons else "维护入口执行失败，请检查本机助手诊断"
            raise WorkbenchUpdateError(detail[:500])
        value = json.loads(output)
        return receipt(value) if operation_id else value

    def check(self, build_id: str = "", release_tag: str = "") -> dict:
        if build_id:
            manifest, _ = self.prepared(build_id)
            value = self._invoke("check-update", ["--manifest", str(manifest)])
            return {"installation_id": self.installation_id, "current_build_id": value["current"],
                    "target_build_id": value["target"], "item": value["item"],
                    "package_verified": False, "compatibility_verified": False}
        arguments = ["--tag", identifier(release_tag)] if release_tag else []
        return {"installation_id": self.installation_id, **self._invoke("github-check", arguments)}

    def prepare(self, release_tag: str, build_id: str) -> dict:
        value = self._invoke("github-fetch", ["--tag", identifier(release_tag), "--build", identifier(build_id)])
        return {"installation_id": self.installation_id, **value}

    def request(self, operation_id: str, build_id: str, rollback: bool = False) -> dict:
        identifier(operation_id)
        identifier(build_id)
        try:
            existing = self.operation(operation_id)
        except FileNotFoundError:
            existing = None
        kind = "rollback" if rollback else "update"
        if existing is not None:
            if (existing.get("kind") != kind or existing["release"]["build_id"] != build_id
                    or existing.get("local_verification")):
                raise WorkbenchUpdateError("operation_id 已绑定其他参数或本地验收操作")
            return self.public(existing)
        arguments = ["--operation", operation_id]
        if rollback:
            arguments += ["--build", build_id]
        else:
            manifest, package = self.prepared(build_id, package=True)
            arguments += ["--manifest", str(manifest), "--package", str(package)]
        return self._invoke(kind, arguments, operation_id, expected_build=build_id)

    def resume(self, operation_id: str) -> dict:
        row = self.operation(operation_id)
        if row.get("local_verification"):
            raise WorkbenchUpdateError("MCP 不恢复绕过兼容验收的本地操作，请使用本地维护入口")
        return self._invoke("recover", ["--operation", identifier(operation_id)], operation_id)

    def cancel(self, operation_id: str) -> dict:
        return self._invoke("cancel-update", ["--operation", identifier(operation_id)], operation_id)

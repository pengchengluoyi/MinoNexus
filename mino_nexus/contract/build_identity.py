"""编译产物的身份指纹：版本 + 编译器合同 + 资源目录版本。"""
from __future__ import annotations

COMPILE_CONTRACT_REVISION = 15
CATALOG_VERSION = 13


def build_identity() -> dict[str, str | int]:
    from pathlib import Path

    build_version = (Path(__file__).resolve().parents[1] / "VERSION").read_text().strip()
    return {"nexus_version": build_version,
            "compiler_contract": COMPILE_CONTRACT_REVISION,
            "resource_catalog_version": CATALOG_VERSION}

"""Offline installer upgrade, including the stable launcher, with retained backups.

Unlike unattended updates this is a local maintenance operation. Migration is
rehearsed twice against a stopped snapshot; no compatibility evidence is forged.
After normal startup we never roll the database back automatically.
"""
import os
from pathlib import Path
import shutil
import subprocess
import uuid

from mino_nexus.core.deployment import Deployment, TERMINAL, read_json
from mino_nexus.core import update_engine as engine
from mino_nexus.core.release_package import stage_release


def plain_tree(path):
    path = Path(path).absolute()
    if path.resolve() != path:
        raise ValueError('Installation and data paths must not traverse links')
    for p in [path, *path.rglob('*')]:
        if p.is_symlink() or getattr(p, 'is_junction', lambda: False)() or (p.is_file() and p.stat().st_nlink != 1):
            raise ValueError('Linked files require a manual backup strategy: ' + str(p))


def upgrade(root, manifest, package):
    raise RuntimeError("upgrade_and_rollback_removed")

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
    root = root.expanduser().absolute()
    if root.resolve() != root:
        raise ValueError('Installation path must not traverse links')
    for name in ('config', 'updates', 'releases', 'launcher'):
        p = root / name
        if not p.is_dir() or p.resolve() != p or getattr(p, 'is_junction', lambda: False)():
            raise ValueError('Installation directory boundary is invalid: ' + str(p))
    d = Deployment(root)
    with d.lock('helper', blocking=False):
        before = d.current_build()
        old = read_json(d.release(before) / 'release.json')
        target_doc = read_json(manifest)
        target = target_doc['build_id']
        if old['version'] not in {'0.1.43', '0.1.44', '0.1.45', '0.1.46', '0.1.50'}:
            raise ValueError('Installer upgrade supports Nexus 0.1.43 and later verified baselines only')
        if tuple(map(int, target_doc['version'].split('.'))) < tuple(map(int, old['version'].split('.'))):
            raise ValueError('Installer cannot downgrade an existing database')
        with d.lock():
            previous = d.state()
            unfinished = previous and previous.get('state') not in TERMINAL
            resumable = unfinished and previous.get('installer_upgrade') and previous.get('target_build') == target and previous.get('phase') in {'starting', 'probing'} and before == target
            if unfinished and not resumable:
                raise ValueError('Unfinished maintenance preserved; inspect operation ' + str(previous.get('operation_id')))
        if resumable:
            engine.control_service(d, 'start')
            engine.await_health(d, target)
            d.save(previous, state='succeeded', phase='complete')
            return {'state': 'succeeded', 'build_id': target, 'resumed': True}
        release = stage_release(d, target_doc, package)
        if target == before:
            engine.control_service(d, 'start')
            engine.await_health(d, before)
            return {'state': 'unchanged', 'build_id': before}
        data = Path(d.config['data_dir']).absolute()
        plain_tree(data)
        plain_tree(root / 'launcher')
        plain_tree(root / 'config')
        operation = 'installer-' + uuid.uuid4().hex
        journal = root / 'updates' / operation
        journal.mkdir()
        shutil.copytree(root / 'config', journal / 'config')
        shutil.copytree(release / 'launcher', journal / 'launcher-new')
        row = dict(operation_id=operation, previous_build=before, target_build=target,
                   state='installing', phase='stopping', writes_reopened=False,
                   backup='', error='', installer_upgrade=True, launcher_backup=str(journal / 'launcher-old'))
        # Admission closes atomically with the last activity/writer check.
        with d.lock():
            with d.lock('writers', blocking=False):
                if any(engine.activity(d).values()):
                    raise ValueError('Nexus has active work; finish it and rerun the installer')
                row = d.save(row)
        guard = None
        moved_launcher = False
        migration_started = False
        try:
            engine.control_service(d, 'stop')
            guard = engine._wait_exit(d)
            row = d.save(row, phase='backing_up')
            snapshot = engine.backup(d, row)
            row = d.save(row, backup=str(snapshot), phase='rehearsing')
            rehearsal = journal / 'migration-preview'
            shutil.copytree(snapshot, rehearsal)
            env = engine.release_env(d, target)
            env.pop('MINO_NEXUS_INSTALL_ROOT', None)
            env.pop('MINO_WORKBENCH_INSTALL_ROOT', None)
            # Only the target's normal offline migration may touch the data.
            def migrate(path):
                with (journal / 'migration.log').open('ab') as log:
                    subprocess.run(engine.release_command(d, target, 'maintenance', 'migrate', '--data-dir', str(path)),
                                   env={**env, 'MINO_NEXUS_DATA_DIR': str(path)}, stdout=log, stderr=log, check=True)
            migrate(rehearsal)
            migrate(rehearsal)
            row = d.save(row, phase='migrating')
            migration_started = True
            migrate(data)
            row = d.save(row, phase='switching')
            os.rename(root / 'launcher', journal / 'launcher-old')
            moved_launcher = True
            os.rename(journal / 'launcher-new', root / 'launcher')
            d.switch(target)
            row = d.save(row, state='restarting', phase='starting', writes_reopened=True)
            guard.__exit__(None, None, None); guard = None
            engine.control_service(d, 'start')
            row = d.save(row, state='verifying', phase='probing')
            engine.await_health(d, target)
            row = d.save(row, state='succeeded', phase='complete')
            return {'state': row['state'], 'build_id': target, 'backup': str(snapshot), 'launcher_backup': str(journal / 'launcher-old')}
        except Exception as exc:
            row = d.save(row, error=str(exc))
            if row['writes_reopened']:
                # Startup may already have accepted writes. Preserve all new data.
                d.save(row, state='needs_operator')
                raise RuntimeError('Startup could not be verified; data and backups preserved. Inspect ' + str(journal)) from exc
            try:
                if guard is None:
                    engine.control_service(d, 'stop')
                    guard = engine._wait_exit(d)
                if migration_started:
                    engine.restore_prestart(d, row)
                if moved_launcher:
                    if (root / 'launcher').exists():
                        os.rename(root / 'launcher', journal / 'launcher-failed')
                    os.rename(journal / 'launcher-old', root / 'launcher')
                d.switch(before)
                row = d.save(row, phase='rolling_back')
                guard.__exit__(None, None, None); guard = None
                engine.control_service(d, 'start')
                engine.await_health(d, before)
                d.save(row, state='rolled_back', phase='complete')
            except Exception as recovery:
                d.save(row, state='needs_operator', recovery_error=str(recovery))
            raise RuntimeError('Upgrade failed; preserved operation ' + operation + ': ' + str(exc)) from exc
        finally:
            if guard:
                guard.__exit__(None, None, None)

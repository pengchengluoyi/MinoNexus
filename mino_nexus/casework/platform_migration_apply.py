"""Reviewed platform migration transactions, separate from normal startup.

Plans contain field paths and platform/identity values only. Historical runs,
sessions, frozen publications, credentials and lock keys are immutable here.
Production activation requires consumer acceptance and a quiescent database.
"""
from __future__ import annotations

from contextlib import closing
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3

from mino_nexus.core.platform_contract import target_platform, platform_set, host_os
from mino_nexus.casework.platform_migration import _run_unresolved, _ACTIVE_WORK

SCHEMA = 'platform-migration.v1'
CONSUMERS = ('nexus', 'scout', 'workbench', 'import', 'recovery', 'references')
_COLUMNS = {
    'apps': {'platforms', 'env'}, 'projects': {'env'},
    'project_cases': {'platform', 'extra'}, 'm_device': {'sn', 'platform', 'extra'},
    'nodes': {'extra'}, 'device_app_sessions': {'sn', 'platform', 'extra'},
    'catalog_entries': {'platforms_json', 'payload_json'},
    'resource_transition_rules': {'platform', 'effects_json'},
    'nav_flow_block_catalog': {'steps_json'}, 'ui_controls': {'locator_spec'},
}
_SCALAR = {'platform', 'platform_scope', 'sn', 'device_sn', 'os'}
_SN_MAPS = {'device_targets', 'platforms_by_sn'}
_FORBIDDEN = {'v5_publication', 'publication', 'runtime_context', 'history', 'review_history',
              'source', 'source_snapshot'}


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def _digest(value):
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _cell_hash(value, column):
    if column in {'env', 'extra', 'locator_spec'} or column.endswith('_json'):
        value = json.loads(value) if isinstance(value, str) else value
    return _digest(value)


def _path(path):
    if not isinstance(path, list) or len(path) > 40:
        raise ValueError('migration_path_invalid')
    for part in path:
        if type(part) not in (str, int) or isinstance(part, int) and part < 0:
            raise ValueError('migration_path_invalid')
        if isinstance(part, str) and (part in _FORBIDDEN or any(x in part.lower() for x in ('password', 'secret', 'token', 'credential'))):
            raise ValueError('migration_protected_path')


def _validate_value(column, path, value, operation):
    key = path[-1] if path else column
    if operation == 'rename_key':
        if len(path) < 2 or path[-2] not in _SN_MAPS:
            raise ValueError('migration_identity_map_forbidden')
        old = re.fullmatch(r'desktop:([A-Za-z0-9_.-]+):console', str(key))
        if not old or not isinstance(value, str) or value not in {f'mac:{old[1]}:console', f'win:{old[1]}:console'}:
            raise ValueError('migration_identity_mapping_invalid')
        return
    if operation == 'remove':
        if key != 'os' and key != 'platform':
            raise ValueError('migration_remove_forbidden')
        return
    if len(path) >= 2 and path[-2] == 'platforms_by_sn':
        target_platform(value)
    elif key == 'platform':
        target_platform(value)
    elif key in ('platforms', 'platforms_json'):
        if column == 'platforms' and not path and isinstance(value, str):
            platform_set(value.split(',') if value else [])
        else:
            platform_set(value)
    elif key == 'platform_scope':
        if value not in ('all', 'explicit'):
            raise ValueError('migration_scope_invalid')
    elif key == 'os' and len(path) >= 2 and path[-2] == 'host':
        host_os(value)
    elif key == 'host' and isinstance(value, dict) and set(value) == {'os'}:
        host_os(value['os'])
    elif key in ('sn', 'device_sn'):
        if not isinstance(value, str) or not value or len(value) > 256:
            raise ValueError('migration_identity_invalid')
    elif key == 'sns':
        if not isinstance(value, list) or any(not isinstance(s, str) or not s for s in value):
            raise ValueError('migration_identity_invalid')
    else:
        raise ValueError('migration_field_not_allowed')


def _decode(raw, column, path):
    if path or column.endswith('_json'):
        return json.loads(raw) if isinstance(raw, str) else deepcopy(raw)
    return raw


def _at(document, path):
    value = document
    for part in path:
        value = value[part]
    return value


def _patch(raw, column, edits, *, reverse=False):
    document = _decode(raw, column, edits[0]['path'])
    for edit in reversed(edits) if reverse else edits:
        path = edit['path']
        operation = edit['operation']
        before, after = edit.get('before'), edit.get('after')
        if operation == 'rename_key':
            parent = _at(document, path[:-1])
            old_key, new_key = (after, before) if reverse else (before, after)
            if not isinstance(parent, dict) or old_key not in parent or new_key in parent:
                raise ValueError('migration_identity_collision_or_missing')
            # Move the value intact; plans never export the referenced document.
            parent[new_key] = parent.pop(old_key)
            continue
        if reverse:
            operation = {'set':'set', 'add':'remove', 'remove':'add'}[operation]
            before, after = after, before
        if not path:
            if operation != 'set' or document != before:
                raise ValueError('migration_value_changed')
            document = after
            continue
        parent, key = _at(document, path[:-1]), path[-1]
        if operation == 'add':
            if not isinstance(parent, dict) or key in parent:
                raise ValueError('migration_value_changed')
            parent[key] = after
        else:
            if parent[key] != before:
                raise ValueError('migration_value_changed')
            if operation == 'remove':
                if not isinstance(parent, dict):
                    raise ValueError('migration_remove_forbidden')
                if not reverse and key == 'platform' and parent.get('kind') != 'server':
                    raise ValueError('migration_remove_platform_forbidden')
                if not reverse and key == 'os' and not isinstance(parent.get('host'), dict):
                    raise ValueError('migration_host_required')
                del parent[key]
            else:
                parent[key] = after
    if edits[0]['path'] or column.endswith('_json'):
        return _canonical(document)
    return document


def _tables(db):
    return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _quiescent(db):
    tables = _tables(db)
    for table, active in _ACTIVE_WORK.items():
        if table in tables:
            placeholders = ','.join('?' for _ in active)
            if db.execute(f'SELECT 1 FROM "{table}" WHERE status IN ({placeholders}) LIMIT 1', tuple(active)).fetchone():
                raise ValueError('migration_active_compile' if table == 'case_compile_jobs' else 'migration_active_task')
    # Conservatively refuse rather than recycle leases/isolation during migration.
    for table in ('device_reservations', 'device_resource_leases'):
        if table in tables and db.execute(f'SELECT 1 FROM "{table}" LIMIT 1').fetchone():
            raise ValueError('migration_resources_not_quiescent')
    if 'app_regression_runs' in tables:
        for status, raw in db.execute('SELECT status,payload FROM app_regression_runs'):
            if status in ('running', 'queued'):
                raise ValueError('migration_active_run')
            if _run_unresolved(json.loads(raw) if isinstance(raw, str) else raw):
                raise ValueError('migration_unknown_write')
    if 'pool_accounts' in tables:
        for (raw,) in db.execute('SELECT lease FROM pool_accounts'):
            lease = json.loads(raw) if isinstance(raw, str) else raw
            if lease and lease.get('run_id'):
                raise ValueError('migration_account_leased')


def _source(path):
    source = Path(path).absolute()
    if source.resolve(strict=True) != source or not source.is_file() or source.stat().st_nlink != 1:
        raise ValueError('migration_source_alias_forbidden')
    return source


def _group_edits(edits):
    if not isinstance(edits, list) or not edits or len(edits) > 100000:
        raise ValueError('migration_edits_required')
    groups = {}
    for edit in edits:
        if not isinstance(edit, dict) or set(edit) - {'table','rowid','column','path','operation','before','after'}:
            raise ValueError('migration_edit_invalid')
        table, column = edit.get('table'), edit.get('column')
        if table not in _COLUMNS or column not in _COLUMNS[table] or type(edit.get('rowid')) is not int:
            raise ValueError('migration_column_forbidden')
        path, op = edit.get('path'), edit.get('operation')
        _path(path)
        if op not in ('set','add','remove','rename_key') or not path and op != 'set':
            raise ValueError('migration_operation_invalid')
        _validate_value(column, path, edit.get('after'), op)
        if op == 'rename_key' and edit.get('before') != path[-1]:
            raise ValueError('migration_identity_mapping_invalid')
        # Only value domains explicitly needed for migration are exported.
        if op != 'add':
            before = edit.get('before')
            if not isinstance(before, (str, list)) or isinstance(before, list) and any(not isinstance(x,str) for x in before):
                raise ValueError('migration_before_invalid')
        key = (table, edit['rowid'], column)
        previous = groups.setdefault(key, [])
        if any(e['path'] == path or e['path'][:len(path)] == path or path[:len(e['path'])] == e['path'] for e in previous):
            raise ValueError('migration_overlapping_edits')
        previous.append(deepcopy(edit))
    return groups


def prepare(database, edits, *, reviewer, acceptance):
    """Create CAS guards for an explicit reviewed inventory, never infer Mobile.

Each edit: table, rowid, column, path, operation=set|add|remove|rename_key,
before/after. rename_key is restricted to reviewed desktop SN map keys;
before/after contain only the keys, not the referenced document. Put a key
rename before edits beneath its new key; rollback reverses that order.
Acceptance contains versioned evidence refs, not boolean claims of completion.
No full JSON cells or credential-bearing backups appear in the returned plan.
"""
    source = _source(database)
    if not isinstance(reviewer, str) or not reviewer.strip() or not isinstance(acceptance, dict):
        raise ValueError('migration_review_required')
    if set(acceptance) != set(CONSUMERS) or any(not isinstance(v, str) or not v.strip() for v in acceptance.values()):
        raise ValueError('migration_consumer_acceptance_required')
    groups = _group_edits(edits)
    cells = []
    with closing(sqlite3.connect(source.as_uri()+'?mode=ro', uri=True)) as db:
        db.execute('BEGIN')
        _quiescent(db)
        for (table, rowid, column), patches in sorted(groups.items()):
            row = db.execute(f'SELECT "{column}" FROM "{table}" WHERE rowid=?', (rowid,)).fetchone()
            if row is None:
                raise ValueError('migration_row_missing')
            after = _patch(row[0], column, patches)
            cells.append({'table':table,'rowid':rowid,'column':column,'before_hash':_cell_hash(row[0],column),
                          'after_hash':_cell_hash(after,column),'edits':patches})
        db.rollback()
    body = {'schema':SCHEMA,'database':str(source),'reviewer':reviewer,'acceptance':acceptance,'cells':cells}
    return {**body, 'plan_id':_digest(body)}


def _backup(source, destination):
    dest = Path(destination).absolute()
    if dest.parent.resolve(strict=True) != dest.parent or dest.exists() or dest.is_symlink() or dest == source:
        raise ValueError('migration_backup_path_invalid')
    fd = os.open(dest, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    with closing(sqlite3.connect(source.as_uri()+'?mode=ro', uri=True)) as reader, closing(sqlite3.connect(dest)) as copy:
        reader.backup(copy)
        if copy.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('migration_backup_invalid')
    return str(dest)


def apply(database, plan, *, backup_path, reverse=False):
    """Atomic CAS apply/rollback. Rollback only touches reviewed cells, not new data.

Caller must stop writers/registrations for the whole identity cutover. SQLite's
write transaction makes the backup and the CAS changes consistent; a later
writer still requires the synchronized consumers recorded in acceptance.
"""
    source = _source(database)
    if not isinstance(plan, dict):
        raise ValueError('migration_plan_invalid')
    body = {k:v for k,v in plan.items() if k != 'plan_id'}
    if plan.get('schema') != SCHEMA or plan.get('database') != str(source) or _digest(body) != plan.get('plan_id'):
        raise ValueError('migration_plan_invalid')
    # Revalidate editable plan structure; a digest alone is not authorization.
    edits = [e for c in plan.get('cells', []) for e in c.get('edits', [])]
    if (not isinstance(plan.get('reviewer'), str) or not plan['reviewer'].strip()
            or not isinstance(plan.get('acceptance'), dict) or set(plan['acceptance']) != set(CONSUMERS)
            or any(not isinstance(v, str) or not v.strip() for v in plan['acceptance'].values())):
        raise ValueError('migration_consumer_acceptance_required')
    groups = _group_edits(edits)
    if len(groups) != len(plan.get('cells', [])):
        raise ValueError('migration_plan_invalid')
    for cell in plan['cells']:
        key = (cell.get('table'), cell.get('rowid'), cell.get('column'))
        if groups.get(key) != cell.get('edits'):
            raise ValueError('migration_plan_invalid')
    with closing(sqlite3.connect(source, timeout=5)) as db:
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('BEGIN IMMEDIATE')
        db.execute('PRAGMA defer_foreign_keys=ON')
        try:
            _quiescent(db)
            ledger = 'platform_migration_receipts'
            exists = ledger in _tables(db)
            previous = db.execute('SELECT state FROM platform_migration_receipts WHERE plan_id=?', (plan['plan_id'],)).fetchone() if exists else None
            desired = 'reverted' if reverse else 'applied'
            if previous and previous[0] == desired:
                for cell in plan['cells']:
                    table, column = cell['table'], cell['column']
                    row = db.execute(f'SELECT "{column}" FROM "{table}" WHERE rowid=?', (cell['rowid'],)).fetchone()
                    expected = cell['before_hash'] if reverse else cell['after_hash']
                    if row is None or _cell_hash(row[0],column) != expected:
                        raise ValueError('migration_receipt_drift')
                db.rollback()
                return {'plan_id':plan['plan_id'],'status':'unchanged','state':desired}
            if reverse and (not previous or previous[0] != 'applied'):
                raise ValueError('migration_not_applied')
            updates = []
            for cell in plan['cells']:
                table, column, rowid = cell['table'], cell['column'], cell['rowid']
                if table not in _COLUMNS or column not in _COLUMNS[table] or type(rowid) is not int:
                    raise ValueError('migration_column_forbidden')
                row = db.execute(f'SELECT "{column}" FROM "{table}" WHERE rowid=?', (rowid,)).fetchone()
                expected = cell['after_hash'] if reverse else cell['before_hash']
                if row is None or _cell_hash(row[0],column) != expected:
                    raise ValueError('migration_stale_plan')
                value = _patch(row[0], column, cell['edits'], reverse=reverse)
                resulting_hash = cell['before_hash'] if reverse else cell['after_hash']
                if _cell_hash(value, column) != resulting_hash:
                    raise ValueError('migration_result_mismatch')
                # Reverse JSON serialization may differ; compare semantic edits,
                # never replace the complete old document from a stale backup.
                updates.append((table,column,rowid,value))
            backup = _backup(source, backup_path)
            for table,column,rowid,value in updates:
                db.execute(f'UPDATE "{table}" SET "{column}"=? WHERE rowid=?', (value,rowid))
            if db.execute('PRAGMA foreign_key_check').fetchone():
                raise ValueError('migration_reference_integrity_failed')
            db.execute('CREATE TABLE IF NOT EXISTS platform_migration_receipts '
                       '(plan_id TEXT PRIMARY KEY,state TEXT NOT NULL,reviewer TEXT NOT NULL,backup_path TEXT NOT NULL)')
            db.execute('INSERT INTO platform_migration_receipts VALUES (?,?,?,?) ON CONFLICT(plan_id) DO UPDATE SET '
                       'state=excluded.state,backup_path=excluded.backup_path', (plan['plan_id'],desired,plan['reviewer'],backup))
            db.commit()
            return {'plan_id':plan['plan_id'],'status':desired,'changed_cells':len(updates)}
        except BaseException:
            db.rollback()
            raise


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description='Apply explicit reviewed platform edits; never run at startup')
    parser.add_argument('database')
    commands = parser.add_subparsers(dest='command', required=True)
    planning = commands.add_parser('prepare')
    planning.add_argument('review', help='JSON with edits, reviewer and acceptance evidence references')
    planning.add_argument('output', help='New plan file; existing files are never overwritten')
    for command in ('apply', 'rollback'):
        execution = commands.add_parser(command)
        execution.add_argument('plan')
        execution.add_argument('--backup', required=True, help='New local SQLite backup file')
    args = parser.parse_args(argv)
    if args.command == 'prepare':
        review = json.loads(Path(args.review).read_text())
        plan = prepare(args.database, review['edits'], reviewer=review['reviewer'], acceptance=review['acceptance'])
        with open(args.output, 'x', encoding='utf-8') as out:
            out.write(_canonical(plan))
        print(_canonical({'plan_id':plan['plan_id'],'changed_cells':len(plan['cells']),'status':'prepared'}))
    else:
        plan = json.loads(Path(args.plan).read_text())
        print(_canonical(apply(args.database, plan, backup_path=args.backup, reverse=args.command=='rollback')))


if __name__ == '__main__':
    main()

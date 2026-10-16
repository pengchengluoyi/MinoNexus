"""Read-only application issue rollup; diagnostics are not product failures."""
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone

from fastapi import HTTPException

from mino_nexus.core.database import session_scope
from mino_nexus.core.execution_user import require_user_id
from mino_nexus.models.project import App
from mino_nexus.services.case_compilation import digest, readiness


def evaluated(case):
    job = case.get('compile_job') or {}
    revision = case.get('source_revision_id')
    return bool(revision and job.get('revision_id') == revision
                and job.get('job_id') == case.get('latest_compile_job_id')
                and (job.get('status') == 'failed'
                     or job.get('status') == 'succeeded' and readiness(case) is None))



def review_items(case, review):
    """Keep disappeared diagnostics open until the existing evidence rule closes them."""
    from mino_nexus.services.case_review import _state
    items = list(review['requirements'])
    active = {item['id'] for item in items}
    latest = {}
    for record in review.get('history') or []:
        if (record.get('revision_id') == str(case.get('source_revision_id') or '')
                and record.get('source_revision') == int(case.get('v5_draft_revision') or 1)):
            latest[(record['item_id'], digest(record.get('context') or {}))] = record
    for record in latest.values():
        evidence = record.get('evidence') or {}
        item = deepcopy(evidence.get('diagnostic') or {})
        if not item or item.get('id') in active:
            continue
        status = record.get('review_status') or 'pending_review'
        if status == 'verified_resolved':
            if evidence.get('state_digest') == _state(case):
                continue
            status = 'fixed_pending_run'
        item['review_status'] = status
        items.append(item)
    return items


def aggregate(cases, describe, *, app_id, project_id, owner, platform='', category='', status='', page=1, size=50):
    """Group by diagnostic identity, not revision-local item/operation IDs.

    Grouping means the same diagnostic kind, never a confirmed common root cause.
    Counts are calculated before paging; case links retain each original item ID.
    """
    groups = {}
    totals = Counter()
    platforms = {}
    for case in cases:
        # Cases are project-shared unless explicitly tied to another application.
        if case.get('app_id') and case['app_id'] != app_id:
            continue
        plat = str(case.get('platform') or 'unknown').lower()
        if platform and plat != platform:
            continue
        assessed = evaluated(case)
        counts = platforms.setdefault(plat, Counter())
        for counter in (counts, totals):
            counter['total_cases'] += 1
            counter['evaluated_cases'] += int(assessed)
        review = describe(case)
        seen = set()
        affected = False
        for item in review_items(case, review):
            context = item.get('context') or {}
            if context and context.get('app_id') != app_id:
                continue
            if category and (item.get('category') or 'unconfirmed') != category:
                continue
            if status and item.get('review_status') != status:
                continue
            # Same source item from repeated snapshots is one occurrence.
            occurrence = (item['id'], digest(context))
            if occurrence in seen:
                continue
            seen.add(occurrence)
            affected = True
            identity = [app_id, plat, item['origin'], item['reason_code'], item.get('category'),
                        item.get('phase'), item.get('control_id'), sorted(map(str, item.get('missing_fields') or []))]
            key = digest(identity)[:24]
            group = groups.setdefault(key, {'id': key, 'platform': plat,
                **{k: item.get(k) for k in ('origin', 'reason_code', 'category', 'message', 'phase')},
                'occurrences': 0, 'stale_occurrences': 0, 'statuses': Counter(), 'cases': {}})
            group['occurrences'] += 1
            group['stale_occurrences'] += int(item.get('freshness') == 'stale')
            group['statuses'][item['review_status']] += 1
            link = group['cases'].setdefault(case['case_id'], {'case_id': case['case_id'],
                'name': case.get('name') or case['case_id'], 'revision_id': case.get('source_revision_id') or '',
                'evaluated': assessed, 'item_ids': []})
            if item['id'] not in link['item_ids']:
                link['item_ids'].append(item['id'])
        for counter in (counts, totals):
            counter['affected_cases'] += int(affected)
            counter['evaluated_affected_cases'] += int(affected and assessed)
    rows = []
    for group in groups.values():
        group['cases'] = sorted(group['cases'].values(), key=lambda c: c['case_id'])
        group['affected_cases'] = len(group['cases'])
        rows.append(group)
    rows.sort(key=lambda r: (-r['affected_cases'], r['platform'], r['reason_code'], r['id']))
    def summary(counter):
        return {**{k: counter[k] for k in ('total_cases', 'evaluated_cases', 'affected_cases', 'evaluated_affected_cases')},
                'unevaluated_cases': counter['total_cases'] - counter['evaluated_cases']}
    return {'app_id': app_id, 'project_id': project_id, 'owner_user_id': owner,
        'as_of': datetime.now(timezone.utc).isoformat(),
        'scope': 'project_shared_and_app_specific_cases',
        'evaluation_basis': 'current_revision_terminal_compilation',
        'summary': {**summary(totals), 'unique_issues': len(rows), 'occurrences': sum(r['occurrences'] for r in rows)},
        'platforms': [{'platform': p, **summary(c)} for p, c in sorted(platforms.items())],
        'items': rows[(page - 1) * size:page * size], 'page': page, 'size': size, 'total': len(rows)}


def list_issues(project_id, app_id, **filters):
    owner = require_user_id()
    with session_scope() as db:
        app = db.get(App, app_id)
        if app is None or app.project_id != project_id:
            raise HTTPException(status_code=404, detail='应用不存在或不属于该项目')
    from mino_nexus.services.case_store import list_cases
    from mino_nexus.services.case_review import describe
    return aggregate(list_cases(project_id), describe, app_id=app_id, project_id=project_id, owner=owner, **filters)

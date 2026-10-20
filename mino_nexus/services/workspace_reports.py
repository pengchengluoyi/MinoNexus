"""Reports use run_store facts; optional model output is advice only."""
import json
import time
from collections import Counter
from mino_nexus.core.database import session_scope
from mino_nexus.models.mino_bots import CollabMessage
from mino_nexus.services import collab_workspace as ws, run_store


def facts(project_id, topic_id, sess, run_id=''):
    owner = ws.authorize(project_id, sess)
    with session_scope() as db:
        topic = ws.owned(db, project_id, topic_id)
        if not run_id:
            card = db.query(CollabMessage).filter_by(topic_id=topic_id, card_kind='run').order_by(CollabMessage.created_at.desc()).first()
            run_id = card.ref_id if card else ''
        title = topic.title
    doc = run_store.get(run_id) if run_id else None
    from mino_nexus.services.project_store import require_app
    if not doc or doc.get('owner_user_id') != owner or require_app(doc['app_id'])['project_id'] != project_id:
        raise ValueError('请指定当前项目中可读取的运行，或先在工作区执行用例。')
    if run_store.task_is_live(doc):
        raise ValueError('运行尚未结束，不能生成终态报告。')
    cases = [{'case_id': c.get('case_id'), 'name': c.get('name') or c.get('case_id'),
              'status': c.get('status') or 'unknown', 'session_id': c.get('session_id') or '',
              'reason': str(c.get('fail_reason') or c.get('error') or c.get('summary') or '')[:800]}
             for c in doc.get('cases', []) if isinstance(c, dict)]
    counts = dict(Counter(c['status'] for c in cases))
    evidence = list(dict.fromkeys([run_id] + [c['session_id'] for c in cases if c['session_id']]))
    from mino_nexus.core.secret_redaction import sanitize
    return sanitize({'title': title, 'run_id': run_id, 'app_id': doc['app_id'], 'status': doc.get('status'),
        'total': doc.get('total', len(cases)), 'passed': doc.get('passed', 0), 'failed': doc.get('failed', 0),
        'counts': counts, 'cases': cases, 'allowed_evidence_ids': evidence,
        'missing_session_evidence': sum(not c['session_id'] for c in cases),
        'generated_at': ws.now()})


def markdown(report):
    lines = ['# 测试报告', report['title'], f"运行：{report['run_id']}；状态：{report['status']}",
             f"总数 {report['total']}；通过 {report['passed']}；失败 {report['failed']}",
             '逐项状态：' + '；'.join(f'{k} {v}' for k, v in report['counts'].items()),
             f"缺少 Session 引用：{report['missing_session_evidence']}。运行终态不等于业务目标已达成。"]
    for case in report['cases']:
        lines.append(f"- {case['name']}：{case['status']}；Session：{case['session_id'] or '未记录'}；{case['reason']}")
    if report.get('advice_error'):
        lines.append('建议未生成：' + report['advice_error'])
    if report.get('next_steps'):
        lines.extend(['## 后续建议', *['- ' + s for s in report['next_steps']]])
    return '\n\n'.join(lines)


def generate(project_id, topic_id, sess, run_id='', *, deadline=None, advice=True):
    report = facts(project_id, topic_id, sess, run_id)
    report.update(next_steps=[], evidence_ids=[])
    if advice:
        from mino_nexus.ai.llm_client import resolve_regression_provider, call_chat_plain
        from mino_nexus.ai.prompt_render import render
        from mino_nexus.ai import dispatch_log
        from mino_nexus.services.workspace_agent import bounded_json
        provider, _ = resolve_regression_provider()
        remaining = (deadline or time.monotonic() + 45) - time.monotonic()
        if not provider or remaining <= 0:
            report['advice_error'] = '未配置可用模型或本轮时间已用完；事实报告仍可查看。'
        else:
            messages, meta = render('draft_test_report', {'user_payload': bounded_json(report, 16000)})
            token = dispatch_log.bind(job='draft_test_report', role='report-writer', prompt_version=meta.get('prompt_version'), prompt_sha=meta.get('prompt_sha'))
            try:
                raw, result = call_chat_plain(provider=provider, messages=messages, temperature=0.1,
                    max_tokens=768, timeout_sec=min(45, remaining), max_attempts=1)
                parsed = json.loads(raw)
                if not isinstance(parsed, dict) or set(parsed) != {'next_steps', 'evidence_ids'}:
                    raise ValueError('报告建议格式不符合合同')
                steps, ids = parsed['next_steps'], parsed['evidence_ids']
                if (not isinstance(steps, list) or len(steps) > 5 or any(not isinstance(s, str) or len(s) > 400 for s in steps)
                    or not isinstance(ids, list) or len(ids) > 30 or any(not isinstance(i, str) or i not in report['allowed_evidence_ids'] for i in ids)):
                    raise ValueError('报告建议含无效引用或超出长度限制')
                report.update(next_steps=steps, evidence_ids=ids)
            except (ValueError, TypeError):
                report['advice_error'] = '模型未返回符合合同的建议；未采用模型生成的数字或结论。'
            finally:
                dispatch_log.reset(token)
    return report

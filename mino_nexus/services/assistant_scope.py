"""项目共享用例范围；使用稳定排序与数据库完整结果，不依赖模型预览。"""
from __future__ import annotations
import hashlib
import json
from typing import Any
from mino_nexus.core.database import session_scope
from mino_nexus.models.case import ProjectCase


def resolve(args: dict[str, Any]) -> tuple[dict, str, list[dict]]:
    from mino_nexus.services.project_store import require_app
    app = require_app(str(args['app_id']))
    pid = str(app.get('project_id') or '')
    out = dict(args, project_id=pid)
    with session_scope() as db:
        rows = db.query(ProjectCase).filter_by(project_id=pid).order_by(ProjectCase.sort_index, ProjectCase.pk).all()
        modules = sorted({str(row.module or '').strip() or '未分组' for row in rows})
        if 'case_ids' in args and not args['case_ids']:
            return out, '没有选中任何用例，本次不会执行整个项目。请明确模块或用例。', []
        selected = rows
        module = str(args.get('module') or '').strip()
        requested_modules = list(args.get('modules') or ([module] if module else []))
        if 'modules' in args and not requested_modules:
            return out, '没有选择测试模块，本次不会执行整个项目。', []
        if requested_modules:
            if len(set(requested_modules)) != len(requested_modules):
                requested_modules = list(dict.fromkeys(requested_modules))
            matched = []
            for value in requested_modules:
                hits = [name for name in modules if name.casefold() == str(value).casefold()]
                if len(hits) != 1:
                    return out, '没有找到唯一匹配的模块，请选择当前项目的模块。', [{'label': name, 'value': name} for name in modules]
                matched.append(hits[0])
            out['modules'] = matched
            out.pop('module', None)
            selected = [row for row in selected if (str(row.module or '').strip() or '未分组') in matched]
        if args.get('search'):
            keyword = str(args['search']).casefold()
            selected = [row for row in selected if keyword in str(row.name or '').casefold()]
        if args.get('case_ids') is not None:
            wanted = [str(value) for value in args['case_ids']]
            found = {str(row.case_id) for row in selected}
            if len(set(wanted)) != len(wanted) or not set(wanted).issubset(found):
                return out, '所选用例已变化、已删除或不属于当前范围，请重新选择。', []
            selected = [row for row in selected if str(row.case_id) in wanted]
        if args.get('ordinal'):
            ordinal = int(args['ordinal'])
            if ordinal < 1 or ordinal > len(selected):
                return out, f'当前范围共有 {len(selected)} 条用例，没有第 {ordinal} 条。请指定有效范围。', []
            selected = [selected[ordinal - 1]]
        if not any(args.get(key) for key in ('module', 'modules', 'search', 'ordinal', 'case_ids', 'all_cases')):
            return out, '本次测试哪个模块？也可以明确说执行整个项目的全部用例。', [{'label': name, 'value': name} for name in modules]
        if not selected:
            return out, '这个范围没有用例，本次任务尚未提交。请在用例页检查模块和筛选条件。', []
        digest = hashlib.sha256(json.dumps([(row.case_id, row.sort_index, str(row.updated_at), row.precondition,
                                            row.steps, row.expected, (row.extra or {}).get('meta', {}).get('ui_automation'))
                                           for row in selected], ensure_ascii=False, default=str).encode()).hexdigest()
        if args.get('scope_revision') and args['scope_revision'] != digest:
            return out, '用例内容或范围在等待期间发生变化，请重新选择，避免执行错误范围。', []
        out['case_ids'] = [str(row.case_id) for row in selected]
        out['scope_revision'] = digest
        # 冻结已解析 ID；继续请求时不能把 ordinal 在选中子集上再应用一次。
        out.pop('ordinal', None)
        return out, '', []

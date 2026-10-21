"""Source coverage and executable actions, without changing historical records."""
import re


def explicit_remark(text):
    return bool(re.match(r'^\s*[（(]?\s*(?:备注|说明|仅供参考|仅对齐预期|跑批不执行)\s*[：:）)]', str(text or '')))


def precondition_lines(text):
    from mino_nexus.runtime.session_gate import split_precondition_text
    lines = [re.sub(r'^\s*\d+[.、．)）]\s*', '', line).strip() for line in split_precondition_text(text)]
    return [line for line in lines
            if line.lower() not in {'', '无', '暂无', '无要求', '无前置条件', 'none', 'any'}]


def precondition_diagnostics(case):
    precondition = str(case.get('precondition') or case.get('precondition_raw') or '')
    source = precondition_lines(precondition)
    if not source:
        return []
    claim = case.get('resource_key') or (case.get('meta') or {}).get('resource_key') or {}
    rows = claim.get('precondition_coverage') or []
    if [row.get('source') for row in rows] != source:
        return [{'reason': 'precondition_coverage_missing', 'source_clause': precondition,
                 'user_message': '前置原文没有完整对应的准备或资源验证项，请按当前原文重新编译。'}]
    issues = [{'reason': row.get('reason') or 'precondition_not_executable',
             'source_clause': row['source'], 'user_message': row.get('detail') or '前置缺少可执行验证或真实夹具，不能跳过。'}
            for row in rows if row.get('status') != 'mapped' or not coverage_matches(row, claim)]
    scene = case.get('case_scene')
    if isinstance(scene, dict) and any(row.get('status') == 'mapped' and not coverage_matches(
            row, {**claim, 'case_scene': scene}) for row in rows):
        issues.append({'reason': 'precondition_scene_mismatch', 'source_clause': precondition,
                       'user_message': '执行场景丢失了已编译的前置条件，请使用当前原文重新编译。'})
    return issues


def coverage_matches(row, claim):
    conditions = row.get('conditions') or []
    if not conditions:
        return False
    for condition in conditions:
        reference, rules = condition.get('key_ref'), condition.get('rules') or {}
        if reference == 'precondition.device_session':
            wanted = rules.get('required_session')
            if wanted not in {'logged_in', 'guest'} or (claim.get('case_scene') or {}).get('required_session') != wanted:
                return False
        elif reference == 'precondition.account_field':
            required = ((claim.get('account') or {}).get('requirements') or {}).get('all') or []
            if {'facet': rules.get('facet'), 'op': 'eq', 'value': rules.get('value')} not in required:
                return False
        elif reference == 'precondition.clear_cache':
            if not any(item.get('kind') == 'clear_cache' for item in (claim.get('case_scene') or {}).get('prep_items') or []):
                return False
        elif reference != 'precondition.author_remark' or not explicit_remark(condition.get('clause')):
            return False
    return True


def operation_diagnostics(case):
    rows = []
    bundles = (case.get('meta') or {}).get('step_program_keys') or {}
    for number, bundle in bundles.items():
        for step in (bundle.get('do_program_plan') or {}).get('steps') or []:
            source = step.get('source_clause') or step.get('source_line') or step.get('title') or ''
            skipped = step.get('kind') == 'remark' or step.get('run_policy') == 'skip'
            if skipped and not explicit_remark(source):
                rows.append({'reason': 'required_operation_skipped', 'case_step': number,
                             'source_clause': source, 'user_message': '必需操作或观察被编译为可跳过说明，请按当前原文重新编译。'})
    return rows


def validate_precondition_hits(hits, claim, fields):
    definitions = {row['key']: row for row in fields if row.get('key')}
    mapped = []
    for hit in hits:
        reference = hit.get('key_ref')
        rules = hit.get('rules') or {}
        if reference == 'precondition.device_session':
            required = rules.get('required_session')
            if required not in {'logged_in', 'guest'}:
                raise ValueError('precondition_session_invalid')
            previous = claim['case_scene']['required_session']
            if previous not in {'any', required}:
                raise ValueError('precondition_session_conflict')
            claim['case_scene']['required_session'] = required
            claim['case_scene']['session_prep'] = 'skip' if required == 'logged_in' else 'logout'
            claim['device_app']['required_session'] = required
            claim['device_app']['allow'] = ['logged_in'] if required == 'logged_in' else ['guest', 'logged_out']
            mapped.append('prep_device_session')
        elif reference == 'precondition.account_field':
            facet, value = rules.get('facet'), rules.get('value')
            definition = definitions.get(facet)
            if not definition or value not in {option.get('value') for option in definition.get('options') or []}:
                raise ValueError('precondition_account_field_unavailable')
            constraints = claim['account']['requirements'].setdefault('all', [])
            clause = {'facet': facet, 'op': 'eq', 'value': value}
            if any(row.get('facet') == facet and row != clause for row in constraints):
                raise ValueError('precondition_account_field_conflict')
            if clause not in constraints:
                constraints.append(clause)
            mapped.append('account.requirements.' + facet)
        elif reference == 'precondition.clear_cache':
            item = {'kind': 'clear_cache', 'phase': 'before_launch', 'text': hit['clause']}
            claim['case_scene']['prep_items'].append(item)
            claim['device_app']['prep'].append(dict(item))
            mapped.append('prep_clear_cache')
        elif reference == 'precondition.author_remark' and explicit_remark(hit.get('clause')):
            mapped.append('author_remark')
        else:
            raise ValueError('precondition_not_executable')
    return mapped


def compile_preconditions(precondition, claim, *, platform, env_doc):
    from copy import deepcopy
    from mino_nexus.services.account_pool_templates import merged_pool_field_defs
    from mino_nexus.services.case_step_span_llm import compile_step_spans
    fields = merged_pool_field_defs(env_doc)
    result = deepcopy(claim)
    coverage = []
    for source in precondition_lines(precondition):
        hits, failure = compile_step_spans(source, 'precondition', platform=platform,
                                         field_defs=fields)
        if failure:
            coverage.append({'source': source, 'status': 'unsupported',
                             'reason': 'precondition_not_executable',
                             'detail': '前置「' + source + '」没有对应的准备能力、账号约束或已验证夹具。'})
            continue
        candidate = deepcopy(result)
        try:
            references = validate_precondition_hits(hits, candidate, fields)
            if not references:
                raise ValueError('precondition_not_executable')
        except ValueError as error:
            coverage.append({'source': source, 'status': 'unsupported', 'reason': str(error),
                             'detail': '前置「' + source + '」无法映射到真实可验证条件。'})
            continue
        result = candidate
        coverage.append({'source': source, 'status': 'mapped', 'references': references,
                         'conditions': [{key: hit[key] for key in ('key_ref', 'rules', 'clause')} for hit in hits]})
    result['case_scene']['lease_requirements'] = deepcopy(result['account']['requirements'])
    result['precondition_coverage'] = coverage
    return result

"""Source coverage and executable actions, without changing historical records.

Preconditions are split into two different questions:

* Can the UI plan (operations + checks) be compiled and executed?
* Can the starting data / device / account state be prepared?

An un-prepared business fixture is not a UI capability gap.  It must be surfaced
as ``needs_setup`` so the UI steps stay executable once the fixture is ready.
"""
import re

SETUP_REFERENCES = frozenset(
    {
        "precondition.fixture",
        "precondition.manual",
        "precondition.ui_state",
        "precondition.device_state",
    }
)

_SETUP_REASON = {
    "precondition.fixture": "fixture_required",
    "precondition.manual": "manual_prerequisite_required",
    "precondition.ui_state": "ui_state_precondition",
    "precondition.device_state": "device_state_setup_required",
}

_SETUP_DETAIL = {
    "precondition.fixture": "这句描述业务数据或后台状态，需要 fixture、接口造数或人工准备；UI 步骤仍可执行。",
    "precondition.manual": "这句没有对应的自动准备能力，需要人工准备或补充夹具；UI 步骤仍可执行。",
    "precondition.ui_state": "这是起点界面状态，不是可执行的准备能力；请改成【导航:...】或首步操作，或人工确认起点。",
    "precondition.device_state": "这句描述设备或账号状态，需要 Prep、机态登记或人工准备；UI 步骤仍可执行。",
}

_ACTION_RE = re.compile(
    r"(?:点击|点按|单击|点一下|输入|填写|选择|返回|"
    r"(?<!已)(?<!首次)打开|(?<!已)(?<!首次)启动|(?<!已)关闭|(?<!已)进入|"
    r"滑动|上滑|下滑|左滑|右滑|长按|勾选|取消勾选|上传|下载|扫码|扫描|"
    r"从[^，。；]{0,16}(?:点击|进入|打开))"
)
_COMPOUND_RE = re.compile(r"[，,；;]|(?:或者|或)")
_SESSION_RE = re.compile(r"(?:已登录|未登录|登录状态|游客|访客|登出|退出登录)")
_UI_STATE_RE = re.compile(
    r"(?:已(?:进入|打开|到达|停留|展示|显示|播放完|结束)|当前(?:在|处于)|"
    r"页面|界面|屏幕|弹窗|动画[^，。；]{0,12}(?:播放|结束)|已出现|已展示)"
)
_BUSINESS_RE = re.compile(
    r"(?:已(?:配置|生成|创建|支付|退款|下单|购买|绑定|发布|上架|审核|领取|"
    r"注册|开通|充值|完成)|注册(?:时间|天数|≤|>=|<|>)|订单|商品|价格|折扣|"
    r"优惠|活动|作品|海报|优惠券|会员|余额|额度|库存|短信|任务|退款|支付)"
)
_DEVICE_RE = re.compile(
    r"(?:设备|手机|首次安装|未授权|已授权|已清(?:除|空|数据)|未清|播放过|未播放|"
    r"新设备|旧设备|同一账号|设备[A-Za-z]|系统时间|时区|语言|权限)"
)
_ACCOUNT_RE = re.compile(r"(?:账号|用户|登录|游客)")


def explicit_remark(text):
    return bool(re.match(r'^\s*[（(]?\s*(?:备注|说明|仅供参考|仅对齐预期|跑批不执行)\s*[：:）)]', str(text or '')))


def precondition_lines(text):
    from mino_nexus.runtime.session_gate import split_precondition_text
    lines = [re.sub(r'^\s*\d+[.、．)）]\s*', '', line).strip() for line in split_precondition_text(text)]
    return [line for line in lines
            if line.lower() not in {'', '无', '暂无', '无要求', '无前置条件', 'none', 'any'}]


def _fixture_id(text):
    slug = re.sub(r"[^\w]+", "_", str(text or "").strip().lower()).strip("_")[:56]
    return "fixture." + (slug or "precondition")


def _classification(status, klass, severity, reason, detail, setup=None):
    row = {"status": status, "class": klass, "severity": severity,
           "reason": reason, "detail": detail}
    if setup:
        row["setup"] = setup
    return row


def classify_precondition_line(text, failure=""):
    """Deterministic fallback when the LLM cannot map a precondition line.

    The result never claims the line is ready.  It only decides whether the line
    is a source problem (an action written in the wrong column, which must be
    edited) or a setup problem (data/device state, which can be prepared).
    """
    raw = str(text or "").strip()
    detail = str(failure or "").strip()
    if _ACTION_RE.search(raw):
        message = "这句是操作，不应作为前置；请移到步骤，或改成可执行的准备项。"
        if _COMPOUND_RE.search(raw):
            message = "这句包含多个动作或备选路径；请拆成单动作步骤，前置只保留起点状态。"
        return _classification("source", "source", "blocking", "precondition_is_action",
                               message, None)
    if _SESSION_RE.search(raw):
        return _classification(
            "needs_setup", "setup", "degraded", "device_session_setup_required",
            "这句描述设备/账号登录态，请改成「设备登录态：已登录/未登录/游客」或补齐 Prep。",
            {"kind": "device_session", "verify": "declared"},
        )
    if _UI_STATE_RE.search(raw):
        return _classification(
            "needs_setup", "setup", "degraded", "ui_state_precondition",
            _SETUP_DETAIL["precondition.ui_state"],
            {"kind": "ui_state", "verify": "runtime"},
        )
    if _BUSINESS_RE.search(raw):
        return _classification(
            "needs_setup", "setup", "degraded", "fixture_required",
            _SETUP_DETAIL["precondition.fixture"],
            {"kind": "fixture", "id": _fixture_id(raw), "verify": "declared"},
        )
    if _DEVICE_RE.search(raw) or _ACCOUNT_RE.search(raw):
        return _classification(
            "needs_setup", "setup", "degraded", "device_state_setup_required",
            _SETUP_DETAIL["precondition.device_state"],
            {"kind": "device_state", "verify": "declared"},
        )
    return _classification(
        "needs_setup", "setup", "degraded", "manual_prerequisite_required",
        _SETUP_DETAIL["precondition.manual"] + (("（" + detail[:120] + "）") if detail else ""),
        {"kind": "manual", "verify": "declared"},
    )


def _issue(reason, source, user_message, *, klass="setup", severity="degraded",
           setup=None, detail=""):
    row = {"reason": str(reason or "manual_prerequisite_required"),
           "class": klass, "severity": severity,
           "source_clause": str(source or ""),
           "user_message": str(user_message or "")[:400]}
    if setup:
        row["setup"] = setup
    if detail:
        row["detail"] = str(detail)[:240]
    return row


def precondition_diagnostics(case):
    precondition = str(case.get('precondition') or case.get('precondition_raw') or '')
    source = precondition_lines(precondition)
    if not source:
        return []
    claim = case.get('resource_key') or (case.get('meta') or {}).get('resource_key') or {}
    rows = claim.get('precondition_coverage') or []
    if [str(row.get('source') or '') for row in rows] != source:
        return [_issue(
            "precondition_coverage_missing", precondition,
            "前置原文没有完整对应的准备或资源验证项；请重新编译。UI 步骤仍可单独执行，但前置未准备前不能算可跑。",
            setup={"kind": "recompile", "verify": "recompile"},
        )]
    issues = []
    for row in rows:
        source_line = str(row.get('source') or '')
        status = str(row.get('status') or 'mapped')
        reason = str(row.get('reason') or '')
        klass = str(row.get('class') or ('source' if status == 'source' else 'setup'))
        if status == 'mapped':
            if coverage_matches(row, claim):
                continue
            issues.append(_issue(
                "precondition_scene_mismatch", source_line,
                "执行场景丢失了已编译的前置条件，请使用当前原文重新编译。",
                setup={"kind": "scene", "verify": "recompile"},
            ))
            continue
        if klass == 'source' or status == 'source':
            issues.append(_issue(
                reason or "precondition_is_action", source_line,
                str(row.get('detail') or row.get('user_message') or
                    "前置里写入了操作，请移到步骤后再编译。"),
                klass="source", severity="blocking",
            ))
            continue
        if status == 'unsupported' or not reason:
            fallback = classify_precondition_line(source_line, reason)
            issues.append(_issue(
                fallback["reason"], source_line, fallback["detail"],
                klass=fallback["class"], severity=fallback["severity"],
                setup=fallback.get("setup"),
            ))
            continue
        setup_row = row.get('setup') if isinstance(row.get('setup'), dict) else {}
        detail = str(row.get('detail') or row.get('user_message') or
                     _SETUP_DETAIL.get(f"precondition.{setup_row.get('kind', 'manual')}",
                                       _SETUP_DETAIL["precondition.manual"]))
        issues.append(_issue(
            reason, source_line, detail,
            klass=klass, severity=str(row.get('severity') or 'degraded'),
            setup=setup_row or {"kind": "manual", "verify": "declared"},
        ))
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
        elif reference == 'precondition.account_session':
            if str((claim.get('account') or {}).get('required_session') or 'any') != str(rules.get('required_session') or 'any'):
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
                rows.append({'reason': 'required_operation_skipped', 'class': 'source',
                             'severity': 'blocking', 'case_step': number,
                             'source_clause': source,
                             'user_message': '必需操作或观察被编译为可跳过说明，请按当前原文重新编译。'})
    return rows


def _setup_from_hit(hit):
    reference = str(hit.get('key_ref') or '')
    rules = hit.get('rules') if isinstance(hit.get('rules'), dict) else {}
    kind = {
        'precondition.fixture': 'fixture',
        'precondition.manual': 'manual',
        'precondition.ui_state': 'ui_state',
        'precondition.device_state': 'device_state',
    }.get(reference, 'manual')
    setup = {"kind": kind, "verify": str(rules.get('verify') or 'declared')}
    if kind == 'fixture':
        setup['id'] = str(rules.get('fixture_id') or rules.get('id') or '').strip()
    if kind == 'ui_state':
        for key in ('screen', 'state'):
            if str(rules.get(key) or '').strip():
                setup[key] = str(rules.get(key)).strip()
    return setup, str(rules.get('detail') or rules.get('reason') or '')


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
        elif reference == 'precondition.account_session':
            required = str(rules.get('required_session') or 'any').strip()
            if required not in {'logged_in', 'logged_out', 'guest', 'any'}:
                raise ValueError('precondition_account_session_invalid')
            claim.setdefault('account', {})['required_session'] = required
            mapped.append('account.required_session.' + required)
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
        elif reference in SETUP_REFERENCES:
            setup, _detail = _setup_from_hit(hit)
            if setup['kind'] == 'fixture' and not setup.get('id'):
                raise ValueError('fixture_id_required')
            mapped.append(reference)
        elif reference == 'precondition.author_remark' and explicit_remark(hit.get('clause')):
            mapped.append('author_remark')
        else:
            raise ValueError('precondition_not_executable')
    return mapped


def compile_preconditions(precondition, claim, *, platform, env_doc):
    from copy import deepcopy
    from mino_nexus.resources.accounts.pool_templates import merged_pool_field_defs
    from mino_nexus.casework.case_step_span_llm import compile_step_spans
    fields = merged_pool_field_defs(env_doc)
    result = deepcopy(claim)
    coverage = []
    for source in precondition_lines(precondition):
        hits, failure = compile_step_spans(source, 'precondition', platform=platform,
                                         field_defs=fields)
        if failure:
            fallback = classify_precondition_line(source, str(failure.get('detail') or failure.get('reason') or ''))
            coverage.append({'source': source, **fallback})
            continue
        candidate = deepcopy(result)
        try:
            references = validate_precondition_hits(hits, candidate, fields)
            if not references:
                raise ValueError('precondition_not_executable')
        except ValueError as error:
            fallback = classify_precondition_line(source, str(error))
            coverage.append({'source': source, **fallback})
            continue
        setup_hit = next((hit for hit in hits if str(hit.get('key_ref') or '') in SETUP_REFERENCES), None)
        if setup_hit is not None:
            setup, detail = _setup_from_hit(setup_hit)
            reference = str(setup_hit.get('key_ref') or '')
            coverage.append({
                'source': source,
                'status': 'needs_setup',
                'class': 'setup',
                'severity': 'degraded',
                'reason': _SETUP_REASON.get(reference, 'manual_prerequisite_required'),
                'detail': detail or _SETUP_DETAIL.get(reference, _SETUP_DETAIL['precondition.manual']),
                'setup': setup,
                'references': references,
                'conditions': [{key: hit[key] for key in ('key_ref', 'rules', 'clause')} for hit in hits],
            })
            continue
        result = candidate
        coverage.append({'source': source, 'status': 'mapped', 'references': references,
                         'conditions': [{key: hit[key] for key in ('key_ref', 'rules', 'clause')} for hit in hits]})
    result['case_scene']['lease_requirements'] = deepcopy(result['account']['requirements'])
    result['precondition_coverage'] = coverage
    return result

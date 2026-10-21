"""Current-frame dialog evidence. Foreground package is never dialog proof."""
import math

_DIALOG_IDS = frozenset({'dialog_root_view', 'parentpanel', 'alertdialog', 'dialog_container'})


def dialog_nodes(nodes):
    found = []
    for node in nodes or []:
        if not isinstance(node, dict) or node.get('visible') is False:
            continue
        rid = str(node.get('resource_id') or '')
        role = str(node.get('role') or '').lower()
        cls = str(node.get('class') or node.get('class_name') or '').lower()
        ref = str(node.get('node_ref') or rid or '')
        if ref and (rid.lower().split('/')[-1] in _DIALOG_IDS
                    or role in {'dialog', 'alertdialog'} or cls.endswith(('dialog', 'dialogfragment'))):
            found.append(ref)
    return found


def has_back(nodes):
    for node in nodes or []:
        if not isinstance(node, dict) or node.get('visible') is False:
            continue
        rid = str(node.get('resource_id') or '').lower().split('/')[-1]
        label = str(node.get('content_desc') or node.get('text') or '').strip().lower()
        if rid in {'back', 'back_button', 'navigate_up', 'navigation_back'} or label in {'back', 'navigate up', '返回', '返回上一页'}:
            return True
    return False


def dialog_evidence(nodes, report=None, *, frame_ref=''):
    refs = dialog_nodes(nodes)
    if refs:
        return {'source': 'hierarchy', 'dialog_node_refs': refs}
    report = report if isinstance(report, dict) else {}
    box = report.get('dialog_region')
    # Screenshot boxes are normalized [left, top, right, bottom], bound by the
    # caller to the observation actually supplied to the model, never a stale UI hint.
    if (frame_ref and isinstance(box, list) and len(box) == 4
            and all(type(v) in (int, float) and math.isfinite(v) and 0 <= v <= 1 for v in box)
            and box[0] < box[2] and box[1] < box[3]
            and (box[2]-box[0]) * (box[3]-box[1]) < .95
            and str(report.get('dialog_evidence') or '').strip()):
        return {'source': 'screenshot', 'frame_ref': frame_ref, 'dialog_region': list(box),
                'dialog_evidence': str(report['dialog_evidence'])[:300]}
    return {}

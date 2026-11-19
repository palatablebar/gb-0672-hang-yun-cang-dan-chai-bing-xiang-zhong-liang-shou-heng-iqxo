"""截关处置批次。

在完整核验结果之上，逐张主单给出「进入 / 暂缓 / 阻断」结论及依据。
判定只读取同一次运行已经算出的图、件重、危险品和采信证据，因此拆并关系
变化后重新产生的件重结论会自然进入本批，不会被旧的平面汇总覆盖。
"""

from collections import defaultdict
import hashlib, json
from app.config import RULE_VERSION
from app.coerce import instant

ENTER = '进入'
HOLD = '暂缓'
BLOCK = '阻断'

SEVERITY = {BLOCK: 0, HOLD: 1, ENTER: 2}

# 无法在截关前靠补正闭合，或本身不允许交接的原因。
BLOCKING = {
    'MISSING_PARENT', 'DEPENDENCY_CYCLE', 'UNATTACHED',
    'INCOMPATIBLE', 'UNKNOWN_HAZARD', 'EXPIRED_HOLD',
    'COLUMN_COUNT', 'MISSING_ID', 'DUPLICATE_CONFLICT', 'MISSING_VALUE',
    'BAD_NUMBER', 'BAD_TIME', 'BAD_UNIT', 'INVALID_RANGE',
}
# 等一张补正单或一次复核就能闭合的原因。
HOLDING = {'MISSING_UN', 'WEIGHT_MISMATCH'}

CAUSE_LABELS = {
    'READY': '可交接',
    'MISSING_PARENT': '缺失上级单',
    'DEPENDENCY_CYCLE': '拆并成环',
    'UNATTACHED': '关系未闭合',
    'INCOMPATIBLE': '危险品不相容',
    'UNKNOWN_HAZARD': '危险类未知',
    'MISSING_UN': '缺 UN 编号',
    'WEIGHT_MISMATCH': '件重未对平',
    'EXPIRED_HOLD': '到期未闭合',
    'COLUMN_COUNT': '列数错误',
    'MISSING_ID': '编号缺失',
    'DUPLICATE_CONFLICT': '编号冲突',
    'MISSING_VALUE': '数值缺失',
    'BAD_NUMBER': '数值格式',
    'BAD_TIME': '时间格式',
    'BAD_UNIT': '单位不支持',
    'INVALID_RANGE': '范围错误',
}


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _digest(value):
    return hashlib.sha256(_canonical(value).encode('utf-8')).hexdigest()


def _subtree(root, children):
    seen, stack = set(), [root]
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        stack.extend(children.get(node, []))
    return seen


def _add(bucket, code, detail, source):
    cause = {'code': code, 'detail': detail, 'source': source}
    if cause not in bucket:
        bucket.append(cause)


def _cause_sort(cause):
    # 到期升级只是结论的时间注脚，永远排在业务依据之后。
    return (cause['code'] == 'EXPIRED_HOLD', SEVERITY.get(_severity(cause['code']), 1),
            cause['code'], cause['detail'])


def _severity(code):
    if code in BLOCKING:
        return BLOCK
    if code in HOLDING:
        return HOLD
    return BLOCK


def _window(state, as_of):
    cutoff_at = state.get('context', {}).get('cutoff_at')
    parsed_cutoff = instant(cutoff_at) if cutoff_at not in (None, '') else None
    parsed_as_of = instant(as_of) if as_of not in (None, '') else None
    # 只有同时给出截关时点和判定时点才把时间纳入结论，避免墙钟时间破坏可复现性。
    expired = bool(parsed_cutoff and parsed_as_of and parsed_as_of > parsed_cutoff)
    return cutoff_at, expired


def decide(state, as_of=None):
    graph = state.get('graph', {'roots': [], 'children': {}, 'errors': []})
    roots = sorted(graph.get('roots', []))
    children = graph.get('children', {})
    by_id = {row['id']: row for row in state.get('records', [])}

    members, root_of = {}, {}
    for root in roots:
        nodes = _subtree(root, children)
        members[root] = nodes
        for node in nodes:
            root_of[node] = root

    causes = defaultdict(list)
    unassigned_causes = defaultdict(list)
    unassigned_kind = {}

    def attach(node, code, detail, source):
        root = root_of.get(node)
        if root is not None:
            _add(causes[root], code, detail, source)
        else:
            if node in by_id:
                unassigned_kind.setdefault(node, '未归属')
            _add(unassigned_causes[node], code, detail, source)

    # 拆并关系：缺父级、成环，或整段关系挂不到任何主单。
    cycle_nodes = set()
    for error in graph.get('errors', []):
        path = error.get('path', [])
        if error.get('code') == 'DEPENDENCY_CYCLE':
            detail = '拆并关系成环：' + ' → '.join(path or [error.get('id', '')])
            cycle_nodes.update(path)
            # 环上每个节点各自保留同一条环路径，便于逐张核对交接证据。
            targets = sorted(set(path)) or [error.get('id', '')]
        else:
            detail = f"上级单号 {'/'.join(path) or '?'} 不存在，拆并关系未闭合"
            targets = [error.get('id', '')]
        for node in targets:
            attach(node, error.get('code', 'GRAPH_ERROR'), detail, 'grouping')
            if node in by_id and root_of.get(node) is None:
                unassigned_kind[node] = '父级缺失' if error.get('code') == 'MISSING_PARENT' else '成环'
    for node in sorted(set(by_id) - set(root_of)):
        if node in cycle_nodes:
            unassigned_kind.setdefault(node, '成环')
        else:
            unassigned_kind.setdefault(node, '未归属')
        if not unassigned_causes[node]:
            _add(unassigned_causes[node], 'UNATTACHED', '拆并关系未闭合，无法归属任何主单', 'grouping')

    # 危险品：节点级问题按子树归属；整箱不相容按用箱主单归属。
    container_roots = defaultdict(set)
    for root in roots:
        for node in members[root]:
            container_roots[by_id.get(node, {}).get('container', '')].add(root)
    for issue in state.get('hazard_issues', []):
        code = issue.get('code', 'HAZARD_ISSUE')
        if code == 'INCOMPATIBLE':
            container = issue.get('id', '')
            detail = f"箱号 {container} 同箱混装 {issue.get('classes', '?')} 类危险品"
            owners = sorted(container_roots.get(container, set()))
            if owners:
                for root in owners:
                    _add(causes[root], code, detail, 'hazard')
            else:
                _add(unassigned_causes[container], code, detail, 'hazard')
                unassigned_kind.setdefault(container, '危险品')
        else:
            node = issue.get('id', '')
            if code == 'UNKNOWN_HAZARD':
                category = by_id.get(node, {}).get('hazard', '')
                detail = f"危险类 {category} 未在许可目录，需复核"
            else:
                category = by_id.get(node, {}).get('hazard', '')
                detail = f"危险类 {category} 缺有效 UN 编号"
            attach(node, code, detail, 'hazard')

    # 件重：只把每个节点的直接子级合计与该节点申报值比对后的差异带入。
    for check in state.get('weight_checks', []):
        if check.get('code') == 'BALANCED':
            continue
        node = check.get('id', '')
        detail = (f"{check.get('field', '')} 申报 {check.get('declared', '')} 与子单合计 "
                  f"{check.get('children_sum', '')} 相差 {check.get('difference', '')}")
        attach(node, 'WEIGHT_MISMATCH', detail, 'weight')

    # 采信隔离：能挂到主单的按主单，其余作为未归属单据阻断。
    for item in state.get('quarantine', []):
        raw = item.get('raw', {})
        node = item.get('id', '')
        parent = raw.get('parent', '')
        root = root_of.get(node) or (node if node in roots else None) or root_of.get(parent) or (parent if parent in roots else None)
        detail = f"{item.get('code', '')}：{item.get('detail', '')}（原始行 {item.get('line', '?')}）"
        if root is not None:
            _add(causes[root], item.get('code', 'QUARANTINE'), detail, 'intake')
        else:
            _add(unassigned_causes[node], item.get('code', 'QUARANTINE'), detail, 'intake')
            unassigned_kind.setdefault(node, '采信隔离')

    cutoff_at, expired = _window(state, as_of)
    totals = state.get('balance', {})
    context = state.get('context', {})

    rows = []
    for root in roots:
        bucket = causes[root]
        disposition = ENTER
        if bucket:
            disposition = min((_severity(c['code']) for c in bucket), key=lambda s: SEVERITY[s])
        if disposition == HOLD and expired:
            disposition = BLOCK
            _add(bucket, 'EXPIRED_HOLD', f"已到截关时点 {cutoff_at}，仍有未闭合事项", 'cutoff')
        bucket.sort(key=_cause_sort)
        primary = bucket[0]['code'] if bucket else 'READY'
        node_rows = [by_id[n] for n in members[root] if n in by_id]
        revision = max((int(r.get('revision', 1)) for r in node_rows), default=1)
        row = {
            'id': root,
            'kind': '主单',
            'disposition': disposition,
            'cause_code': primary,
            'cause_label': CAUSE_LABELS.get(primary, primary),
            'cause': '；'.join(c['detail'] for c in bucket) or '拆并闭合、件重对平、危险品无阻断',
            'members': sorted(members[root]),
            'member_count': len(members[root]),
            'revision_no': revision,
            'causes': bucket,
            'declared': {
                'pieces': by_id[root].get('pieces'),
                'net_kg': by_id[root].get('net_kg'),
                'gross_kg': by_id[root].get('gross_kg'),
            } if root in by_id else {},
        }
        row['evidence_ref'] = _digest({k: row[k] for k in ('id', 'disposition', 'cause_code', 'members', 'declared')})[:12]
        rows.append(row)

    unassigned = []
    for node in sorted(unassigned_causes):
        bucket = unassigned_causes[node]
        bucket.sort(key=_cause_sort)
        unassigned.append({
            'id': node,
            'kind': unassigned_kind.get(node, '未归属'),
            'disposition': BLOCK,
            'cause_code': bucket[0]['code'],
            'cause_label': CAUSE_LABELS.get(bucket[0]['code'], bucket[0]['code']),
            'cause': '；'.join(c['detail'] for c in bucket),
            'line': next((q.get('line') for q in state.get('quarantine', []) if q.get('id') == node),
                         by_id.get(node, {}).get('_line', 0)),
            'causes': bucket,
        })

    declaration = context.get('declaration', {})
    declaration_ok = not totals.get('declaration_differences')
    input_ok = bool(totals.get('row_balanced'))
    gates = {
        'input_conservation': {
            'status': '通过' if input_ok else '失衡',
            'input': totals.get('input'),
            'accounted': totals.get('accepted', 0) + totals.get('quarantined', 0) + state.get('counts', {}).get('blank', 0)
                         + state.get('counts', {}).get('duplicate', 0) + state.get('counts', {}).get('repeated_header', 0),
        },
        'declaration': {
            'status': '一致' if declaration_ok else '差异',
            'differences': list(totals.get('declaration_differences', [])),
            'declared': declaration,
            'totals': {k: totals.get(k) for k in ('pieces', 'net_kg', 'gross_kg')},
        },
        'unassigned': {'status': '无' if not unassigned else '存在', 'count': len(unassigned)},
        'cutoff': {'cutoff_at': cutoff_at, 'as_of': as_of, 'expired': expired},
    }

    counts = {
        'roots': len(roots),
        'enter': sum(r['disposition'] == ENTER for r in rows),
        'hold': sum(r['disposition'] == HOLD for r in rows),
        'block': sum(r['disposition'] == BLOCK for r in rows),
        'unassigned': len(unassigned),
    }
    if counts['roots'] == 0:
        status = '无可处置主单'
    elif (counts['enter'] == counts['roots'] and input_ok and declaration_ok and not unassigned):
        status = '可交接'
    elif counts['enter'] == counts['roots'] and input_ok:
        status = '待整批对账'
    elif counts['enter'] > 0:
        status = '部分可交接'
    else:
        status = '不可交接'
    status_class = {'可交接': 'ok', '部分可交接': 'partial', '待整批对账': 'bad',
                    '不可交接': 'bad', '无可处置主单': 'bad'}[status]

    evidence = {
        'run_id': state.get('manifest', {}).get('run_id'),
        'sha256': state.get('manifest', {}).get('sha256'),
        'source': state.get('source'),
        'rule_version': RULE_VERSION,
        'counts': state.get('counts', {}),
        'graph_errors': graph.get('errors', []),
        'weight_checks': [c for c in state.get('weight_checks', []) if c.get('code') != 'BALANCED'],
        'hazard_issues': state.get('hazard_issues', []),
        'quarantine': state.get('quarantine', []),
        'declaration': gates['declaration'],
        'roots': [{'id': r['id'], 'revision': r['revision_no'], 'members': r['members'],
                   'declared': r['declared'], 'disposition': r['disposition'], 'cause_code': r['cause_code']}
                  for r in rows],
        'unassigned': [{'id': u['id'], 'kind': u['kind'], 'cause_code': u['cause_code']} for u in unassigned],
    }

    core = {
        'run_sha256': evidence['sha256'],
        'rule_version': RULE_VERSION,
        'as_of': as_of or None,
        'counts': counts,
        'rows': rows,
        'unassigned': unassigned,
        'gates': gates,
        'evidence': evidence,
    }
    digest_value = _digest(core)
    decision = {
        'batch_id': None,
        'content_id': 'CUT-' + digest_value[:12].upper(),
        'batch_key': None,
        'decision_digest': digest_value,
        'status': status,
        'status_class': status_class,
        'counts': counts,
        'rows': rows,
        'unassigned': unassigned,
        'gates': gates,
        'evidence': evidence,
        'rule_version': RULE_VERSION,
        'as_of': as_of or None,
        'cutoff_at': cutoff_at,
        'expired': expired,
    }
    return decision


def batch_id_for(key):
    return 'CUT-' + _digest({'batch_key': key, 'rule_version': RULE_VERSION})[:12].upper()


def bind_batch(decision, state, batch_key=None):
    """把同一业务批次的多次生成归到一条批次线。

    批次键优先取显式参数，其次取上下文中的业务批次号；都没有时按源快照
    落一条稳定批次线，保证同一批重跑不会另起一套批次。
    """
    if batch_key is None:
        batch_key = state.get('context', {}).get('batch')
    if batch_key is not None and str(batch_key).strip():
        key = str(batch_key).strip()
    else:
        key = 'SRC-' + state.get('manifest', {}).get('sha256', '')[:16]
    decision['batch_key'] = key
    decision['batch_id'] = batch_id_for(key)
    return decision


def apply(state):
    decision = bind_batch(decide(state, state.get('context', {}).get('as_of')), state)
    state['cutoff'] = decision
    state['views']['cutoff'] = {
        'title': '截关处置批次',
        'rows': decision['rows'] + decision['unassigned'],
        'metrics': {'主单': decision['counts']['roots'], '进入': decision['counts']['enter'],
                    '暂缓': decision['counts']['hold'], '阻断': decision['counts']['block'],
                    '未归属': decision['counts']['unassigned']},
    }

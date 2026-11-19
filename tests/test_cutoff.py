import copy, json, sqlite3

import pytest
from fastapi.testclient import TestClient

from app import main, store
from app.engine import evaluate
from app.modules.cutoff import store as batches
from app.modules.cutoff.rules import BLOCK, ENTER, HOLD, decide
from app.modules.grouping.rules import apply as regroup
from app.modules.weight.rules import apply as reweigh


def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DB_PATH', str(tmp_path / 'cutoff.sqlite'))
    return TestClient(main.app)


def with_weight_delta(state, node='M001', field='gross_kg', value='441'):
    """改动一个节点的申报件重，并像真实重算那样重建图与逐层核验。"""
    changed = copy.deepcopy(state)
    for row in changed['records']:
        if row['id'] == node:
            row[field] = value
    regroup(changed)
    reweigh(changed)
    return changed


def test_dirty_every_root_has_disposition(sample):
    decision = decide(sample, '2026-11-19T09:00:00+08:00')
    assert [r['id'] for r in decision['rows']] == sample['graph']['roots']
    assert {r['disposition'] for r in decision['rows']} <= {ENTER, HOLD, BLOCK}
    assert all(r['cause'] and r['cause_code'] for r in decision['rows'])


def test_dirty_clean_roots_enter_hazard_root_blocked(sample):
    decision = decide(sample, '2026-11-19T09:00:00+08:00')
    by_id = {r['id']: r for r in decision['rows']}
    assert by_id['M001']['disposition'] == BLOCK
    assert by_id['M001']['cause_code'] == 'INCOMPATIBLE'
    assert by_id['M002']['disposition'] == ENTER
    assert decision['counts'] == {'roots': 6, 'enter': 5, 'hold': 0, 'block': 1, 'unassigned': 6}


def test_unclosed_graph_and_quarantine_are_blocked_not_entered(sample):
    decision = decide(sample)
    unassigned = {u['id']: u for u in decision['unassigned']}
    assert unassigned['ORPHAN']['cause_code'] == 'MISSING_PARENT'
    assert unassigned['LOOP-A']['cause_code'] == 'DEPENDENCY_CYCLE'
    assert unassigned['COLUMN']['cause_code'] == 'COLUMN_COUNT'
    assert all(u['disposition'] == BLOCK for u in decision['unassigned'])
    # 环上每个节点都带同一条环路径，不因 DFS 从哪个节点起步而漏掉。
    assert unassigned['LOOP-B']['cause_code'] == 'DEPENDENCY_CYCLE'
    assert 'LOOP-A → LOOP-B → LOOP-A' in unassigned['LOOP-B']['cause']
    assert unassigned['LOOP-B']['kind'] == '成环'


def test_weight_gap_holds_then_blocks_after_cutoff(sample):
    changed = with_weight_delta(evaluate({'dataset': 'clean'}))
    before = decide(changed, '2026-11-19T09:00:00+08:00')
    row = next(r for r in before['rows'] if r['id'] == 'M001')
    assert row['disposition'] == HOLD and row['cause_code'] == 'WEIGHT_MISMATCH'
    after = decide(changed, '2026-11-19T19:00:00+08:00')
    escalated = next(r for r in after['rows'] if r['id'] == 'M001')
    assert escalated['disposition'] == BLOCK
    assert escalated['cause_code'] == 'WEIGHT_MISMATCH'
    assert {c['code'] for c in escalated['causes']} == {'WEIGHT_MISMATCH', 'EXPIRED_HOLD'}


def test_split_change_reruns_weight_into_new_decision(clean):
    """拆并关系变化后重新产生的件重结论必须进入本批判定。"""
    regrouped = copy.deepcopy(clean)
    regrouped['records'].append(dict(regrouped['records'][0], id='M002-EXTRA', parent='M002',
                                     pieces=1, net_kg='1', gross_kg='1'))
    regroup(regrouped)
    reweigh(regrouped)
    decision = decide(regrouped, '2026-11-19T09:00:00+08:00')
    m002 = next(r for r in decision['rows'] if r['id'] == 'M002')
    assert m002['disposition'] == HOLD
    assert any(c['source'] == 'weight' for c in m002['causes'])
    # 同一批其它主单不受这次拆并变化牵连。
    assert next(r for r in decision['rows'] if r['id'] == 'M003')['disposition'] == ENTER


def test_missing_un_holds_then_blocks_after_cutoff(clean):
    changed = copy.deepcopy(clean)
    for row in changed['records']:
        if row['id'] == 'M004-P1-1':
            row['hazard'], row['un_code'] = '3', ''
    from app.modules.hazard.rules import apply as rehazard
    rehazard(changed)
    before = next(r for r in decide(changed, '2026-11-19T09:00:00+08:00')['rows'] if r['id'] == 'M004')
    assert before['disposition'] == HOLD and before['cause_code'] == 'MISSING_UN'
    after = next(r for r in decide(changed, '2026-11-19T19:00:00+08:00')['rows'] if r['id'] == 'M004')
    assert after['disposition'] == BLOCK
    assert after['cause_code'] == 'MISSING_UN'


def test_incompatible_container_blocks_every_sharing_root(clean):
    changed = copy.deepcopy(clean)
    # 同一箱号上分别挂两个主单，3 类与 5.1 类混装必须同时阻断两张主单。
    for row in changed['records']:
        if row['id'] == 'M002-P1-1':
            row['container'] = 'C001'
            row['hazard'], row['un_code'] = '5.1', 'UN1479'
        if row['id'] == 'M001-P1-2':
            row['hazard'], row['un_code'] = '3', 'UN1203'
    from app.modules.hazard.rules import apply as rehazard
    rehazard(changed)
    decision = decide(changed, '2026-11-19T09:00:00+08:00')
    by_id = {r['id']: r for r in decision['rows']}
    assert by_id['M001']['disposition'] == BLOCK
    assert by_id['M002']['disposition'] == BLOCK
    assert by_id['M003']['disposition'] == ENTER


def test_decision_is_order_and_time_independent(sample):
    a = decide(sample, '2026-11-19T09:00:00+08:00')
    b = decide(sample, '2026-11-19T09:00:00+08:00')
    assert a['decision_digest'] == b['decision_digest']
    assert a['rows'] == b['rows']


def test_gates_report_input_and_declaration(sample):
    gates = decide(sample)['gates']
    assert gates['input_conservation']['status'] == '通过'
    assert gates['input_conservation']['input'] == gates['input_conservation']['accounted']
    assert gates['declaration']['status'] == '差异'
    assert gates['declaration']['differences'] == ['gross_kg']


def test_clean_batch_fully_releasable(clean):
    decision = decide(clean, '2026-11-19T09:00:00+08:00')
    assert decision['status'] == '可交接'
    assert decision['counts'] == {'roots': 6, 'enter': 6, 'hold': 0, 'block': 0, 'unassigned': 0}


def test_generate_is_idempotent(tmp_path, sample):
    path = str(tmp_path / 'batches.sqlite')
    first = batches.generate(path, sample, batch_key='batch-A')
    again = batches.generate(path, sample, batch_key='batch-A')
    assert first['batch_id'] == again['batch_id']
    assert first['version'] == again['version'] == 1
    assert len(batches.versions(path, 'batch-A')) == 1
    assert len(batches.history(path)) == 1


def test_new_evidence_appends_version_not_new_batch(tmp_path, sample, clean):
    path = str(tmp_path / 'batches.sqlite')
    first = batches.generate(path, sample, batch_key='batch-A')
    second = batches.generate(path, clean, batch_key='batch-A')
    assert second['batch_id'] == first['batch_id']
    assert second['version'] == 2
    assert second['previous_version'] == 1
    # 版本表保留第一版原证据，可供追溯。
    rows = batches.versions(path, 'batch-A')
    assert [r['version'] for r in rows] == [2, 1]
    con = sqlite3.connect(path)
    old = con.execute('SELECT result FROM cutoff_versions WHERE batch_key=? AND version=1', ('batch-A',)).fetchone()
    con.close()
    assert json.loads(old[0])['decision_digest'] == first['decision_digest']


def test_failed_generation_restores_previous_batch(tmp_path, sample, clean):
    path = str(tmp_path / 'batches.sqlite')
    good = batches.generate(path, sample, batch_key='batch-A')

    def boom():
        raise RuntimeError('下游落盘失败')

    with pytest.raises(RuntimeError):
        batches.generate(path, clean, batch_key='batch-A', failpoint=boom)

    restored = batches.get(path, 'batch-A')
    assert restored['version'] == good['version']
    assert restored['decision_digest'] == good['decision_digest']
    assert restored['evidence'] == good['evidence']
    assert len(batches.versions(path, 'batch-A')) == 1


def test_failed_first_generation_leaves_no_half_batch(tmp_path, sample):
    path = str(tmp_path / 'batches.sqlite')

    def boom():
        raise RuntimeError('下游落盘失败')

    with pytest.raises(RuntimeError):
        batches.generate(path, sample, batch_key='batch-A', failpoint=boom)
    assert batches.history(path) == []
    with pytest.raises(Exception):
        batches.get(path, 'batch-A')


def test_api_same_batch_across_pages(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    run = c.post('/api/runs', json={}).json()
    rid = run['manifest']['run_id']
    first = c.post('/api/cutoff', json={'run_id': rid}).json()
    again = c.post('/api/cutoff', json={'run_id': rid}).json()
    assert first['batch_id'] == again['batch_id']
    assert first['version'] == again['version'] == 1
    assert len(c.get('/api/cutoff/batches').json()) == 1
    for key in main.MODULES:
        assert c.get('/' + key + '?run_id=' + rid).status_code == 200
    page = c.get('/cutoff?run_id=' + rid).text
    assert first['batch_id'] in page
    assert '截关处置批次' in page
    assert c.get('/api/cutoff/batches/' + first['batch_id']).json()['batch_id'] == first['batch_id']
    assert len(c.get('/api/cutoff/batches/' + first['batch_id'] + '/versions').json()) == 1


def test_api_regenerate_same_evidence_no_new_version(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    run = c.post('/api/runs', json={}).json()
    batch = c.post('/api/cutoff', json={'run_id': run['manifest']['run_id']}).json()
    repeated = c.post('/api/cutoff/batches/' + batch['batch_id'] + '/regenerate', json={})
    assert repeated.status_code == 200
    assert repeated.json()['version'] == batch['version']
    assert repeated.json()['batch_id'] == batch['batch_id']


def test_api_regenerate_with_new_run_appends_version(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    dirty = c.post('/api/runs', json={}).json()
    clean = c.post('/api/runs', json={'dataset': 'clean'}).json()
    batch = c.post('/api/cutoff', json={'run_id': dirty['manifest']['run_id']}).json()
    moved = c.post('/api/cutoff/batches/' + batch['batch_id'] + '/regenerate',
                   json={'run_id': clean['manifest']['run_id']}).json()
    assert moved['batch_id'] == batch['batch_id']
    assert moved['version'] == 2
    assert len(c.get('/api/cutoff/batches').json()) == 1


def test_api_preview_does_not_persist(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    preview = c.post('/api/cutoff/preview', json={})
    assert preview.status_code == 200
    assert preview.json()['counts']['roots'] == 6
    assert preview.json()['batch_id'].startswith('CUT-')
    assert c.get('/api/cutoff/batches').json() == []


def test_explicit_batch_key_groups_runs_into_one_line(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    dirty = c.post('/api/runs', json={}).json()['manifest']['run_id']
    clean = c.post('/api/runs', json={'dataset': 'clean'}).json()['manifest']['run_id']
    first = c.post('/api/cutoff', json={'run_id': dirty, 'batch_key': 'CN1126'}).json()
    second = c.post('/api/cutoff', json={'run_id': clean, 'batch_key': 'CN1126'}).json()
    assert first['batch_id'] == second['batch_id']
    assert (first['version'], second['version']) == (1, 2)
    assert len(c.get('/api/cutoff/batches').json()) == 1


def test_api_bad_batch_key(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    assert c.post('/api/cutoff', json={'batch_key': '   '}).status_code == 422
    assert c.get('/api/cutoff/batches/nope').status_code == 404


def test_export_carries_cutoff_decision(tmp_path, monkeypatch):
    import io, zipfile
    c = client(tmp_path, monkeypatch)
    run = c.post('/api/runs', json={}).json()
    rid = run['manifest']['run_id']
    batch = c.post('/api/cutoff', json={'run_id': rid}).json()
    bundle = zipfile.ZipFile(io.BytesIO(c.get('/api/export/' + rid).content))
    evidence = json.loads(bundle.read('evidence.json'))
    assert evidence['cutoff']['batch_id'] == batch['batch_id']

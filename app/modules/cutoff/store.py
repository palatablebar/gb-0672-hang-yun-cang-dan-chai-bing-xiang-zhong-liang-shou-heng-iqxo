"""截关处置批次的持久化。

同一条批次线只保留一个「当前」结论；结论变化时追加版本。版本表只增不删，
因此生成失败时可以恢复上一版，失败前后写入的证据都仍可追溯。
"""

import json, sqlite3
from pathlib import Path
from app.config import RULE_VERSION
from app.errors import BusinessError
from .rules import batch_id_for, decide

BATCH_SCHEMA = """
CREATE TABLE IF NOT EXISTS cutoff_batches (
  batch_key TEXT PRIMARY KEY,
  batch_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  run_id TEXT NOT NULL,
  decision_digest TEXT NOT NULL,
  result TEXT NOT NULL,
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS cutoff_versions (
  batch_key TEXT NOT NULL,
  version INTEGER NOT NULL,
  batch_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  decision_digest TEXT NOT NULL,
  result TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  PRIMARY KEY (batch_key, version)
);
"""


def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=15, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL')
    con.executescript(BATCH_SCHEMA)
    return con


def _dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def resolve_key(state, batch_key=None):
    """批次线的身份。

    显式批次键优先；其次取上下文里的业务批次号；都没有时按源快照散列，
    这样同一输入重跑自动落回同一条批次线，不会另起一套。
    """
    if batch_key is not None:
        key = str(batch_key).strip()
        if not key:
            raise BusinessError('BAD_CONTEXT', '批次键须为非空字符串')
        return key
    context = state.get('context', {})
    if str(context.get('batch', '')).strip():
        return str(context['batch']).strip()
    return 'SRC-' + state['manifest']['sha256'][:16]


def _write(con, prev, decision, key, batch_id):
    version = (prev['version'] + 1) if prev else 1
    record = dict(decision)
    record.update({'batch_key': key, 'batch_id': batch_id, 'version': version,
                   'previous_version': prev['version'] if prev else None,
                   'rule_version': RULE_VERSION})
    payload = _dump(record)
    run_id = record['evidence']['run_id']
    con.execute('INSERT INTO cutoff_versions(batch_key,version,batch_id,run_id,decision_digest,result) VALUES(?,?,?,?,?,?)',
                (key, version, batch_id, run_id, record['decision_digest'], payload))
    con.execute('''INSERT INTO cutoff_batches(batch_key,batch_id,version,run_id,decision_digest,result)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(batch_key) DO UPDATE SET
                     batch_id=excluded.batch_id,version=excluded.version,run_id=excluded.run_id,
                     decision_digest=excluded.decision_digest,result=excluded.result,
                     updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')''',
                (key, batch_id, version, run_id, record['decision_digest'], payload))
    return record


def generate(path, state, batch_key=None, as_of=None, failpoint=None):
    """生成或更新一条截关处置批次。

    - 结论与当前版本一致时原样返回，不新增版本（同一批重跑不另起一套）。
    - 结论变化时在同一批次线上追加版本。
    - 写入过程中任何失败都回滚到上一版，版本表保留原证据。
    """
    key = resolve_key(state, batch_key)
    decision = decide(state, as_of)
    con = connect(path)
    try:
        con.execute('BEGIN IMMEDIATE')
        # 以版本表最近一次已提交版本为准，当前行只作展示用途，不能成为证据来源。
        prev_row = con.execute('SELECT * FROM cutoff_versions WHERE batch_key=? ORDER BY version DESC LIMIT 1',
                               (key,)).fetchone()
        prev = json.loads(prev_row['result']) if prev_row is not None else None
        if prev is not None and prev_row['decision_digest'] == decision['decision_digest'] \
                and prev_row['run_id'] == decision['evidence']['run_id']:
            con.execute('ROLLBACK')
            return prev
        batch_id = prev_row['batch_id'] if prev_row is not None else batch_id_for(key)
        record = _write(con, prev, decision, key, batch_id)
        if failpoint is not None:
            failpoint()  # 模拟下游落盘失败：必须发生在提交之前。
        con.execute('COMMIT')
        return record
    except Exception:
        try:
            con.execute('ROLLBACK')
        except sqlite3.Error:
            pass
        _restore(con, key)
        raise
    finally:
        con.close()


def _restore(con, key):
    """回滚后核对当前批次确实等于最近一次已提交的版本。

    版本表只增不删，这里不会删除任何证据；若当前行意外领先于已提交版本，
    以版本表中最大已提交版本为准写回。
    """
    committed = con.execute('SELECT * FROM cutoff_versions WHERE batch_key=? ORDER BY version DESC LIMIT 1',
                            (key,)).fetchone()
    current = con.execute('SELECT * FROM cutoff_batches WHERE batch_key=?', (key,)).fetchone()
    if committed is None:
        if current is not None:
            con.execute('DELETE FROM cutoff_batches WHERE batch_key=?', (key,))
        return
    if current is not None and current['version'] == committed['version'] and current['result'] == committed['result']:
        return
    con.execute('''INSERT INTO cutoff_batches(batch_key,batch_id,version,run_id,decision_digest,result)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(batch_key) DO UPDATE SET
                     batch_id=excluded.batch_id,version=excluded.version,run_id=excluded.run_id,
                     decision_digest=excluded.decision_digest,result=excluded.result,
                     updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')''',
                (key, committed['batch_id'], committed['version'], committed['run_id'],
                 committed['decision_digest'], committed['result']))


def _find(con, batch):
    row = con.execute('SELECT * FROM cutoff_batches WHERE batch_key=?', (batch,)).fetchone()
    if row is None:
        row = con.execute('SELECT * FROM cutoff_batches WHERE batch_id=?', (batch,)).fetchone()
    return row


def get(path, batch):
    con = connect(path)
    try:
        row = _find(con, batch)
    finally:
        con.close()
    if row is None:
        raise BusinessError('NOT_FOUND', '处置批次不存在', 404)
    return json.loads(row['result'])


def versions(path, key):
    con = connect(path)
    try:
        row = _find(con, key)
        if row is None:
            raise BusinessError('NOT_FOUND', '处置批次不存在', 404)
        rows = con.execute('''SELECT version,batch_id,run_id,decision_digest,created_at
                              FROM cutoff_versions WHERE batch_key=? ORDER BY version DESC''',
                           (row['batch_key'],)).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def history(path):
    con = connect(path)
    try:
        rows = con.execute('''SELECT batch_key,batch_id,version,run_id,decision_digest,updated_at
                              FROM cutoff_batches ORDER BY updated_at DESC, batch_key''').fetchall()
    finally:
        con.close()
    return [dict(r) for r in rows]

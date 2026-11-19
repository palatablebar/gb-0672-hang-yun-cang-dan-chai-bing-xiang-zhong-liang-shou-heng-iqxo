from fastapi import APIRouter, Request, Body
from app.engine import evaluate
from app import store
from app.errors import BusinessError
from app.web import render
from . import store as batches
from .rules import bind_batch, decide

router = APIRouter()


def _db():
    from app.main import DB_PATH
    return DB_PATH


def _state(payload, persist=True):
    """整批来源：已有 run_id 则沿用其不可变快照，否则现算一次完整运行并落库。"""
    payload = payload or {}
    run_id = payload.get('run_id')
    if run_id:
        previous = store.get(_db(), str(run_id))
        if 'input_snapshot' not in previous:
            raise BusinessError('INPUT_SNAPSHOT_MISSING', '旧批缺少源快照，请重新导入')
        return evaluate({'dataset': previous['source']}, snapshot=previous['input_snapshot'])
    payload = {k: v for k, v in payload.items() if k in {'dataset', 'csv_text', 'overrides'}}
    state = evaluate(payload)
    # 批次引用运行结果，先落库才能在历史与重算里按 run_id 找回同一证据。
    return store.save(_db(), state) if persist else state


@router.get('/cutoff')
def page(request: Request):
    return render(request, 'cutoff')


@router.post('/api/cutoff')
def create(payload: dict = Body(default={})):
    state = _state(payload)
    return batches.generate(_db(), state, batch_key=payload.get('batch_key'), as_of=payload.get('as_of'))


@router.get('/api/cutoff/batches')
def batch_list():
    return batches.history(_db())


@router.get('/api/cutoff/batches/{batch}')
def batch_get(batch: str):
    return batches.get(_db(), batch)


@router.get('/api/cutoff/batches/{batch}/versions')
def batch_versions(batch: str):
    return batches.versions(_db(), batch)


@router.post('/api/cutoff/batches/{batch}/regenerate')
def batch_regenerate(batch: str, payload: dict = Body(default={})):
    """按原批次线重算：批号与批次线不变，结论变化才追加版本。"""
    current = batches.get(_db(), batch)
    run_id = payload.get('run_id') or current['evidence']['run_id']
    state = _state({'run_id': run_id})
    as_of = payload.get('as_of', current.get('as_of'))
    return batches.generate(_db(), state, batch_key=current['batch_key'], as_of=as_of)


@router.post('/api/cutoff/preview')
def preview(payload: dict = Body(default={})):
    """只看结论、不落批次，便于先核对再生成。"""
    state = _state(payload, persist=False)
    return bind_batch(decide(state, payload.get('as_of')), state, batch_key=payload.get('batch_key'))

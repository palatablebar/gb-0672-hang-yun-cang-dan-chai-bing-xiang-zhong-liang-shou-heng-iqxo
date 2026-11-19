from fastapi import Request
from fastapi.templating import Jinja2Templates
from app.config import ROOT, META, MODULES
from app.engine import evaluate
from app import store

templates=Jinja2Templates(directory=str(ROOT/'app/templates'))

def _ensure_cutoff(state):
    """ensure the run carries the cutoff view regardless of which module computed it."""
    if 'cutoff' not in state and 'cutoff' in MODULES:
        from app.modules.cutoff.rules import apply
        apply(state)
    decision = state.get('cutoff')
    if decision is not None:
        # 页面显示这条运行在批次线上的落位：同结论则显示已生成版本，否则提示尚待生成。
        from app.modules.cutoff import store as batches
        from app.errors import BusinessError
        from app.main import DB_PATH
        decision.setdefault('version', None)
        try:
            persisted = batches.get(DB_PATH, decision['batch_key'])
        except BusinessError:
            persisted = None
        if persisted and persisted.get('decision_digest') == decision['decision_digest']:
            decision['version'] = persisted['version']
            decision['batch_status'] = '已入批'
        elif persisted:
            decision['latest_version'] = persisted['version']
            decision['batch_status'] = '待生成'
        else:
            decision['batch_status'] = '待生成'
    return state

def render(request:Request,key):
    from app.main import DB_PATH
    run_id=request.query_params.get('run_id')
    if run_id:
        state=store.get(DB_PATH,run_id)
    else:
        state=store.save(DB_PATH,evaluate({'dataset':request.query_params.get('dataset','dirty')}))
    state=_ensure_cutoff(state)
    return templates.TemplateResponse(request=request,name=f'{key}.html',context={'meta':META,'key':key,'state':state,'view':state['views'][key]})

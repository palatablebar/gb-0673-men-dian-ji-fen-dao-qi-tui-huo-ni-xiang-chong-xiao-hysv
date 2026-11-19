from fastapi.testclient import TestClient
from app import main
from app.engine import evaluate
import pytest

def client(tmp_path,monkeypatch):
    monkeypatch.setattr(main,'DB_PATH',str(tmp_path/'test.sqlite'));return TestClient(main.app)

def test_provisional_accept_keeps_settlement(sample):
    # 同号两来源冲突：先到行暂行采信，结算不停摆
    assert sample['archive']['status']=='可归档'
    dup=next(r for r in sample['records'] if r['id']=='DUP')
    assert (dup['_line'],dup['member'],dup['points'])==(53,'00003',1)
    assert sample['balance']['spent']==731

def test_every_decision_traceable(sample):
    # 每条接受、替代或待核记录都能追到原始行和依据
    for row in sample['attribution']:
        assert row['decision'] in ('接受','替代','待核')
        assert isinstance(row['line'],int) and row['line']>=1
        assert row['basis'] and row['publish']
    pending={(q['id'],q['line']) for q in sample['quarantine'] if q['code'] in ('DUPLICATE_CONFLICT','SUPERSEDED')}
    decided={(x['id'],x['line']) for x in sample['attribution'] if x['decision'] in ('替代','待核')}
    assert pending==decided
    accepted={x['id'] for x in sample['attribution'] if x['decision']=='接受'}
    assert {r['id'] for r in sample['records']}<=accepted

def test_retry_keeps_balances():
    # 同输入重放：归属与依赖余额不改换
    a,b=evaluate(),evaluate()
    assert a['attribution']==b['attribution']
    assert a['archive']['balances']==b['archive']['balances']
    assert a['manifest']['run_id']==b['manifest']['run_id']

def test_attribution_replace_creates_new_run(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch)
    original=c.post('/api/runs',json={}).json();rid=original['manifest']['run_id']
    decided=c.post(f'/api/runs/{rid}/attribution',json={'id':'DUP','line':54,'revision':1})
    assert decided.status_code==200
    new=decided.json();nrid=new['manifest']['run_id']
    assert nrid!=rid and new['manifest']['derived_from']==rid
    dup=[x for x in new['attribution'] if x['id']=='DUP']
    assert [(x['decision'],x['line'],x['publish']) for x in dup]==[('接受',54,'已发布'),('替代',53,'已替代')]
    balances={r['member']:r['remaining'] for r in new['archive']['balances']}
    assert balances['00003']==70 and balances['00004']==68
    # 旧版结果保留：原批次余额与依据不被覆盖
    old=c.get(f'/api/runs/{rid}').json()
    assert old['balance']['remaining']==1009
    assert [x['decision'] for x in old['attribution'] if x['id']=='DUP']==['接受','待核']
    # 新批次重试后余额不改换
    again=c.post(f'/api/runs/{nrid}/rerun',json={}).json()
    assert again['manifest']['run_id']==nrid and again['archive']['balances']==new['archive']['balances']
    # 同一决定重复提交幂等，不产生第三批
    repeat=c.post(f'/api/runs/{rid}/attribution',json={'id':'DUP','line':54,'revision':1}).json()
    assert repeat['manifest']['run_id']==nrid and len(c.get('/api/runs').json())==2

def test_attribution_invalid_keeps_old_run(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch)
    rid=c.post('/api/runs',json={}).json()['manifest']['run_id']
    for payload,code in [({'id':'DUP','line':999,'revision':1},'ATTRIBUTION_INVALID'),({'id':'E2','line':2,'revision':1},'ATTRIBUTION_INVALID'),({'id':'DUP','line':54,'revision':9},'STALE_VERSION'),({'line':54,'revision':1},'BAD_ATTRIBUTION')]:
        response=c.post(f'/api/runs/{rid}/attribution',json=payload)
        assert response.status_code in (409,422) and response.json()['code']==code
    assert len(c.get('/api/runs').json())==1
    assert c.get(f'/api/runs/{rid}').json()['balance']['remaining']==1009

def test_failed_run_preserves_previous(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch)
    rid=c.post('/api/runs',json={}).json()['manifest']['run_id']
    assert c.post('/api/runs',json={'csv_text':'id,member\ntotally,broken'}).status_code==422
    history=c.get('/api/runs').json()
    assert len(history)==1 and history[0]['id']==rid
    assert c.get(f'/api/runs/{rid}').json()['manifest']['sha256']

def test_export_contains_attribution(tmp_path,monkeypatch):
    import zipfile,io,json
    c=client(tmp_path,monkeypatch)
    rid=c.post('/api/runs',json={}).json()['manifest']['run_id']
    z=zipfile.ZipFile(io.BytesIO(c.get(f'/api/export/{rid}').content))
    evidence=json.loads(z.read('evidence.json'))
    assert any(x['id']=='DUP' and x['decision']=='待核' for x in evidence['attribution'])

def test_intake_page_shows_only_attribution(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch)
    html=c.get('/intake').text
    for text in ['事件归属','归属','依据','发布状态','暂行发布','待核']:
        assert text in html
    for text in ['会员余额','失败事件','原始证据']:
        assert text not in html

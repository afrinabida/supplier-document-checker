"""Sequential batches use isolated IDs, mocked providers, and explicit starts."""
from types import SimpleNamespace

import pytest
from pydantic import SecretStr
from streamlit.testing.v1 import AppTest

from src.gemini_config import GeminiConfig
from src.ai_graph_workflow import ai_sqlite_service
from src.minimal_json_adapter import MinimalJsonAdapter
from supplier_cases import supplier_case, mocked_gemini
from ui import ai_processing
from ui.batch_evaluation import prepare_batch, run_batch, summary_rows


def config():
    return GeminiConfig(enabled=True,api_key=SecretStr('synthetic-only'),model='gemini-synthetic')


def result(name,thread,decision='Approve',**extra):
    return 'AI-assisted',dict(thread_id=thread,input_filename=name,automated_decision=decision,
        rule_findings=([dict(rule_id='R5',status='fail',category='Reject')] if decision=='Reject' else []),**extra)


def test_batch_continues_after_exception_and_keeps_unique_ids_and_results():
    documents=[('first.pdf',b'a'),('failed.pdf',b'b'),('last.pdf',b'c')]
    batch=prepare_batch(None,documents,config());calls=[];events=[]
    def evaluate(name,data,**kwargs):
        calls.append((name,kwargs['thread_id']))
        if name=='failed.pdf':raise RuntimeError('private payload must never be displayed')
        return result(name,kwargs['thread_id'],'Reject' if name=='last.pdf' else 'Approve')
    run_batch(batch,documents,config=config(),evaluate=evaluate,
        on_progress=lambda i,total,row,stage:events.append((i,row['status'],stage)))
    rows=summary_rows(batch)
    assert [r['Decision'] for r in rows]==['Approve','Processing Error','Reject']
    assert [name for name,_ in calls]==[name for name,_ in documents]
    assert len({r['Evaluation ID'] for r in rows})==3
    assert 'R5' in rows[-1]['Main reason']
    assert 'private payload' not in repr(batch)
    assert all(events[i][0]<=events[i+1][0] for i in range(len(events)-1))
    run_batch(batch,documents,config=config(),evaluate=evaluate)
    assert len(calls)==3  # Repeated starts retain errors and successful evaluations.


def test_batch_identity_and_explicit_fresh_attempt():
    docs=[('one.pdf',b'identical'),('two.pdf',b'identical')]
    first=prepare_batch(None,docs,config())
    assert prepare_batch(first,docs,config()) is first
    fresh=prepare_batch(first,docs,config(),fresh=True)
    assert {r['evaluation_id'] for r in first['documents']}.isdisjoint(
        r['evaluation_id'] for r in fresh['documents'])
    assert prepare_batch(first,[('one.pdf',b'changed')],config()) is not first
    assert prepare_batch(first,docs,config().model_copy(update={'enabled':False})) is not first


def test_error_state_is_not_a_human_review_and_state_snapshots_do_not_leak():
    docs=[('one.pdf',b'a'),('two.pdf',b'b')];batch=prepare_batch(None,docs,config())
    shared={}
    def evaluate(name,data,**kwargs):
        shared.clear()
        shared.update(thread_id=kwargs['thread_id'],input_filename=name,
            automated_decision='Needs a person to check',facts={'document':name})
        if name=='one.pdf':shared['safe_error']={'stage':'extract','code':'INVALID_RESPONSE'}
        return 'AI-assisted',shared
    run_batch(batch,docs,config=config(),evaluate=evaluate)
    first,second=batch['documents']
    assert first['status']=='Processing Error' and second['status']=='Human Review'
    assert first['state']['facts']['document']=='one.pdf'
    assert 'safe_error' not in second['state']


def test_mismatched_thread_cannot_attach_another_documents_result():
    docs=[('one.pdf',b'a')];batch=prepare_batch(None,docs,config())
    run_batch(batch,docs,config=config(),evaluate=lambda *a,**k:result('other.pdf','other-thread'))
    assert batch['documents'][0]['status']=='Processing Error'
    assert batch['documents'][0]['state'] is None


def test_changed_batch_inputs_fail_before_any_processing():
    docs=[('one.pdf',b'a')];batch=prepare_batch(None,docs,config())
    with pytest.raises(ValueError,match='inputs changed'):
        run_batch(batch,[('one.pdf',b'changed')],config=config(),evaluate=lambda *a,**k:pytest.fail('Unexpected execution'))


def test_duplicate_names_are_errors_without_blocking_other_documents():
    docs=[('duplicate.pdf',b'a'),('duplicate.pdf',b'b'),('unique.pdf',b'c')]
    batch=prepare_batch(None,docs,config());calls=[]
    def evaluate(name,data,**kwargs):
        calls.append(name)
        return result(name,kwargs['thread_id'])
    run_batch(batch,docs,config=config(),evaluate=evaluate)
    assert [r['status'] for r in batch['documents']]==['Processing Error','Processing Error','Approve']
    assert calls==['unique.pdf']


def test_deterministic_batch_reuses_existing_graph_with_separate_checkpoints(tmp_path,monkeypatch):
    from src import graph_workflow
    service=graph_workflow.sqlite_service
    database=tmp_path/'local.sqlite3'
    monkeypatch.setattr(graph_workflow,'DEFAULT_DB',database)
    monkeypatch.setattr(graph_workflow,'sqlite_service',lambda:service(database))
    data=supplier_case().pdf
    docs=[('one.pdf',data),('two.pdf',data)]
    local=config().model_copy(update={'enabled':False})
    batch=prepare_batch(None,docs,local)
    run_batch(batch,docs,config=local)
    assert len({r['evaluation_id'] for r in batch['documents']})==2
    with service(database) as saved:
        for row in batch['documents']:
            assert row['mode']=='Deterministic' and row['status']!='Processing Error'
            state=saved.inspect(row['evaluation_id'])
            assert state['input_filename']==row['filename']
            assert state['automated_assessment']['filename']==row['filename']


def test_real_graphs_persist_individual_audits_and_continue_after_invalid_pdf(tmp_path,monkeypatch):
    approved=supplier_case();rejected=supplier_case(advance='27')
    cases={c.document.metadata.document_id:c for c in (approved,rejected)}
    calls=[]
    def adapter(document):
        transport,requests=mocked_gemini(cases[document.metadata.document_id].response())
        calls.append(requests)
        return MinimalJsonAdapter(document,transport=transport)
    monkeypatch.setattr('src.minimal_json_adapter.MinimalJsonAdapter',adapter)
    monkeypatch.setattr(ai_processing,'storage_root',lambda:tmp_path/'ai')
    monkeypatch.setattr(ai_processing,'purchase_request_items',lambda frame:approved.purchase_request)
    docs=[('approved.pdf',approved.pdf),('invalid.pdf',b'not a PDF'),('rejected.pdf',rejected.pdf)]
    batch=prepare_batch(None,docs,config())
    run_batch(batch,docs,config=config(),cloud_processing_authorized=True)
    assert [r['status'] for r in batch['documents']]==['Approve','Processing Error','Reject']
    with ai_sqlite_service(tmp_path/'ai') as service:
        for row in (batch['documents'][0],batch['documents'][2]):
            state=service.inspect(row['evaluation_id'])
            assert state['input_filename']==row['filename']
            assert state['document_sha256']==row['sha256']
            assert state['automated_decision']==row['status']
            assert state['audit_events'] and state['artifacts']['assessment']
    run_batch(batch,docs,config=config(),cloud_processing_authorized=True)
    assert len(calls)==2 and all(len(requests)==1 for requests in calls)


def test_upload_uses_multiple_selection_and_queues_invalid_pdf_individually(monkeypatch):
    data=supplier_case().pdf;options=[]
    def uploader(*args,**kwargs):
        options.append(kwargs)
        return [SimpleNamespace(name='valid.pdf',file_id='valid',getvalue=lambda:data),
                SimpleNamespace(name='invalid.pdf',file_id='invalid',getvalue=lambda:b'bad')]
    monkeypatch.setattr('streamlit.file_uploader',uploader)
    app=AppTest.from_string('from ui.upload import render\nrender()').run()
    assert not app.exception and options[0]['accept_multiple_files'] is True
    assert app.error and not next(b for b in app.button if b.label=='Continue to Processing').disabled
    next(b for b in app.button if b.label=='Queue selected documents').click().run()
    assert len(app.session_state['pending_documents'])==2


def test_processing_batch_summary_and_widget_rerenders_do_not_rerun(monkeypatch):
    data=supplier_case().pdf;calls=[]
    monkeypatch.setenv('ENABLE_GEMINI_EXTRACTION','true')
    monkeypatch.setenv('GEMINI_API_KEY','synthetic-only')
    monkeypatch.setattr(ai_processing,'render_run',lambda thread:None)
    def evaluate(name,data,**kwargs):
        calls.append((name,kwargs['thread_id']))
        if name=='failed.pdf':raise RuntimeError('do not expose this payload')
        return result(name,kwargs['thread_id'],'Reject' if name=='rejected.pdf' else 'Approve')
    monkeypatch.setattr('ui.batch_evaluation.start_evaluation',evaluate)
    app=AppTest.from_string('from ui.processing import render\nrender()')
    app.session_state['pending_documents']=[(name,data) for name in ('approved.pdf','failed.pdf','rejected.pdf')]
    app.run(timeout=30)
    assert calls==[]
    app.checkbox(key='ai_cloud_consent').check().run()
    next(b for b in app.button if b.label=='Start Evaluation').click().run(timeout=30)
    assert not app.exception and len(calls)==3
    table=next(d.value for d in app.dataframe if 'Evaluation ID' in d.value.columns)
    assert list(table['Decision'])==['Approve','Processing Error','Reject']
    first_ids=list(table['Evaluation ID'])
    app.run()
    next(b for b in app.button if b.label=='Start Evaluation').click().run()
    assert len(calls)==3
    app.checkbox(key='ai_fresh_attempt').check().run()
    assert len(calls)==3
    next(b for b in app.button if b.label=='Start Evaluation').click().run()
    assert len(calls)==6 and set(first_ids).isdisjoint(thread for _,thread in calls[3:])

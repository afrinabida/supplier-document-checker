"""Real UI/graph integration, synthetic bytes and mocked Gemini transport only."""
from hashlib import sha256
from pathlib import Path
import json
import pytest
from pydantic import SecretStr
from streamlit.testing.v1 import AppTest
from supplier_cases import supplier_case, mocked_gemini
from ui import private_fixtures as private
from ui import ai_processing
from ui.evaluation_workflow import start_evaluation
from src.gemini_config import GeminiConfig
from src.minimal_json_adapter import MinimalJsonAdapter


@pytest.fixture
def setup_ui(tmp_path,monkeypatch):
    case=supplier_case()
    monkeypatch.setattr(ai_processing,'storage_root',lambda:tmp_path/'ai')
    for key in ('SUPPLIER_MANUAL_EVALUATION','SUPPLIER_LOCAL_ONLY','SUPPLIER_FROZEN_ENGINE_SHA256'):
        monkeypatch.delenv(key,raising=False)
    config=GeminiConfig(enabled=True,api_key=SecretStr('synthetic-not-a-key'),model='gemini-synthetic',max_retries=0)
    return case,config


def transport(monkeypatch,payload,status=200):
    provider,calls=mocked_gemini(payload,status=status)
    class Adapter(MinimalJsonAdapter):
        def __init__(self,document,**kwargs):
            super().__init__(document,transport=provider)
    monkeypatch.setattr('src.minimal_json_adapter.MinimalJsonAdapter',Adapter)
    return calls


def enable_manual(monkeypatch):
    from src.ai_graph_workflow import implementation_hash
    monkeypatch.setenv('SUPPLIER_MANUAL_EVALUATION','true')
    monkeypatch.setenv('SUPPLIER_LOCAL_ONLY','1')
    monkeypatch.setenv('SUPPLIER_FROZEN_ENGINE_SHA256',implementation_hash())


def run_ui(state):
    app=AppTest.from_string('from ui.ai_processing import render_run\nimport streamlit as st\nrender_run(st.session_state["thread"])')
    app.session_state['thread']=state['thread_id']
    app.run(timeout=30)
    assert not app.exception
    return app


def test_authorized_synthetic_upload_can_be_queued(setup_ui):
    case,_=setup_ui
    app=AppTest.from_string('import streamlit as st\nfrom ui.upload import render_documents\nrender_documents(st.session_state["documents"])')
    app.session_state['documents']=[('authorized.pdf',case.pdf)]
    app.run(timeout=30)
    assert not app.exception and not app.error
    next(b for b in app.button if b.label=='Queue selected documents').click().run()
    assert app.session_state['pending_documents']==[('authorized.pdf',case.pdf)]


@pytest.mark.parametrize('kind',['valid','unsupported','provider','pydantic','evidence','identity','json','mapping'])
def test_real_ui_route_safe_diagnostics_and_review(setup_ui,monkeypatch,kind):
    case,config=setup_ui
    payload=case.response();status=200
    if kind=='provider':status=504
    elif kind=='unsupported':payload['line_items'][0]['quantity']['v']=['99999']
    elif kind=='pydantic':payload['supplier']['registrations'][0]['value']='invalid-scalar'
    elif kind=='evidence':payload['supplier']['name']['e'][0]['q']='Invented unsupported source quote'
    elif kind=='identity':payload['document_id']='sha256:'+'0'*64
    elif kind=='json':payload='not JSON'
    elif kind=='mapping':
        def broken_mapping(*args,**kwargs):
            raise TypeError('synthetic private mapping detail')
        monkeypatch.setattr('src.gemini_minimal_contract.SemanticEvidence',broken_mapping)
    calls=transport(monkeypatch,payload,status)
    # Real CSV loader/converter is deliberately retained at this UI boundary.
    mode,state=start_evaluation('authorized.pdf',case.pdf,config=config,batch='synthetic',cloud_processing_authorized=True)
    assert mode=='AI-assisted' and len(calls)==1
    assert calls[0]['generationConfig']['responseMimeType']=='application/json'
    assert not calls[0].get('tools') and 'responseJsonSchema' not in calls[0]['generationConfig']
    assert state['human_review_status']=='pending' and state['interrupts']
    app=run_ui(state)
    errors=' '.join(e.value for e in app.error)
    expected={'provider':'TIMEOUT','pydantic':'RESPONSE_PYDANTIC','evidence':'SOURCE_QUOTE',
        'identity':'DOCUMENT_IDENTITY','json':'JSON_SYNTAX','mapping':'DOMAIN_MAPPING'}
    if kind in ('valid','unsupported'):
        assert state['extraction_status']=='SCHEMA_VALIDATED' and state['trusted_facts_status']=='BUILT'
        assert state['automated_decision']=='Needs a person to check' and not app.error
        assert any('Purchase request comparison' in e.value for e in app.markdown)
        if kind=='unsupported':
            assert any(f['path']=='line_items[0].quantity' for f in state['evidence_summary']['unresolved'])
    else:
        assert expected[kind] in errors and 'successful supplier assessment' in errors
        assert 'assessment' not in state['artifacts'] and state['trusted_facts_status']=='NOT_STARTED'
        assert 'Invented unsupported source quote' not in errors and 'invalid-scalar' not in errors


def test_positive_ai_ui_decision_with_independent_synthetic_pr(setup_ui,monkeypatch):
    case,config=setup_ui
    # Separate positive control; real PR is never rewritten.
    monkeypatch.setattr(ai_processing,'purchase_request_items',lambda frame:case.purchase_request)
    calls=transport(monkeypatch,case.response())
    _,state=start_evaluation('positive.pdf',case.pdf,config=config,batch='positive',cloud_processing_authorized=True)
    assert len(calls)==1 and state['final_disposition']=='Approve'
    app=run_ui(state)
    assert not app.error


@pytest.mark.parametrize('gate',['off','no-freeze','wrong-freeze','not-local','frozen'])
def test_manual_gate_uses_only_synthetic_fingerprints(setup_ui,monkeypatch,gate):
    case,config=setup_ui
    monkeypatch.setattr(private,'PRIVATE_PDF_SHA256',frozenset([sha256(case.pdf).hexdigest()]))
    if gate!='off':enable_manual(monkeypatch)
    if gate=='no-freeze':monkeypatch.delenv('SUPPLIER_FROZEN_ENGINE_SHA256')
    if gate=='wrong-freeze':monkeypatch.setenv('SUPPLIER_FROZEN_ENGINE_SHA256','0'*64)
    if gate=='not-local':monkeypatch.delenv('SUPPLIER_LOCAL_ONLY')
    calls=transport(monkeypatch,case.response())
    if gate!='frozen':
        with pytest.raises(ValueError,match='private evaluation'):
            start_evaluation('renamed.pdf',case.pdf,config=config,batch='blocked',cloud_processing_authorized=True)
        assert calls==[]
    else:
        _,state=start_evaluation('renamed.pdf',case.pdf,config=config,batch='manual',cloud_processing_authorized=True)
        assert len(calls)==1 and state['extraction_status']=='SCHEMA_VALIDATED'
        # Public byte downloads remain blocked even with manual mode enabled.
        with pytest.raises(ValueError):private.require_public_document(case.pdf,'renamed.pdf')


def test_manual_upload_preview_has_no_download_or_batch(setup_ui,monkeypatch):
    case,_=setup_ui;enable_manual(monkeypatch)
    monkeypatch.setattr(private,'PRIVATE_PDF_SHA256',frozenset([sha256(case.pdf).hexdigest()]))
    app=AppTest.from_string('import streamlit as st\nfrom ui.upload import render_documents\nrender_documents(st.session_state["documents"])')
    app.session_state['documents']=[('manual.pdf',case.pdf)]
    app.run(timeout=30)
    assert not app.exception and not app.error and not app.get('download_button')
    app.session_state['documents']=[('manual.pdf',case.pdf),('another.pdf',case.pdf)]
    app.run()
    assert app.error and next(b for b in app.button if b.label=='Continue to Processing').disabled


def test_processing_screen_blocks_stale_private_batch(setup_ui,monkeypatch):
    case,_=setup_ui;enable_manual(monkeypatch)
    monkeypatch.setattr(private,'PRIVATE_PDF_SHA256',frozenset([sha256(case.pdf).hexdigest()]))
    monkeypatch.setenv('ENABLE_GEMINI_EXTRACTION','true')
    monkeypatch.setenv('GEMINI_API_KEY','synthetic-not-a-key')
    app=AppTest.from_string('from ui.processing import render\nrender()')
    app.session_state['pending_documents']=[('one.pdf',case.pdf),('two.pdf',case.pdf)]
    app.run(timeout=30)
    assert not app.exception
    assert next(b for b in app.button if b.label=='Start Evaluation').disabled


def test_start_button_runs_ai_only_after_consent(setup_ui,monkeypatch):
    case,_=setup_ui
    monkeypatch.setenv('ENABLE_GEMINI_EXTRACTION','true')
    monkeypatch.setenv('GEMINI_MODEL','gemini-synthetic')
    monkeypatch.setenv('GEMINI_API_KEY','synthetic-not-a-key')
    calls=transport(monkeypatch,case.response())
    app=AppTest.from_string('from ui.processing import render\nrender()')
    app.session_state['pending_documents']=[('uploaded.pdf',case.pdf)]
    app.run(timeout=30)
    assert next(b for b in app.button if b.label=='Start Evaluation').disabled and calls==[]
    app.checkbox(key='ai_cloud_consent').check().run()
    next(b for b in app.button if b.label=='Start Evaluation').click().run(timeout=30)
    assert not app.exception and not app.error and len(calls)==1
    assert app.session_state['ai_thread_ids']
    app.run()
    assert len(calls)==1  # Inspection/rerun never generates again.


def test_manual_evaluation_never_uses_deterministic_route(setup_ui,monkeypatch):
    case,config=setup_ui;enable_manual(monkeypatch)
    monkeypatch.setattr(private,'PRIVATE_PDF_SHA256',frozenset([sha256(case.pdf).hexdigest()]))
    with pytest.raises(ValueError,match='AI-assisted'):
        start_evaluation('manual.pdf',case.pdf,config=config.model_copy(update={'enabled':False}),batch='no-legacy')


@pytest.mark.parametrize('manual',[False,True])
def test_launcher_keeps_manual_server_loopback_without_static_assets(monkeypatch,manual,tmp_path):
    import launch_local
    from types import SimpleNamespace
    commands=[]
    monkeypatch.setattr(launch_local,'load_local_env',lambda **kwargs:None)
    monkeypatch.setattr(launch_local,'configuration_summary',lambda environment:{})
    # This test covers launch arguments, not runtime lifecycle. The development
    # guard intentionally blocks the production isolated-runtime prefix.
    monkeypatch.setattr(launch_local,'prepare_runtime',lambda:tmp_path/'synthetic-runtime')
    monkeypatch.setattr(launch_local,'claim_runtime',lambda path:path)
    monkeypatch.setattr(launch_local.subprocess,'run',lambda command,**kwargs:(commands.append((command,kwargs)) or SimpleNamespace(returncode=0)))
    if manual:monkeypatch.setenv('SUPPLIER_MANUAL_EVALUATION','true')
    else:monkeypatch.delenv('SUPPLIER_MANUAL_EVALUATION',raising=False)
    assert launch_local.main(['--isolated','--port','8502'])==0
    command,options=commands[0]
    assert command[command.index('--server.address')+1]=='127.0.0.1'
    assert command[command.index('--server.enableStaticServing')+1]=='false'
    assert options['env']['SUPPLIER_LOCAL_ONLY']=='1'


def test_manual_launcher_rejects_public_server_override(monkeypatch):
    import launch_local
    monkeypatch.setenv('SUPPLIER_MANUAL_EVALUATION','true')
    monkeypatch.setattr(launch_local,'load_local_env',lambda **kwargs:None)
    monkeypatch.setattr(launch_local.subprocess,'run',lambda *a,**k:pytest.fail('Server must not start'))
    assert launch_local.main(['--server.address','0.0.0.0'])!=0


def test_development_access_guard_still_blocks_after_manual_gate(setup_ui,monkeypatch,tmp_path):
    enable_manual(monkeypatch)
    from src.development_isolation import EvaluationAccessError
    with pytest.raises(EvaluationAccessError):
        (tmp_path/'supplier-pack-01.pdf').read_bytes()


def test_no_discovery_or_label_access_in_manual_gate(setup_ui,monkeypatch):
    enable_manual(monkeypatch)
    monkeypatch.setattr(Path,'glob',lambda *a,**k:pytest.fail('No file discovery permitted'))
    monkeypatch.setattr(Path,'rglob',lambda *a,**k:pytest.fail('No file discovery permitted'))
    assert private.manual_evaluation_enabled()


def test_diagnostics_do_not_display_arbitrary_error_payloads():
    from ui.ai_diagnostics import failure_details
    details=failure_details({'safe_error':{'stage':'secret-content','code':'secret-content'}},
        {'extraction':{'errors':[{'diagnostic':{'safe_message':'secret-content'},
            'validation_diagnostic':{'stage':'secret-content'}}]}})
    assert 'secret-content' not in json.dumps(details)


@pytest.mark.parametrize('status,category,provider_status',[
    (400,'INVALID_REQUEST','INVALID_ARGUMENT'),
    (503,'PROVIDER_UNAVAILABLE','UNAVAILABLE'),
    (504,'TIMEOUT','DEADLINE_EXCEEDED')])
def test_provider_status_categories_are_preserved_without_raw_messages(status,category,provider_status):
    from ui.ai_diagnostics import failure_details
    diagnostic=dict(http_status=status,category=category,provider_status=provider_status,
        detail='PROVIDER_ERROR',safe_message='synthetic-secret-never-display')
    details=failure_details({'safe_error':{'stage':'extract','code':'INVALID_REQUEST'}},
        {'extraction':{'errors':[{'diagnostic':diagnostic}]}})
    assert details['http_status']==status and details['category']==category
    assert 'synthetic-secret' not in json.dumps(details)

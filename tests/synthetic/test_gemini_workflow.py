"""Real ingestion, SDK serialization, strict parsing, verifier and fixed graph.

Only the HTTP transport is mocked. Expected decisions are authored from policy.
No original PDFs, labels or older test fixtures are imported.
"""
import json
import pytest
from pydantic import SecretStr

from src.ai_assisted import run_ai_assisted
from src.ai_graph_workflow import ai_sqlite_service
from src.ai_graph_models import TrustedReviewer
from src.gemini_config import GeminiConfig
from supplier_cases import supplier_case,mocked_gemini


def execute(tmp_path,case,*,payload=None,status=200):
    transport,calls=mocked_gemini(case.response() if payload is None else payload,status=status)
    config=GeminiConfig(enabled=True,api_key=SecretStr('synthetic-only-not-a-credential'),
        model='gemini-synthetic-offline',max_retries=0)
    state=run_ai_assisted(case.pdf,case.purchase_request,thread_id='synthetic-development',
        storage_root=tmp_path,config=config,cloud_processing_authorized=True,
        transport=transport,filename='independent-development.pdf')
    return state,calls


@pytest.mark.parametrize('layout',['ledger','reordered','multipage'])
@pytest.mark.parametrize('multiple',[False,True])
def test_independent_layouts_use_gemini_then_verified_rules(tmp_path,layout,multiple,monkeypatch):
    import src.extractor
    import src.rule_engine
    def forbidden(*args,**kwargs): raise AssertionError('Legacy/template extraction was invoked')
    monkeypatch.setattr(src.extractor,'extract_fields',forbidden)
    monkeypatch.setattr(src.rule_engine,'_complete_sample_layout',forbidden)
    case=supplier_case(layout=layout,multiple=multiple)
    state,calls=execute(tmp_path,case)
    assert state['safe_error'] is None
    assert state['extraction_status']=='SCHEMA_VALIDATED'
    assert state['trusted_facts_status']=='BUILT'
    assert state['final_disposition']=='Approve',state['review_reasons']
    assert len(calls)==1
    options=calls[0]['generationConfig']
    assert options['responseMimeType']=='application/json'
    assert 'responseJsonSchema' not in options and 'responseSchema' not in options
    assert not calls[0].get('tools')


@pytest.mark.parametrize('options,expected,rule',[
    ({'price':'7.50'},'Reject','R1'),
    ({'price':'7.30'},'Needs a person to check','H1'),
    ({'quantity':'6'},'Needs a person to check','H2'),
    ({'payment_days':'85'},'Needs a person to check','H3'),
    ({'vat':'12'},'Needs a person to check','H5'),
    ({'advance':'25'},'Reject','R2'),
    ({'delivery':'47'},'Reject','R5'),
    ({'explicit_no_penalty':True},'Reject','R3'),
])
def test_policy_outcomes_from_independent_synthetic_facts(tmp_path,options,expected,rule):
    state,calls=execute(tmp_path,supplier_case(**options))
    assert state['automated_decision']==expected
    assert any(r['rule_id']==rule and r['status'] in ('fail','unknown') for r in state['rule_findings'])
    assert len(calls)==1
    if expected=='Needs a person to check':
        assert state['interrupts'] and state['final_disposition'] is None


@pytest.mark.parametrize('kind',['missing-supplier','ambiguous-supplier','wrong-identity',
    'fabricated-quote','unsupported-value','missing-penalty','missing-registration','missing-items','malformed','missing-group'])
def test_untrusted_or_incomplete_output_never_approves(tmp_path,kind,monkeypatch):
    import src.rule_engine
    monkeypatch.setattr(src.rule_engine,'_complete_sample_layout',lambda *a:pytest.fail('Template guard forbidden'))
    case=supplier_case();data=case.response()
    if kind=='missing-supplier': data['supplier']['name']=None
    elif kind=='ambiguous-supplier': data['supplier']['name'].update(s='AMBIGUOUS',r='Supplier ownership is unresolved.')
    elif kind=='wrong-identity': data['document_id']='sha256:'+'0'*64
    elif kind=='fabricated-quote': data['line_items'][0]['unit_price']['e'][0]['q']='Invented evidence not in this document'
    elif kind=='unsupported-value': data['line_items'][0]['quantity']['v']=['99999']
    elif kind=='missing-penalty': data['penalties']=[]
    elif kind=='missing-registration': data['supplier']['registrations']=[]
    elif kind=='missing-items': data['line_items']=[]
    elif kind=='missing-group': del data['delivery']
    state,calls=execute(tmp_path,case,payload='not JSON' if kind=='malformed' else data)
    assert state['automated_decision']=='Needs a person to check'
    assert state['final_disposition'] is None and state['interrupts'] and len(calls)==1
    if kind in ('wrong-identity','fabricated-quote','malformed','missing-group'):
        assert state['safe_error']['stage']=='extract'
        assert state['trusted_facts_status']=='NOT_STARTED' and 'assessment' not in state['artifacts']
    else:
        assert state['safe_error'] is None
    if kind in ('missing-penalty','missing-registration'):
        rule='R3' if kind=='missing-penalty' else 'R4'
        assert all(r['status']!='fail' for r in state['rule_findings'] if r['rule_id']==rule)


def test_http_504_is_single_attempt_failure_without_legacy_fallback(tmp_path,monkeypatch):
    import src.extractor
    monkeypatch.setattr(src.extractor,'extract_fields',lambda *a:pytest.fail('Fallback forbidden'))
    state,calls=execute(tmp_path,supplier_case(),status=504)
    assert len(calls)==1 and state['safe_error']=={'stage':'extract','code':'TIMEOUT'}
    assert state['trusted_facts_status']=='NOT_STARTED' and not state['rule_findings']
    assert 'assessment' not in state['artifacts'] and state['final_disposition'] is None
    assert state['interrupts']
    with ai_sqlite_service(tmp_path) as service:
        error=service.review_materials(state['thread_id'],reviewer=TrustedReviewer(subject='synthetic',authorized=True))
    # Provider diagnostics remain in the extraction artifact, not an invented result.
    extraction=error['extraction']
    assert extraction['attempts']==1
    diagnostic=extraction['errors'][0]['diagnostic']
    assert diagnostic['http_status']==504 and diagnostic['category']=='TIMEOUT'
    assert diagnostic['timeout_origin']=='PROVIDER_HTTP'


def test_review_restart_and_resume_never_regenerates_claims(tmp_path):
    case=supplier_case(payment_days='85')
    state,calls=execute(tmp_path,case)
    assert state['next_nodes']==['human_review']
    payload={key:state[key] for key in ('thread_id','workflow_id','document_sha256','review_token')}
    payload.update(submission_id='synthetic-correction',action='request_correction',
        rationale='Clarify the payment deadline before procurement proceeds.')
    with ai_sqlite_service(tmp_path) as service:
        restored=service.inspect(state['thread_id'])
        assert restored['automated_decision']==state['automated_decision']
        final=service.resume(state['thread_id'],payload,
            reviewer=TrustedReviewer(subject='synthetic-reviewer',authorized=True),current_pdf=case.pdf)
    assert len(calls)==1
    assert final['final_disposition']=='Correction requested'
    assert final['automated_decision']=='Needs a person to check'


def test_unspecified_pr_tax_basis_remains_review_not_invented(tmp_path):
    state,_=execute(tmp_path,supplier_case(tax_basis=None))
    assert state['automated_decision']=='Needs a person to check'


def test_conflicting_source_parties_remain_unresolved(tmp_path):
    state,_=execute(tmp_path,supplier_case(conflicting_supplier=True))
    assert state['safe_error'] is None
    assert state['automated_decision']=='Needs a person to check'
    assert state['evidence_summary']['conflicts'] or state['evidence_summary']['unresolved']


def test_reject_precedence_survives_other_ambiguous_facts(tmp_path):
    case=supplier_case(price='7.50');data=case.response()
    data['supplier']['name'].update(s='AMBIGUOUS',r='Source party requires review.')
    state,_=execute(tmp_path,case,payload=data)
    assert state['automated_decision']=='Reject'
    assert any(r['rule_id']=='R1' and r['status']=='fail' for r in state['rule_findings'])


def test_all_seventeen_groups_preserved_in_minimal_contract():
    case=supplier_case()
    assert set(case.response())==set(type(case.claims).model_fields)
    assert len(case.response())==17

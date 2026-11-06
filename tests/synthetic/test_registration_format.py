"""Registration wire-format regressions using independent synthetic data only."""
import json
from dataclasses import replace
import pytest
import pymupdf

from src.ai_graph_workflow import ai_sqlite_service
from src.ai_graph_models import TrustedReviewer
from src.gemini_minimal_contract import (proposal,parse_minimal,VERSION,PROMPT_VERSION,
    REGISTRATION_OUTPUT_INSTRUCTIONS)
from src.minimal_json_adapter import MinimalJsonAdapter
from src.pdf_ingestion import ingest_pdf
from src.trusted_models import TrustedFactsResult
from src.trusted_facts import fact_at,value_at
from supplier_cases import supplier_case
from test_gemini_workflow import execute


def registration_case():
    case=supplier_case();data=case.response()
    for registration in data['supplier']['registrations']:
        quote=registration['value']['e'][0]
        registration['label']={'v':[registration['kind']],'e':[dict(quote)]}
        registration['owner_role']={'v':['Supplier'],'e':[dict(quote)]}
    return case,data


def materials(root,state):
    with ai_sqlite_service(root) as service:
        return service.review_materials(state['thread_id'],reviewer=TrustedReviewer(subject='synthetic',authorized=True))


def test_prompt_explicitly_distinguishes_registration_enum_and_claims():
    case=supplier_case();plan=proposal(case.document)
    contract=json.loads(plan['context'])['response_contract']
    assert REGISTRATION_OUTPUT_INSTRUCTIONS in contract['instructions']
    assert 'kind alone is a plain enum string' in REGISTRATION_OUTPUT_INSTRUCTIONS
    assert 'never a bare string, number, boolean or array' in REGISTRATION_OUTPUT_INSTRUCTIONS
    assert 'Missing information is null' in REGISTRATION_OUTPUT_INSTRUCTIONS
    assert 'Do not infer owner_role' in REGISTRATION_OUTPUT_INSTRUCTIONS
    shape=contract['shape']['supplier']['registrations'][0]
    assert shape=={'kind':'string','label':'claim or null','value':'claim or null','owner_role':'claim or null'}
    assert plan['version']==VERSION and plan['prompt_version']==PROMPT_VERSION
    assert MinimalJsonAdapter.contract_version==VERSION
    assert MinimalJsonAdapter.prompt_version==PROMPT_VERSION


def test_valid_registration_claims_reach_verified_trusted_facts(tmp_path):
    case,data=registration_case();state,calls=execute(tmp_path,case,payload=data)
    assert len(calls)==1 and state['safe_error'] is None
    assert state['extraction_status']=='SCHEMA_VALIDATED' and state['trusted_facts_status']=='BUILT'
    assert state['final_disposition']=='Approve'
    facts=TrustedFactsResult.model_validate(materials(tmp_path,state)['facts'])
    for index,registration in enumerate(data['supplier']['registrations']):
        prefix=f'supplier.registrations[{index}]'
        assert value_at(facts,prefix+'.label')==registration['kind']
        assert value_at(facts,prefix+'.value')==registration['value']['v'][0]
        assert value_at(facts,prefix+'.owner_role')=='supplier'
    assert state['prompt_version']==PROMPT_VERSION
    sent=json.dumps(calls[0]['contents'])
    assert 'Registration formatting:' in sent


@pytest.mark.parametrize('null_values',[False,True])
def test_null_registration_fields_stay_missing_without_coercion(tmp_path,null_values):
    case,data=registration_case()
    for registration in data['supplier']['registrations']:
        registration['label']=None;registration['owner_role']=None
        if null_values: registration['value']=None
    state,_=execute(tmp_path,case,payload=data)
    assert state['safe_error'] is None and state['trusted_facts_status']=='BUILT'
    facts=TrustedFactsResult.model_validate(materials(tmp_path,state)['facts'])
    for index in range(2):
        for leaf in (('label','value','owner_role') if null_values else ('label','owner_role')):
            fact=fact_at(facts,f'supplier.registrations[{index}].{leaf}')
            assert not fact.authoritative and not fact.raw_values and not fact.evidence
            assert fact.normalized is None
    if null_values:
        assert state['automated_decision']=='Needs a person to check' and state['interrupts']
        assert any(r['rule_id']=='R4' and r['status']=='unknown' for r in state['rule_findings'])


@pytest.mark.parametrize('leaf',['label','value','owner_role'])
@pytest.mark.parametrize('scalar',['not-a-claim',False,42,['not-a-claim']])
def test_registration_scalars_and_arrays_are_rejected_not_wrapped(tmp_path,leaf,scalar):
    case,data=registration_case();data['supplier']['registrations'][0][leaf]=scalar
    state,_=execute(tmp_path,case,payload=data)
    assert state['safe_error']=={'stage':'extract','code':'INVALID_RESPONSE'}
    assert state['automated_decision']=='Needs a person to check' and state['interrupts']
    assert state['trusted_facts_status']=='NOT_STARTED' and 'assessment' not in state['artifacts']
    error=materials(tmp_path,state)['extraction']['errors'][0]
    assert error['validation_diagnostic']['stage']=='RESPONSE_PYDANTIC'
    assert any(i['field_path']==f'$.supplier.registrations[*].{leaf}' and i['error_type']=='model_type'
        for i in error['validation_diagnostic']['issues'])


@pytest.mark.parametrize('leaf',['label','value','owner_role'])
def test_observed_registration_fields_require_evidence(tmp_path,leaf):
    case,data=registration_case();data['supplier']['registrations'][0][leaf]['e']=[]
    state,_=execute(tmp_path,case,payload=data)
    assert state['safe_error']=={'stage':'extract','code':'INVALID_RESPONSE'}
    assert state['trusted_facts_status']=='NOT_STARTED' and 'assessment' not in state['artifacts']
    assert materials(tmp_path,state)['extraction']['errors'][0]['validation_diagnostic']['stage']=='RESPONSE_PYDANTIC'


@pytest.mark.parametrize('leaf',['label','value','owner_role'])
def test_fabricated_registration_quotes_are_rejected(tmp_path,leaf):
    case,data=registration_case()
    data['supplier']['registrations'][0][leaf]['e'][0]['q']='Invented registration evidence absent from the PDF'
    state,_=execute(tmp_path,case,payload=data)
    assert state['safe_error']=={'stage':'extract','code':'INVALID_RESPONSE'}
    assert state['trusted_facts_status']=='NOT_STARTED' and state['interrupts']
    assert materials(tmp_path,state)['extraction']['errors'][0]['validation_diagnostic']['stage']=='SOURCE_QUOTE'


def test_exact_quote_does_not_verify_a_different_identifier(tmp_path):
    case,data=registration_case()
    # A real BIN value cannot be reassigned to the TIN quote/role.
    data['supplier']['registrations'][0]['value']['v']=data['supplier']['registrations'][1]['value']['v']
    state,_=execute(tmp_path,case,payload=data)
    assert state['safe_error'] is None and state['extraction_status']=='SCHEMA_VALIDATED'
    assert state['automated_decision']=='Needs a person to check'
    facts=TrustedFactsResult.model_validate(materials(tmp_path,state)['facts'])
    assert not fact_at(facts,'supplier.registrations[0].value').authoritative


def test_ambiguous_registration_claim_is_not_promoted_to_authority(tmp_path):
    case,data=registration_case()
    data['supplier']['registrations'][0]['value'].update(s='AMBIGUOUS',r='Identifier ownership is unresolved.')
    state,_=execute(tmp_path,case,payload=data)
    assert state['safe_error'] is None and state['automated_decision']=='Needs a person to check'
    facts=TrustedFactsResult.model_validate(materials(tmp_path,state)['facts'])
    assert value_at(facts,'supplier.registrations[0].value') is None


def test_conflicting_source_registration_values_remain_unresolved(tmp_path):
    case,data=registration_case()
    extra='Supplier TIN: 1357902468'
    # Transform only this independently authored synthetic document.
    with pymupdf.open() as pdf:
        page=pdf.new_page(width=1400,height=1000)
        for index,line in enumerate([*case.document.pages[0].text.splitlines(),extra]):
            page.insert_text((24,32+24*index),line,fontsize=9)
        raw=pdf.tobytes()
    document=ingest_pdf(raw,filename='synthetic-registration-conflict.pdf')
    data['document_id']=document.metadata.document_id
    claim=data['supplier']['registrations'][0]['value']
    claim['v'].append('1357902468');claim['e'].append({'p':1,'q':extra})
    claims=parse_minimal(json.dumps(data),document)
    case=replace(case,pdf=raw,document=document,claims=claims)
    state,_=execute(tmp_path,case)
    assert state['safe_error'] is None and state['automated_decision']=='Needs a person to check'
    facts=TrustedFactsResult.model_validate(materials(tmp_path,state)['facts'])
    assert value_at(facts,'supplier.registrations[0].value') is None
    assert facts.conflicts or 'supplier.registrations[0].value' in facts.unresolved_fields

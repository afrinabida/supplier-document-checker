"""Schema formatting and conservative decisions using authored fixtures only."""
import json

import pytest

from src.gemini_minimal_contract import proposal, response_model, parse_minimal
from src.trusted_facts import value_at
from src.trusted_models import TrustedFactsResult
from supplier_cases import supplier_case
from test_gemini_workflow import execute
from test_registration_format import materials


def test_request_carries_exact_local_schema_and_container_instructions(tmp_path):
    case=supplier_case()
    plan=proposal(case.document)
    contract=json.loads(plan['context'])['response_contract']
    assert contract['json_schema']==response_model().model_json_schema()
    schema=contract['json_schema']
    assert schema['additionalProperties'] is False
    assert all(d.get('additionalProperties') is False for d in schema['$defs'].values()
               if d.get('type')=='object')
    assert 'Missing observations use null claim leaves, not null containers' in contract['instructions']
    assert 'line_items[*].tax' in contract['instructions']
    assert 'payments[*].timing' in contract['instructions']
    state,calls=execute(tmp_path,case)
    assert state['safe_error'] is None and len(calls)==1
    # The actual mocked SDK request receives the same contract in JSON-only mode.
    request_text=calls[0]['contents'][0]['parts'][0]['text']
    assert json.loads(request_text)['response_contract']['json_schema']==schema
    assert 'responseJsonSchema' not in calls[0]['generationConfig']


@pytest.mark.parametrize('invalid',[None,'not-an-object',[]])
def test_extra_keys_and_invalid_nested_containers_remain_rejected(tmp_path,invalid):
    case=supplier_case();payload=case.response()
    payload.update({f'unexpected_field_{i}':'not part of the contract' for i in range(4)})
    payload['line_items'][0]['tax']=invalid
    payload['payments'][0]['timing']=invalid
    state,calls=execute(tmp_path,case,payload=payload)
    assert len(calls)==1 and state['safe_error']=={'stage':'extract','code':'INVALID_RESPONSE'}
    assert state['trusted_facts_status']=='NOT_STARTED' and 'assessment' not in state['artifacts']
    diagnostic=materials(tmp_path,state)['extraction']['errors'][0]['validation_diagnostic']
    assert diagnostic['stage']=='RESPONSE_PYDANTIC'
    issues=diagnostic['issues']
    assert sum(i['error_type']=='extra_forbidden' for i in issues)==4
    for path in ('$.line_items[*].tax','$.payments[*].timing'):
        assert any(i['field_path']==path and i['error_type']=='model_type' for i in issues)


@pytest.mark.parametrize('empty_objects',[False,True])
def test_missing_nested_claims_are_objects_without_invented_facts(tmp_path,empty_objects):
    case=supplier_case();payload=case.response()
    payload['line_items'][0]['tax']=({} if empty_objects else
        {'treatment':None,'rate_percent':None,'amount':None})
    payload['payments'][0]['timing']=({} if empty_objects else
        dict.fromkeys(('wording','calendar_date','duration','duration_unit','anchor')))
    claims=parse_minimal(json.dumps(payload),case.document)
    assert claims.line_items[0].tax.treatment.state=='MISSING'
    assert claims.payments[0].timing.duration.state=='MISSING'
    state,_=execute(tmp_path,case,payload=payload)
    assert state['safe_error'] is None and state['extraction_status']=='SCHEMA_VALIDATED'
    facts=TrustedFactsResult.model_validate(materials(tmp_path,state)['facts'])
    assert value_at(facts,'line_items[0].tax.treatment') is None
    assert value_at(facts,'payments[0].timing.duration') is None
    assert state['automated_decision']=='Needs a person to check'


@pytest.mark.parametrize('missing_code',[False,True])
def test_unverified_identity_cannot_establish_a_price_reject(tmp_path,missing_code):
    case=supplier_case(price='9.00');payload=case.response()
    if missing_code:payload['line_items'][0]['code']=None
    else:payload['purchase_reference'].update(s='AMBIGUOUS',r='Reference scope is unresolved.')
    state,_=execute(tmp_path,case,payload=payload)
    assert state['safe_error'] is None and state['automated_decision']=='Needs a person to check'
    assert any(r['rule_id']=='R1' and r['status']=='unknown' for r in state['rule_findings'])
    assert state['matching_summary']['items'][0]['status']=='UNMATCHED'


@pytest.mark.parametrize('advance,expected',[('5','Needs a person to check'),('25','Reject')])
def test_unknown_pr_tax_basis_preserves_review_and_independent_reject(tmp_path,advance,expected):
    case=supplier_case(price='9.00',advance=advance,tax_basis=None)
    state,_=execute(tmp_path,case)
    assert state['safe_error'] is None and state['automated_decision']==expected
    assert any(r['rule_id']=='R1' and r['status']=='unknown' for r in state['rule_findings'])
    if expected=='Reject':
        assert any(r['rule_id']=='R2' and r['status']=='fail' for r in state['rule_findings'])

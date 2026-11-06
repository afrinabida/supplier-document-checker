"""Exact source-scoped facts through mocked extraction, verification and rules."""
from dataclasses import replace

import pymupdf
import pytest

from src.pdf_ingestion import ingest_pdf
from src.gemini_minimal_contract import proposal, SCOPED_EVIDENCE_INSTRUCTIONS
from src.trusted_models import TrustedFactsResult
from src.trusted_facts import value_at
from supplier_cases import supplier_case
from test_gemini_workflow import execute
from test_registration_format import materials


def rewrite(case,payload,old,new):
    text=case.document.pages[0].text.replace(old,new)
    assert text!=case.document.pages[0].text
    with pymupdf.open() as pdf:
        page=pdf.new_page(width=1400,height=1500)
        for i,line in enumerate(text.splitlines()):
            page.insert_text((24,32+i*24),line,fontsize=9)
        data=pdf.tobytes()
    document=ingest_pdf(data,filename='authored-evidence.pdf')
    payload['document_id']=document.metadata.document_id
    return replace(case,pdf=data,document=document)


def trusted(root,state):
    return TrustedFactsResult.model_validate(materials(root,state)['facts'])


@pytest.mark.parametrize('days,status,decision',[('18','pass','Approve'),('52','fail','Reject'),('67','fail','Reject')])
def test_separate_exact_delivery_fragments_reach_r5(tmp_path,days,status,decision):
    case=supplier_case(delivery=days);payload=case.response()
    payload['delivery']['duration']['e']=[{'p':1,'q':days}]
    payload['delivery']['anchor']['e']=[{'p':1,'q':'PO'}]
    state,calls=execute(tmp_path,case,payload=payload)
    assert len(calls)==1 and state['safe_error'] is None
    assert value_at(trusted(tmp_path,state),'delivery.duration') is not None
    assert any(r['rule_id']=='R5' and r['status']==status for r in state['rule_findings'])
    assert state['automated_decision']==decision


def test_verified_delivery_reject_survives_missing_item_and_pr(tmp_path):
    case=supplier_case(delivery='52');payload=case.response()
    payload['line_items'][0]['code']=None
    payload['purchase_reference']=None
    payload['delivery']['duration']['e']=[{'p':1,'q':'52'}]
    payload['delivery']['anchor']['e']=[{'p':1,'q':'PO'}]
    state,_=execute(tmp_path,case,payload=payload)
    assert state['matching_summary']['items'][0]['status']=='UNMATCHED'
    assert state['automated_decision']=='Reject'
    assert any(r['rule_id']=='R5' and r['status']=='fail' for r in state['rule_findings'])
    assert value_at(trusted(tmp_path,state),'line_items[0].code') is None


@pytest.mark.parametrize('advance,status',[('13','pass'),('27','fail')])
def test_separate_exact_advance_fragments_reach_r2(tmp_path,advance,status):
    case=supplier_case(advance=advance);payload=case.response()
    payload['payments'][0]['kind']['e']=[{'p':1,'q':'advance'}]
    payload['payments'][0]['percent']['e']=[{'p':1,'q':advance+'%'}]
    state,_=execute(tmp_path,case,payload=payload)
    assert state['safe_error'] is None
    assert any(r['rule_id']=='R2' and r['status']==status for r in state['rule_findings'])
    if status=='fail':assert state['automated_decision']=='Reject'


@pytest.mark.parametrize('label',['Reference','PR reference','Purchase reference'])
def test_adjacent_bare_reference_label_proves_reference_and_matching(tmp_path,label):
    case=supplier_case();payload=case.response()
    reference=payload['purchase_reference']['v'][0]
    case=rewrite(case,payload,'PR reference: '+reference,label+'\n'+reference)
    payload['purchase_reference']['e']=[{'p':1,'q':reference}]
    state,_=execute(tmp_path,case,payload=payload)
    facts=trusted(tmp_path,state)
    assert value_at(facts,'purchase_reference')==reference
    assert value_at(facts,'line_items[0].code')==case.purchase_request[0].item_code
    assert state['matching_summary']['items'][0]['status']=='MATCHED'


def test_unlabelled_reference_value_is_not_guessed(tmp_path):
    case=supplier_case();payload=case.response()
    reference=payload['purchase_reference']['v'][0]
    case=rewrite(case,payload,'PR reference: '+reference,'Unrelated identifier\n'+reference)
    payload['purchase_reference']['e']=[{'p':1,'q':reference}]
    state,_=execute(tmp_path,case,payload=payload)
    assert value_at(trusted(tmp_path,state),'purchase_reference') is None
    assert state['matching_summary']['items'][0]['status']=='UNMATCHED'


def test_repeated_percentage_fragment_does_not_select_convenient_occurrence(tmp_path):
    case=supplier_case(advance='5',vat='15');payload=case.response()
    payload['payments'][0]['kind']['e']=[{'p':1,'q':'advance'}]
    payload['payments'][0]['percent']['e']=[{'p':1,'q':'5%'}]
    state,_=execute(tmp_path,case,payload=payload)
    # The fragment also occurs inside VAT 15%; it does not prove one obligation.
    assert value_at(trusted(tmp_path,state),'payments[0].percent') is None
    assert any(r['rule_id']=='R2' and r['status']=='unknown' for r in state['rule_findings'])


def test_explicit_item_code_fragment_is_verified_from_its_labelled_row(tmp_path):
    case=supplier_case();payload=case.response()
    code=payload['line_items'][0]['code']['v'][0]
    payload['line_items'][0]['code']['e']=[{'p':1,'q':code}]
    state,_=execute(tmp_path,case,payload=payload)
    assert value_at(trusted(tmp_path,state),'line_items[0].code')==code
    assert state['matching_summary']['items'][0]['status']=='MATCHED'


def test_different_delivery_scopes_cannot_be_joined_on_one_page(tmp_path):
    case=supplier_case(delivery='52');payload=case.response()
    old=payload['delivery']['wording']['v'][0]
    new='Delivery 52 calendar days after invoice. Delivery 18 calendar days from PO.'
    case=rewrite(case,payload,old,new)
    payload['delivery']['wording']=None
    payload['delivery']['duration']['e']=[{'p':1,'q':'52'}]
    payload['delivery']['anchor']['e']=[{'p':1,'q':'PO'}]
    state,_=execute(tmp_path,case,payload=payload)
    assert state['automated_decision']=='Needs a person to check'
    assert any(r['rule_id']=='R5' and r['status']=='unknown' for r in state['rule_findings'])


def test_full_page_quote_is_not_material_evidence(tmp_path):
    case=supplier_case(delivery='52',advance='27');payload=case.response()
    page=case.document.pages[0].text
    for claim in (payload['delivery']['duration'],payload['delivery']['anchor'],
                  payload['payments'][0]['kind'],payload['payments'][0]['percent']):
        claim['e']=[{'p':1,'q':page}]
    state,_=execute(tmp_path,case,payload=payload)
    facts=trusted(tmp_path,state)
    assert value_at(facts,'delivery.duration') is None
    assert value_at(facts,'payments[0].percent') is None
    assert state['automated_decision']=='Needs a person to check'
    assert any('FIELD_SCOPED_QUOTE_REQUIRED' in f.reason_codes for f in facts.facts)


def test_prompt_requests_scoped_evidence_and_explicit_codes():
    assert SCOPED_EVIDENCE_INSTRUCTIONS in proposal(supplier_case().document)['context']

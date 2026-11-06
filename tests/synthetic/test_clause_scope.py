"""Numbered field headings retain exact, single-clause evidence scopes."""
import pytest

from src.verification_text import evidence_clause_span
from supplier_cases import supplier_case
from test_gemini_workflow import execute
from test_scoped_evidence import rewrite, trusted
from src.trusted_facts import value_at


@pytest.mark.parametrize('prefix',['','2. ','2) ','Delivery. ','2. Delivery. ','2.1. Delivery. '])
def test_structural_prefix_and_delivery_fragments_have_one_source_scope(prefix):
    text=prefix+'Delivery within 60 days of PO.'
    scope=evidence_clause_span(text,0,len(text))
    assert scope==(0,len(text))
    for fragment in ('60','PO'):
        start=text.index(fragment)
        assert evidence_clause_span(text,start,start+len(fragment))==scope


@pytest.mark.parametrize('text',[
    '2. Delivery is delayed. Payment is due after PO.',
    '2. Delivery. Delivery within 60 days of invoice. Delivery within 18 days of PO.',
    'Payment. Advance is payable. Delivery within 60 days of PO.',
])
def test_prefix_does_not_join_unrelated_operative_sentences(text):
    assert evidence_clause_span(text,0,len(text)) is None


@pytest.mark.parametrize('prefix',['','2. Delivery. '])
def test_exact_delivery_clause_rejects_with_unresolved_pr(tmp_path,prefix):
    case=supplier_case(delivery='60',tax_basis=None);payload=case.response()
    old=payload['delivery']['wording']['v'][0]
    clause='Delivery within 60 days of PO.'
    quote=prefix+clause
    case=rewrite(case,payload,old,quote)
    payload['delivery']['wording']['v']=[clause]
    payload['delivery']['duration_unit']={'v':['days'],'e':[{'p':1,'q':quote}]}
    for name in ('wording','duration','anchor'):
        payload['delivery'][name]['e']=[{'p':1,'q':quote}]
    payload['purchase_reference']=None
    payload['line_items'][0]['code']=None
    state,calls=execute(tmp_path,case,payload=payload)
    assert len(calls)==1 and state['safe_error'] is None
    facts=trusted(tmp_path,state)
    assert str(value_at(facts,'delivery.duration'))=='60'
    assert value_at(facts,'delivery.anchor')=='PO'
    assert state['matching_summary']['items'][0]['status']=='UNMATCHED'
    assert any(r['rule_id']=='R5' and r['status']=='fail' for r in state['rule_findings'])
    assert state['automated_decision']=='Reject'


@pytest.mark.parametrize('prefix',['1. Payment. ','3. Advance payment. '])
def test_numbered_advance_clause_reaches_r2(tmp_path,prefix):
    case=supplier_case(advance='27');payload=case.response()
    payment=payload['payments'][0]
    clause='27% advance payable with the purchase order.'
    quote=prefix+clause
    case=rewrite(case,payload,payment['wording']['v'][0],quote)
    payment['wording']['v']=[clause]
    payment['timing']={}
    for name in ('wording','kind','percent'):
        payment[name]['e']=[{'p':1,'q':quote}]
    state,_=execute(tmp_path,case,payload=payload)
    assert state['safe_error'] is None
    assert any(r['rule_id']=='R2' and r['status']=='fail' for r in state['rule_findings'])
    assert state['automated_decision']=='Reject'


def test_ambiguous_numbered_delivery_fragment_does_not_choose_a_clause(tmp_path):
    case=supplier_case(delivery='60');payload=case.response()
    old=payload['delivery']['wording']['v'][0]
    text='2. Delivery. Delivery within 60 days of PO. Delivery within 60 days of invoice.'
    case=rewrite(case,payload,old,text)
    payload['delivery']['wording']=None
    payload['delivery']['duration']['e']=[{'p':1,'q':'60'}]
    payload['delivery']['anchor']['e']=[{'p':1,'q':'PO'}]
    state,_=execute(tmp_path,case,payload=payload)
    assert state['safe_error'] is None
    assert any(r['rule_id']=='R5' and r['status']=='unknown' for r in state['rule_findings'])
    assert state['automated_decision']=='Needs a person to check'


def test_numbered_clause_does_not_authorize_a_full_page_quote(tmp_path):
    case=supplier_case(delivery='60');payload=case.response()
    old=payload['delivery']['wording']['v'][0]
    case=rewrite(case,payload,old,'2. Delivery. '+old)
    for name in ('wording','duration','anchor'):
        payload['delivery'][name]['e']=[{'p':1,'q':case.document.pages[0].text}]
    state,_=execute(tmp_path,case,payload=payload)
    facts=trusted(tmp_path,state)
    assert value_at(facts,'delivery.duration') is None
    assert any('FIELD_SCOPED_QUOTE_REQUIRED' in f.reason_codes for f in facts.facts)
    assert any(r['rule_id']=='R5' and r['status']=='unknown' for r in state['rule_findings'])

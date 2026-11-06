"""Missing line items cannot discard independently verified document violations."""
import pytest

from src.ai_graph_workflow import ai_sqlite_service
from test_gemini_workflow import execute
from supplier_cases import supplier_case


@pytest.mark.parametrize('options,decision,reject_rule',[
    ({'delivery':'60'},'Reject','R5'),
    ({'advance':'27'},'Reject','R2'),
    ({'delivery':'60','advance':'27'},'Reject','R5'),
    ({},'Needs a person to check',None),
])
def test_document_rules_survive_empty_item_extraction(tmp_path,options,decision,reject_rule):
    case=supplier_case(**options);payload=case.response()
    payload['line_items']=[]
    state,calls=execute(tmp_path,case,payload=payload)
    assert len(calls)==1 and state['safe_error'] is None
    assert state['extraction_status']=='SCHEMA_VALIDATED'
    assert state['trusted_facts_status']=='BUILT'
    assert state['matching_summary']['items']==[]
    assert state['matching_summary']['missing_pr_indices']
    assert state['automated_decision']==decision
    assert any(r['rule_id']=='MATCH' and r['status']=='unknown' for r in state['rule_findings'])
    if reject_rule:
        assert any(r['rule_id']==reject_rule and r['status']=='fail' for r in state['rule_findings'])
        assert state['final_disposition']=='Reject'
    else:
        assert state['interrupts'] and state['final_disposition'] is None
    with ai_sqlite_service(tmp_path) as service:
        restored=service.inspect(state['thread_id'])
        assert restored['automated_decision']==decision
        assert restored['review_token']==state['review_token']
    assert len(calls)==1  # Checkpoint inspection never regenerates extraction.

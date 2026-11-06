"""Independently authored evidence -> unchanged reducer -> honest UI outcomes."""
import pytest
from supplier_cases import supplier_case
from test_manual_ai_ui import setup_ui, transport, run_ui
from ui import ai_processing
from ui.evaluation_workflow import start_evaluation
from src.ai_graph_workflow import ai_sqlite_service
from src.ai_graph_models import TrustedReviewer
from src.execution_status import presentation_outcome, TECHNICAL_FAILURE, HUMAN_REVIEW
from src.run_history import list_ai_evaluations, rows, statistics


@pytest.mark.parametrize('scenario,expected,display',[
    ('compliant','Approve','Approve'),
    ('price-violation','Reject','Reject'),
    ('unclear-payment','Needs a person to check',HUMAN_REVIEW),
    ('unsupported-evidence','Needs a person to check',HUMAN_REVIEW),
    ('timeout',None,TECHNICAL_FAILURE),
    ('invalid-json',None,TECHNICAL_FAILURE),
    ('invalid-contract',None,TECHNICAL_FAILURE),
    ('fabricated-quote',None,TECHNICAL_FAILURE),
    ('unspecified-tax-basis','Needs a person to check',HUMAN_REVIEW),
])
def test_actual_engine_decision_matches_ui_and_saved_history(setup_ui,monkeypatch,scenario,expected,display):
    _,config=setup_ui
    options={'price':'7.50'} if scenario=='price-violation' else {'payment_days':'85'} if scenario=='unclear-payment' else {'tax_basis':None} if scenario=='unspecified-tax-basis' else {}
    case=supplier_case(**options)
    # Independent PR positive/negative controls, not the real supplied CSV.
    monkeypatch.setattr(ai_processing,'purchase_request_items',lambda frame:case.purchase_request)
    payload=case.response();status=504 if scenario=='timeout' else 200
    if scenario=='unsupported-evidence':payload['line_items'][0]['quantity']['v']=['99999']
    elif scenario=='invalid-json':payload='not JSON'
    elif scenario=='invalid-contract':payload['supplier']['registrations'][0]['value']=True
    elif scenario=='fabricated-quote':payload['supplier']['name']['e'][0]['q']='Fabricated synthetic source quote'
    calls=transport(monkeypatch,payload,status)
    mode,state=start_evaluation('independent-decision.pdf',case.pdf,config=config,batch='independent',cloud_processing_authorized=True)
    assert mode=='AI-assisted' and len(calls)==1
    with ai_sqlite_service(ai_processing.storage_root()) as service:
        materials=service.review_materials(state['thread_id'],reviewer=TrustedReviewer(subject='synthetic-auditor',authorized=True))
    assessment=materials.get('assessment')
    assert (assessment or {}).get('decision')==expected
    assert presentation_outcome(state,assessment=assessment or {})=={'status':display,'business_decision':expected}
    app=run_ui(state)
    badges=[m.value for m in app.markdown if 'decision-badge' in m.value]
    assert len(badges)==1 and f'>{display}</span>' in badges[0]
    stages=next(frame.value for frame in app.dataframe if 'Workflow stage' in frame.value.columns)
    if expected is None:
        assert state['safe_error'] and assessment is None
        assert any(TECHNICAL_FAILURE in e.value for e in app.error)
        assert not any('Automated assessment:' in m.value for m in app.markdown)
        assert not any(HUMAN_REVIEW in w.value for w in app.warning)
        assert stages.loc[stages['Workflow stage']=='decide','Status'].iloc[0]!='Completed'
        assert 'Awaiting technical recovery' in list(stages['Status'])
    else:
        assert state['automated_decision']==assessment['decision'] and not app.error
        assert any('Automated assessment:' in m.value and expected in m.value for m in app.markdown)
        if expected=='Needs a person to check':
            assert state['interrupts'] and state['final_disposition'] is None
            assert any('not finalized' in w.value for w in app.warning)
    records=list_ai_evaluations(ai_processing.storage_root())
    assert len(records)==1 and records[0]['automated_decision']==expected
    assert rows(records)[0]['Processing status']==display
    if expected is None:
        assert statistics(records)['total_evaluated']==0 and statistics(records)['failed_documents']==1


def test_real_csv_route_can_reject_independent_advance_violation(setup_ui,monkeypatch):
    _,config=setup_ui
    case=supplier_case(advance='25')
    calls=transport(monkeypatch,case.response())
    _,state=start_evaluation('advance-violation.pdf',case.pdf,config=config,batch='real-csv',cloud_processing_authorized=True)
    assert len(calls)==1 and state['automated_decision']=='Reject'
    assert any(r['rule_id']=='R2' and r['status']=='fail' for r in state['rule_findings'])
    assert any('>Reject</span>' in m.value for m in run_ui(state).markdown)


def test_real_csv_tax_basis_is_not_silently_inferred():
    from src.purchase_request import read_purchase_request,purchase_request_items
    rows=purchase_request_items(read_purchase_request())
    assert rows and all(row.price_tax_basis is None for row in rows)


def test_human_resume_preserves_engine_review_and_does_not_generate_again(setup_ui,monkeypatch):
    _,config=setup_ui;case=supplier_case(payment_days='85')
    monkeypatch.setattr(ai_processing,'purchase_request_items',lambda frame:case.purchase_request)
    calls=transport(monkeypatch,case.response())
    _,state=start_evaluation('review.pdf',case.pdf,config=config,batch='review',cloud_processing_authorized=True)
    app=run_ui(state)
    app.text_area[0].input('Clarify the synthetic payment terms before proceeding.')
    app.text_input[0].input('synthetic-reviewer')
    next(c for c in app.checkbox if c.label.startswith('I have inspected')).check()
    next(b for b in app.button if b.label=='Record human review').click().run(timeout=30)
    assert not app.exception and len(calls)==1
    with ai_sqlite_service(ai_processing.storage_root()) as service:
        final=service.inspect(state['thread_id'])
    assert final['automated_decision']=='Needs a person to check'
    assert final['human_review_status']=='completed' and final['final_disposition']=='Correction requested'
    stages=next(frame.value for frame in app.dataframe if 'Workflow stage' in frame.value.columns)
    assert stages.loc[stages['Workflow stage']=='human_review','Status'].iloc[0]=='Completed'


def test_detailed_status_uses_actual_assessment_not_an_inconsistent_cached_label():
    state={'automated_decision':'Needs a person to check'}
    assert presentation_outcome(state,assessment={'decision':'Reject'})['status']=='Reject'
    assert presentation_outcome(state,assessment={})['business_decision'] is None
    assert state['automated_decision']=='Needs a person to check'  # Presentation never rewrites state.

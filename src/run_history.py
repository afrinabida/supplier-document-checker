"""Presentation-only unified history; never runs extraction or writes evaluations."""
from collections import Counter
from contextlib import closing
from pathlib import Path
import sqlite3
from src import graph_workflow
from src.evaluation_store import list_evaluations,evaluation_rows,filter_evaluations,workflow_status
from src.ai_graph_workflow import ai_sqlite_service,VERSION
from src.ai_graph_models import TrustedReviewer,AIGraphError

AI_PREFIX='ai::'


def ai_root(): return graph_workflow.DEFAULT_DB.parent/'ai_assisted'


def list_ai_evaluations(root=None):
    root=Path(root or ai_root());database=root/'ai_checkpoints.sqlite3'
    if not database.exists(): return []
    try:
        with closing(sqlite3.connect(database.resolve().as_uri()+'?mode=ro',uri=True)) as conn:
            if not conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='checkpoints'").fetchone(): return []
            threads=[r[0] for r in conn.execute("SELECT DISTINCT thread_id FROM checkpoints WHERE checkpoint_ns = ''")]
        records=[]
        with ai_sqlite_service(root) as service:
            for physical in threads:
                if not physical.startswith(VERSION+'-'): continue
                thread=physical[len(VERSION)+1:];state=service.inspect(thread)
                materials=service.review_materials(thread,reviewer=TrustedReviewer(subject='local-history',authorized=True))
                facts=materials.get('facts',{}).get('facts',[])
                supplier=next((f['normalized'].get('text_value') for f in facts if f['path']=='supplier.name' and f['authoritative'] and f.get('normalized')),None)
                events=state.get('audit_events',[])
                records.append(dict(thread_id=AI_PREFIX+thread,ai_thread_id=thread,mode='AI-assisted',
                    input_filename=state.get('input_filename') or materials.get('document',{}).get('metadata',{}).get('original_filename','document.pdf'),
                    input_sha256=state['document_sha256'],saved_at=events[-1]['timestamp_utc'] if events else '',
                    automated_decision=None if state.get('safe_error') else state.get('automated_decision'),
                    error=state.get('safe_error'),review_status=state['human_review_status'],
                    human_review=state.get('human_review'),final_outcome=state.get('final_disposition'),
                    extraction={'fields':{'supplier':{'normalized':supplier}}}))
        return sorted(records,key=lambda r:r['saved_at'],reverse=True)
    except (OSError,sqlite3.Error,AIGraphError,ValueError,KeyError):
        raise ValueError('AI run history could not be read safely. Existing records were not replaced.') from None


def list_runs():
    legacy=[{**r,'mode':'Deterministic'} for r in list_evaluations()]
    return sorted(legacy+list_ai_evaluations(),key=lambda r:r.get('saved_at',''),reverse=True)


def latest_runs(records):
    latest={}
    for row in sorted(records,key=lambda r:r.get('saved_at',''),reverse=True):
        latest.setdefault((row.get('mode','Deterministic'),row['input_filename'],row['input_sha256']),row)
    return list(latest.values())


def statistics(records):
    latest=latest_runs(records);counts=Counter(r.get('automated_decision') for r in latest)
    return dict(total_evaluated=sum(counts[d] for d in ('Approve','Reject','Needs a person to check')),
        approved=counts['Approve'],rejected=counts['Reject'],needs_review=counts['Needs a person to check'],
        pending_reviews=sum(r.get('review_status')=='pending' for r in records),
        failed_documents=sum(bool(r.get('error')) for r in latest),saved_runs=len(records))


def rows(records):
    from src.execution_status import presentation_outcome
    values=evaluation_rows(records)
    return [{'Mode':record.get('mode','Deterministic'),
             'Processing status':presentation_outcome(record)['status'],**value}
            for record,value in zip(records,values)]


def filter_runs(records,search='',decision='All decisions',status='All states',latest_only=True):
    return filter_evaluations(latest_runs(records) if latest_only else records,search,decision,status,False)

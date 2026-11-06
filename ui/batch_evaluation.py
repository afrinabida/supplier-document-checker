"""Session-local sequential orchestration over the existing per-document graphs."""
from copy import deepcopy
from hashlib import sha256
import json
from uuid import uuid4

from src.rules_config import RULE_BY_ID
from ui.evaluation_workflow import start_evaluation

FINISHED = {'Approve', 'Reject', 'Human Review', 'Processing Error'}


def prepare_batch(previous, documents, config, *, fresh=False):
    identity=sha256(json.dumps(dict(
        documents=[(name,sha256(data).hexdigest()) for name,data in documents],
        config=config.model_dump(mode='json')),sort_keys=True).encode()).hexdigest()
    if previous and previous['identity']==identity and not fresh:
        return previous
    return dict(identity=identity,id=uuid4().hex,documents=[
        dict(filename=name,sha256=sha256(data).hexdigest(),evaluation_id='batch_'+uuid4().hex,
             status='Pending',reason='',mode=None,state=None)
        for name,data in documents])


def _outcome(state):
    error=state.get('safe_error') or state.get('error')
    if error:
        return 'Processing Error', 'Evaluation failed; inspect its saved diagnostic.'
    decision=state.get('automated_decision')
    if decision not in ('Approve','Reject','Needs a person to check'):
        return 'Processing Error','No completed automated assessment is available.'
    findings=state.get('rule_findings',[])
    if not findings:
        findings=state.get('automated_assessment',{}).get('evaluation',{}).get('results',[])
    reasons=[f for f in findings if f.get('status') in ('fail','unknown')]
    if decision=='Reject':
        reasons=[f for f in reasons if f.get('status')=='fail' and f.get('category')=='Reject']
    first=reasons[0] if reasons else None
    rule=RULE_BY_ID.get(first.get('rule_id','').split('.')[0]) if first else None
    reason=(first['rule_id']+': '+(rule.wording if rule else 'Unresolved evidence requires review.')) if first else (
        'All required checks passed.' if decision=='Approve' else 'Inspect the saved rule findings.')
    return ('Human Review' if decision=='Needs a person to check' else decision),reason


def run_batch(batch, documents, *, config, cloud_processing_authorized=False,
              evaluate=None, on_progress=None):
    """Each ID is reserved before execution; rerenders/repeated starts reuse results."""
    evaluate=evaluate or start_evaluation
    if len(batch['documents'])!=len(documents):
        raise ValueError('Batch inputs changed; select the documents again.')
    for row,(name,data) in zip(batch['documents'],documents):
        if row['filename']!=name or row['sha256']!=sha256(data).hexdigest():
            raise ValueError('Batch inputs changed; select the documents again.')
    duplicate_names={name for name,_ in documents if sum(n==name for n,_ in documents)>1}
    for index,(row,(name,data)) in enumerate(zip(batch['documents'],documents)):
        def progress(stage):
            if on_progress:on_progress(index,len(documents),row,stage)
        if row['status'] in FINISHED:
            progress('saved')
            continue
        row.update(status='Processing',reason='')
        progress('validate_pdf')
        try:
            if name in duplicate_names:
                raise ValueError('Duplicate filenames require distinct upload names.')
            mode,state=evaluate(name,data,config=config,batch=batch['id'],
                cloud_processing_authorized=cloud_processing_authorized,
                thread_id=row['evaluation_id'],on_stage=progress)
            if state.get('thread_id')!=row['evaluation_id']:
                raise ValueError('Evaluation thread does not belong to this document.')
            state=deepcopy(state)
            status,reason=_outcome(state)
            row.update(mode=mode,state=state,status=status,reason=reason)
        except Exception:
            # Keep a failed start in this batch and continue. Exception payloads
            # may contain document text/credentials and are never displayed.
            row.update(status='Processing Error',state=None,
                reason='Evaluation could not complete safely; other documents continue.')
        progress('saved')
    return batch


def summary_rows(batch):
    return [{'Filename':r['filename'],'Decision':r['status'],'Main reason':r['reason'],
             'Evaluation ID':r['evaluation_id']} for r in batch['documents']]

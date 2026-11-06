"""Separate fixed Gemini graph with private artifacts and durable guarded review.

Only the supported service methods enforce coordination/identity checks. Direct
graph.invoke/update_state is an internal API and must never be exposed to clients.
"""
from contextlib import contextmanager, closing
from datetime import datetime, timezone
from hashlib import sha256
from importlib.metadata import version as package_version
import json
from pathlib import Path
import re
import sqlite3
import threading
from uuid import uuid4

from langgraph.graph import START, END, StateGraph
from langgraph.types import Command, interrupt
from langgraph.checkpoint.sqlite import SqliteSaver
from langsmith import tracing_context

from src.ai_graph_artifacts import AIArtifactStore, canonical_bytes, private_json
from src.ai_graph_models import AIState, AIGraphError, AIReviewSubmission, TrustedReviewer
from src.gemini_config import GeminiConfig
from src.extraction_prompt import PROMPT_VERSION, SCHEMA_VERSION
from src.pdf_ingestion import ingest_pdf, IngestionLimits
from src.ingestion_models import DocumentRecord
from src.semantic_extraction import extract_supplier_document
from src.llm_contracts import StructuredExtractionResult
from src.evidence_verification import verify_extraction
from src.verification_models import EvidenceVerificationReport, VerificationConfig
from src.trusted_facts import build_trusted_facts
from src.trusted_models import TrustedFactsResult, MatchingResult
from src.semantic_matching import match_trusted_items
from src.semantic_rules import evaluate_trusted_rules
from src.decision_engine import decide
from src.models import PurchaseRequestItem, RuleEvaluation, SupplierDecision
from src.rules_config import CONFIG

VERSION='ai-graph-v1'
STAGES=('ingest','extract','verify','trust','match_pr','rules','decide','human_review','finalize')
ROOT=Path(__file__).resolve().parents[1]


def dependency_versions():
    return {name:package_version(name) for name in ('langgraph','langgraph-checkpoint',
        'langgraph-checkpoint-sqlite','pydantic','PyMuPDF','google-genai')}


def implementation_hash():
    names=('ai_graph_workflow.py','ai_graph_models.py','ai_graph_artifacts.py','ai_supplier_service.py',
        'semantic_rules.py','semantic_matching.py','trusted_facts.py','trusted_models.py',
        'semantic_extraction.py','semantic_models.py','llm_contracts.py','extraction_prompt.py',
        'gemini_config.py','gemini_provider.py','evidence_verification.py','verification_models.py',
        'verification_values.py','verification_text.py','verification_conflicts.py','pdf_ingestion.py',
        'ingestion_models.py','document_sources.py','models.py','rule_engine.py','rules_config.py','decision_engine.py','evidence.py',
        'minimal_json_adapter.py','gemini_minimal_contract.py','ai_assisted.py','local_validation.py')
    digest=sha256(VERSION.encode())
    digest.update(canonical_bytes(dependency_versions()))
    for name in names: digest.update((ROOT/'src'/name).read_bytes())
    return digest.hexdigest()


def _now(): return datetime.now(timezone.utc).isoformat()


def _events(state,*entries):
    events=list(state.get('audit_events',[]))
    for event,stage,details in entries:
        events.append(dict(sequence=len(events)+1,event=event,stage=stage,timestamp_utc=_now(),details=details))
    return events


def _advance(state,stage,*,event='stage_completed',details=None,**updates):
    return dict(current_stage=stage,audit_events=_events(state,(event,stage,details or {})),**updates)


def _load(store,state,key,model=None):
    reference=state.get('artifacts',{}).get(key)
    if not reference: raise AIGraphError('REQUIRED_ARTIFACT_MISSING')
    return store.get(reference,model)


def _review_identity(state,submission):
    if any(getattr(submission,key)!=state[key] for key in ('thread_id','workflow_id','review_token')) or submission.document_sha256!=state['document_sha256']:
        raise AIGraphError('REVIEW_IDENTITY_MISMATCH')


def build_ai_graph(checkpointer,store,*,config,provider=None,cloud_processing_authorized=False):
    """Internal graph builder; service owns trusted config/provider outside state."""
    def persist(state,key,value): return {**state['artifacts'],key:store.put(value)}
    def ingest(state):
        data=store.get_bytes(state['artifacts']['source'])
        if sha256(data).hexdigest()!=state['document_sha256']: raise AIGraphError('DOCUMENT_HASH_MISMATCH')
        document=ingest_pdf(data,state.get('input_filename'))
        return _advance(state,'ingest',artifacts=persist(state,'document',document),
            ingestion_completeness=document.completeness.value)
    def extract(state):
        extraction=extract_supplier_document(_load(store,state,'document',DocumentRecord),adapter=provider,
            config=config,cloud_processing_authorized=cloud_processing_authorized)
        # Raw transport text is unnecessary once claims have been validated.
        extraction=extraction.model_copy(update={'raw_claims_json':None})
        updates=dict(extraction_status=extraction.status,artifacts=persist(state,'extraction',extraction))
        if extraction.status!='SCHEMA_VALIDATED':
            code=extraction.errors[0].code.value if extraction.errors else 'EXTRACTION_INCOMPLETE'
            updates['safe_error']=dict(stage='extract',code=code)
        return _advance(state,'extract',event='error' if updates.get('safe_error') else 'stage_completed',
            details={'model_id':state['model_id'],'prompt_version':state['prompt_version'],
                'status':extraction.status,**({'code':updates['safe_error']['code']} if updates.get('safe_error') else {})},**updates)
    def verify(state):
        report=verify_extraction(_load(store,state,'document',DocumentRecord),
            _load(store,state,'extraction',StructuredExtractionResult),
            config=VerificationConfig(require_source_bytes=True),source_bytes=store.get_bytes(state['artifacts']['source']))
        summary=dict(identity=report.identity_outcome.value,hash_check=report.hash_check,
            verified=sum(f.eligible_for_future_use for f in report.fields),
            unresolved=[dict(path=f.field_path,status=f.outcome.value,reasons=list(f.reason_codes),
                pages=sorted({c.source.page_number for c in f.evidence_checks}))
                for f in report.fields if not f.eligible_for_future_use],
            conflicts=[dict(classification=c.classification,reason=c.reason_code) for c in report.conflicts])
        return _advance(state,'verify',details={'identity':summary['identity'],'verified_fields':summary['verified']},
            evidence_summary=summary,artifacts=persist(state,'verification',report))
    def trust(state):
        facts=build_trusted_facts(_load(store,state,'document',DocumentRecord),
            _load(store,state,'extraction',StructuredExtractionResult),
            _load(store,state,'verification',EvidenceVerificationReport),source_bytes=store.get_bytes(state['artifacts']['source']))
        return _advance(state,'trust',trusted_facts_status='BUILT',artifacts=persist(state,'facts',facts))
    def match(state):
        rows=[PurchaseRequestItem.model_validate(row) for row in _load(store,state,'purchase_request')]
        matching=match_trusted_items(_load(store,state,'facts',TrustedFactsResult),rows,_load(store,state,'document',DocumentRecord))
        return _advance(state,'match_pr',matching_summary=matching.model_dump(mode='json'),artifacts=persist(state,'matching',matching))
    def rules(state):
        rows=[PurchaseRequestItem.model_validate(row) for row in _load(store,state,'purchase_request')]
        evaluation,items,uncertainty=evaluate_trusted_rules(_load(store,state,'facts',TrustedFactsResult),
            _load(store,state,'document',DocumentRecord),rows,_load(store,state,'matching',MatchingResult))
        # Values/exact quotations live only in the private artifact.
        findings=[dict(rule_id=r.rule_id,status=r.status,category=r.category) for r in evaluation.results]
        return _advance(state,'rules',rule_findings=findings,review_reasons=list(uncertainty),
            artifacts=persist(state,'rules',dict(evaluation=evaluation,items=items,uncertainty=uncertainty)))
    def aggregate(state):
        artifacts=state['artifacts'];reasons=list(state.get('review_reasons',[]))
        if state.get('safe_error'):
            # No decision from partial/unavailable stages; never a regex fallback.
            decision='Needs a person to check';reasons.append(state['safe_error']['code'])
        else:
            evaluation=RuleEvaluation.model_validate(_load(store,state,'rules')['evaluation'])
            assessment=decide(evaluation);decision=assessment.decision
            artifacts=persist(state,'assessment',assessment)
            reasons.extend(r.rule_id+':'+r.status for r in assessment.reasons)
        pending=decision=='Needs a person to check'
        entries=[('automated_decision','decide',{'decision':decision})]
        if pending: entries.append(('human_review_requested','human_review',{'reason_count':len(set(reasons))}))
        return dict(current_stage='human_review' if pending else 'decide',artifacts=artifacts,
            automated_decision=decision,review_required=pending,review_reasons=list(dict.fromkeys(reasons)),
            human_review_status='pending' if pending else 'not_required',audit_events=_events(state,*entries))
    def review(state):
        # Nothing is persisted before interrupt here: LangGraph restarts this node.
        payload=dict(thread_id=state['thread_id'],workflow_id=state['workflow_id'],document_sha256=state['document_sha256'],
            review_token=state['review_token'],automated_decision=state['automated_decision'],
            review_reasons=state['review_reasons'],evidence_summary=state.get('evidence_summary',{}),
            allowed_actions=['approve','reject','request_correction'])
        receipt=interrupt(payload)
        # Only guarded resume() supplies a private artifact reference here.
        record=store.get(receipt['receipt_ref'])
        submission=AIReviewSubmission.model_validate(record['submission'])
        _review_identity(state,submission)
        if not record.get('reviewer_subject'): raise AIGraphError('TRUSTED_REVIEWER_REQUIRED')
        outcome={'approve':'Approve','reject':'Reject','request_correction':'Correction requested'}[submission.action]
        human=dict(reviewer_subject=record['reviewer_subject'],action=submission.action,timestamp_utc=record['timestamp_utc'],
            document_sha256=state['document_sha256'],submission_id=submission.submission_id,
            submission_sha256=record['submission_sha256'],receipt_ref=receipt['receipt_ref'])
        return _advance(state,'human_review',event='human_review_completed',details={'action':submission.action},
            human_review=human,human_review_status='completed',final_disposition=outcome,
            artifacts={**state['artifacts'],'review_receipt':receipt['receipt_ref']})
    def finalize(state):
        if state['automated_decision']=='Needs a person to check':
            if not state.get('human_review'): raise AIGraphError('REVIEW_REQUIRED_BEFORE_FINALIZATION')
            disposition=state['final_disposition']
        else: disposition=state['automated_decision']
        if disposition=='Approve' and state.get('safe_error') and not state.get('human_review'):
            raise AIGraphError('UNSUPPORTED_AUTOMATIC_APPROVAL')
        return _advance(state,'finalize',event='finalized',details={'disposition':disposition},final_disposition=disposition)
    def guard(stage,function):
        def node(state):
            try: return function(state)
            except Exception as error:
                # Never catch BaseException: interrupt/cancellation control flow survives.
                code=str(error) if isinstance(error,AIGraphError) and re.fullmatch(r'[A-Z0-9_]{1,64}',str(error)) else 'STAGE_FAILED'
                return _advance(state,stage,event='error',details={'code':code},safe_error={'stage':stage,'code':code})
        return node
    graph=StateGraph(AIState)
    functions=(ingest,extract,verify,trust,match,rules)
    for stage,function in zip(STAGES[:6],functions,strict=True): graph.add_node(stage,guard(stage,function))
    graph.add_node('decide',aggregate);graph.add_node('human_review',review);graph.add_node('finalize',finalize)
    graph.add_edge(START,'ingest')
    for i,stage in enumerate(STAGES[:6]):
        following=STAGES[i+1]
        graph.add_conditional_edges(stage,lambda state: 'error' if state.get('safe_error') else 'ok',
            {'error':'decide','ok':following})
    graph.add_conditional_edges('decide',lambda state: 'review' if state['review_required'] else 'done',
        {'review':'human_review','done':'finalize'})
    graph.add_edge('human_review','finalize');graph.add_edge('finalize',END)
    return graph.compile(checkpointer=checkpointer)


class AIGraphService:
    """Trusted-caller entry points; coordination serializes writers across processes."""
    def __init__(self,checkpointer,store,coordination_path,*,config=None,provider=None,cloud_processing_authorized=False):
        self.config=config or GeminiConfig()  # Inspection/review does not need a key.
        self.store=store;self.coordination_path=Path(coordination_path);self._lock=threading.RLock()
        self.cloud_processing_authorized=cloud_processing_authorized;self.provider=provider
        self.implementation_sha256=implementation_hash()
        self.graph=build_ai_graph(checkpointer,store,config=self.config,provider=provider,
            cloud_processing_authorized=cloud_processing_authorized)

    @staticmethod
    def _config(thread_id):
        if not isinstance(thread_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,96}',thread_id):
            raise AIGraphError('INVALID_THREAD_ID')
        # Physical thread namespace cannot collide with legacy thread IDs.
        return {'configurable':{'thread_id':VERSION+'-'+thread_id}}

    @contextmanager
    def _exclusive(self):
        with self._lock,tracing_context(enabled=False):
            try:
                with closing(sqlite3.connect(self.coordination_path,timeout=30)) as connection:
                    connection.execute('CREATE TABLE IF NOT EXISTS coordination (id INTEGER PRIMARY KEY)')
                    connection.commit();connection.execute('BEGIN IMMEDIATE')
                    yield
            except AIGraphError: raise
            except Exception: raise AIGraphError('WORKFLOW_STORAGE_OR_EXECUTION_FAILED') from None

    def _inspect(self,thread_id):
        snapshot=self.graph.get_state(self._config(thread_id))
        if not snapshot.values: raise AIGraphError('THREAD_NOT_FOUND')
        state=json.loads(json.dumps(snapshot.values,allow_nan=False))
        if state.get('checkpoint_version')!=VERSION or state.get('processing_mode')!='gemini':
            raise AIGraphError('CHECKPOINT_VERSION_MISMATCH')
        if (state.get('thread_id')!=thread_id or state.get('document_id')!='sha256:'+state['document_sha256'] or
            state['artifacts'].get('source')!=state['document_sha256'] or
            state['artifacts'].get('purchase_request')!=state['purchase_request_sha256']):
            raise AIGraphError('CHECKPOINT_IDENTITY_MISMATCH')
        for reference in state.get('artifacts',{}).values(): self.store.get_bytes(reference)
        state['next_nodes']=list(snapshot.next)
        state['interrupts']=[item.value for task in snapshot.tasks for item in task.interrupts]
        return state

    def inspect(self,thread_id):
        with self._exclusive(): return self._inspect(thread_id)

    @staticmethod
    def _read_pdf(source):
        try:
            if isinstance(source,bytes): data=source
            else:
                with Path(source).open('rb') as stream: data=stream.read(IngestionLimits().max_file_bytes+1)
        except (OSError,ValueError,TypeError): raise AIGraphError('PDF_INPUT_UNAVAILABLE') from None
        if len(data)>IngestionLimits().max_file_bytes: raise AIGraphError('PDF_SIZE_LIMIT')
        return data

    def start(self,pdf,purchase_request,*,thread_id,on_stage=None,filename=None):
        if not self.config.enabled: raise AIGraphError('AI_FEATURE_DISABLED')
        if not self.cloud_processing_authorized: raise AIGraphError('CLOUD_AUTHORIZATION_REQUIRED')
        graph_config=self._config(thread_id);data=self._read_pdf(pdf)
        from src.pdf_ingestion import safe_filename
        display_filename=safe_filename(filename or 'document.pdf')
        try: rows=[PurchaseRequestItem.model_validate(row) for row in purchase_request]
        except (ValueError,TypeError): raise AIGraphError('INVALID_PURCHASE_REQUEST') from None
        doc_hash=sha256(data).hexdigest();pr_hash=sha256(canonical_bytes(rows)).hexdigest()
        public_config=self.config.model_dump(mode='json')
        config_hash=sha256(canonical_bytes(public_config)).hexdigest()
        policy_hash=sha256((ROOT/'src/rules_config.py').read_bytes()).hexdigest()
        binding=dict(document_sha256=doc_hash,input_filename=display_filename,purchase_request_sha256=pr_hash,configuration_sha256=config_hash,
            implementation_sha256=self.implementation_sha256,policy_sha256=policy_hash,
            prompt_version=getattr(self.provider,'prompt_version',PROMPT_VERSION),
            provider_contract=getattr(self.provider,'contract_version','full-semantic-v1'),
            schema_version=SCHEMA_VERSION,processing_mode='gemini',checkpoint_version=VERSION,
            transport='injected-offline' if self.provider is not None and not self.provider.is_remote else 'remote')
        workflow_id=sha256(canonical_bytes(binding)).hexdigest()
        with self._exclusive():
            snapshot=self.graph.get_state(graph_config)
            if snapshot.values:
                state=self._inspect(thread_id)
                if state['workflow_id']!=workflow_id: raise AIGraphError('THREAD_INPUT_OR_VERSION_MISMATCH')
                if snapshot.next and not state['interrupts'] and not state.get('cancelled'):
                    last=state['current_stage']
                    if last=='registered': last=START
                    elif last=='human_review' and not state.get('human_review'): last='decide'
                    self.graph.update_state(graph_config,{'audit_events':_events(state,
                        ('recovery_started',state['current_stage'],{'next_nodes':list(snapshot.next)}))},as_node=last)
                    for update in self.graph.stream(None,config=graph_config,stream_mode='updates'):
                        if on_stage:
                            for stage in update:
                                if stage!='__interrupt__': on_stage(stage)
                return self._inspect(thread_id)
            initial=dict(**{k:v for k,v in binding.items() if k!='transport'},workflow_id=workflow_id,thread_id=thread_id,
                document_id='sha256:'+doc_hash,model_id=self.config.model,artifacts={'source':self.store.put_bytes(data),
                'purchase_request':self.store.put(rows)},current_stage='registered',review_token=uuid4().hex,
                ingestion_completeness='NOT_STARTED',extraction_status='NOT_STARTED',trusted_facts_status='NOT_STARTED',
                evidence_summary={},matching_summary={},rule_findings=[],automated_decision=None,review_required=False,
                review_reasons=[],human_review_status='not_started',human_review=None,final_disposition=None,
                safe_error=None,cancelled=False,audit_events=[])
            initial['audit_events']=_events(initial,('processing_started','registered',
                {'mode':'gemini','cloud_processing_authorized':True,'dependency_versions':dependency_versions()}))
            for update in self.graph.stream(initial,config=graph_config,stream_mode='updates'):
                if on_stage:
                    for stage in update:
                        if stage!='__interrupt__': on_stage(stage)
            return self._inspect(thread_id)

    @staticmethod
    def _reviewer(reviewer):
        if not isinstance(reviewer,TrustedReviewer) or not reviewer.authorized:
            raise AIGraphError('TRUSTED_AUTHORIZED_REVIEWER_REQUIRED')
        try: return TrustedReviewer.model_validate(reviewer)
        except (ValueError,TypeError): raise AIGraphError('TRUSTED_AUTHORIZED_REVIEWER_REQUIRED') from None

    def resume(self,thread_id,submission,*,reviewer,current_pdf):
        reviewer=self._reviewer(reviewer)
        try: submission=AIReviewSubmission.model_validate(submission)
        except (ValueError,TypeError): raise AIGraphError('INVALID_REVIEW_SUBMISSION') from None
        digest=sha256(self._read_pdf(current_pdf)).hexdigest()
        with self._exclusive():
            state=self._inspect(thread_id);_review_identity(state,submission)
            if digest!=state['document_sha256']: raise AIGraphError('STALE_REVIEW_DOCUMENT')
            if state['implementation_sha256']!=self.implementation_sha256: raise AIGraphError('WORKFLOW_IMPLEMENTATION_CHANGED')
            receipt=dict(submission=private_json(submission),reviewer_subject=reviewer.subject)
            submission_hash=sha256(canonical_bytes(receipt)).hexdigest()
            previous=state.get('human_review')
            if previous:
                if previous['submission_sha256']!=submission_hash: raise AIGraphError('REVIEW_ALREADY_COMPLETED')
                if state['next_nodes']==['finalize']:
                    self.graph.update_state(self._config(thread_id),{'audit_events':_events(state,
                        ('recovery_started','human_review',{'next_nodes':['finalize']}))},as_node='human_review')
                    self.graph.invoke(None,config=self._config(thread_id))
                    return self._inspect(thread_id)
                return state
            if state.get('cancelled') or state['next_nodes']!=['human_review'] or not state['interrupts'] or state['human_review_status']!='pending':
                raise AIGraphError('THREAD_NOT_AWAITING_REVIEW')
            receipt.update(submission_sha256=submission_hash,timestamp_utc=_now())
            reference=self.store.put(receipt)
            self.graph.invoke(Command(resume={'receipt_ref':reference}),config=self._config(thread_id))
            return self._inspect(thread_id)

    def review_materials(self,thread_id,*,reviewer):
        """Explicit sensitive read for a caller-authorized reviewer; never log it."""
        self._reviewer(reviewer)
        with self._exclusive():
            state=self._inspect(thread_id)
            return {key:self.store.get(reference) for key,reference in state['artifacts'].items()
                    if key in ('purchase_request','document','extraction','verification','facts','rules','assessment','review_receipt')}

    def cancel(self,thread_id,*,reviewer,current_pdf):
        """Cancel a pending review; corrections require a fresh document/thread."""
        reviewer=self._reviewer(reviewer);digest=sha256(self._read_pdf(current_pdf)).hexdigest()
        with self._exclusive():
            state=self._inspect(thread_id)
            if digest!=state['document_sha256']: raise AIGraphError('STALE_REVIEW_DOCUMENT')
            if state.get('cancelled'): return state
            if state['human_review_status']!='pending' or state['next_nodes']!=['human_review']:
                raise AIGraphError('THREAD_NOT_CANCELLABLE')
            updates=dict(cancelled=True,current_stage='cancelled',human_review_status='cancelled',final_disposition='Cancelled',
                audit_events=_events(state,('cancelled','human_review',{'reviewer_subject':reviewer.subject})))
            self.graph.update_state(self._config(thread_id),updates,as_node='finalize')
            return self._inspect(thread_id)


@contextmanager
def ai_sqlite_service(storage_root,*,config=None,provider=None,cloud_processing_authorized=False):
    """Explicit AI-only storage; never opens the legacy default checkpoint path."""
    try:
        root=Path(storage_root).resolve();root.mkdir(parents=True,exist_ok=True)
        store=AIArtifactStore(root/'private_artifacts')
        with SqliteSaver.from_conn_string(str(root/'ai_checkpoints.sqlite3')) as saver:
            yield AIGraphService(saver,store,root/'ai_coordination.sqlite3',config=config,provider=provider,
                cloud_processing_authorized=cloud_processing_authorized)
    except AIGraphError: raise
    except (OSError,sqlite3.Error,TypeError): raise AIGraphError('AI_CHECKPOINT_STORAGE_UNAVAILABLE') from None


def run_ai_supplier_graph(pdf,purchase_request,*,thread_id,storage_root,config=None,
        provider=None,cloud_processing_authorized=False):
    """Opt-in run/recover entry point. Existing legacy entry points stay default."""
    try: config=config or GeminiConfig.from_env()
    except (ValueError,TypeError): raise AIGraphError('INVALID_AI_CONFIGURATION') from None
    with ai_sqlite_service(storage_root,config=config,provider=provider,
            cloud_processing_authorized=cloud_processing_authorized) as service:
        return service.start(pdf,purchase_request,thread_id=thread_id)

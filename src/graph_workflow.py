"""Fixed LangGraph orchestration, durable interrupts, and guarded local review API."""
from contextlib import contextmanager, closing
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
from typing import Literal, TypedDict
from uuid import uuid4

from langgraph.graph import START, END, StateGraph
from langgraph.types import Command, interrupt
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langsmith import tracing_context
from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.models import ExtractionResult, PurchaseRequestItem, RuleEvaluation, RuleResult, SupplierDecision
from src.pdf_parser import read_pdf
from src.extractor import extract_fields, validate_structure
from src.evidence import collect_evidence
from src.rule_engine import match_purchase_request, evaluate_arithmetic, evaluate_rules
from src.decision_engine import decide
from src.workflow import SAMPLE_DIR, read_purchase_request, purchase_request_items

ROOT = Path(__file__).resolve().parents[1]
# Configuration is resolved without creating directories/connections on import.
DEFAULT_DB = Path(os.environ.get('SUPPLIER_CHECKER_RUNTIME_DIR', ROOT / '.data')) / 'workflows.sqlite3'

class GraphState(TypedDict, total=False):
    thread_id: str
    pack_id: str
    input_path: str
    input_filename: str
    input_sha256: str
    pr_path: str
    pr_sha256: str
    review_token: str
    extraction: dict
    evidence: list[dict]
    purchase_request: list[dict]
    matched_item: dict | None
    match_explanation: str
    arithmetic_findings: list[dict]
    evaluation: dict
    automated_assessment: dict
    automated_decision: str
    workflow_stage: str
    trace: list[str]
    warnings: list[dict]
    review_status: str
    human_review: dict | None
    final_outcome: str | None
    error: str | None

class ReviewAction(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    thread_id: str
    pack_id: str
    review_token: str
    submission_id: str = Field(min_length=1, max_length=128)
    action: Literal['approve', 'reject', 'request_correction']
    reason: str = Field(min_length=1, max_length=5000)
    reviewer: str | None = None

    @field_validator('reason', 'submission_id')
    @classmethod
    def not_blank(cls, value):
        if not value.strip():
            raise ValueError('A nonblank reason and submission ID are required.')
        return value.strip()


def _extraction(state):
    return ExtractionResult.model_validate(state['extraction'])


def _advance(state, name, **updates):
    return dict(workflow_stage=name, trace=state.get('trace', []) + [name], **updates)


def _guard(name, function):
    def node(state):
        try:
            return function(state)
        except (ValueError, OSError, RuntimeError) as exc:
            return _advance(state, name, error=f'{type(exc).__name__}: {exc}', review_status='error')
    return node


def extract_node(state):
    data = Path(state['input_path']).read_bytes()
    if sha256(data).hexdigest() != state['input_sha256']:
        raise ValueError('Input PDF changed after evaluation was registered.')
    result = extract_fields(read_pdf(data, state['input_filename']))
    return _advance(state, 'extract', extraction=result.model_dump(mode='json'))


def validate_node(state):
    result = validate_structure(_extraction(state))
    evidence = collect_evidence(result)
    # Keep legacy extraction stages unchanged for old UI/consumers.
    result.stages = ['read', 'extract', 'validate structure', 'collect evidence']
    return _advance(state, 'validate', extraction=result.model_dump(mode='json'),
                    evidence=[e.model_dump(mode='json') for e in evidence],
                    warnings=[w.model_dump(mode='json') for w in result.warnings])


def match_node(state):
    path = Path(state['pr_path'])
    if sha256(path.read_bytes()).hexdigest() != state['pr_sha256']:
        raise ValueError('Purchase request changed after evaluation was registered.')
    items = purchase_request_items(read_purchase_request(path))
    item, explanation = match_purchase_request(_extraction(state), items)
    return _advance(state, 'match_pr', purchase_request=[i.model_dump(mode='json') for i in items],
                    matched_item=item.model_dump(mode='json') if item else None, match_explanation=explanation)


def arithmetic_node(state):
    findings = evaluate_arithmetic(_extraction(state))
    return _advance(state, 'arithmetic', arithmetic_findings=[r.model_dump(mode='json') for r in findings])


def rules_node(state):
    items = [PurchaseRequestItem.model_validate(item) for item in state['purchase_request']]
    item = PurchaseRequestItem.model_validate(state['matched_item']) if state['matched_item'] else None
    evaluation = evaluate_rules(
        _extraction(state), items,
        match_result=(item, state['match_explanation']),
        arithmetic_results=[RuleResult.model_validate(r) for r in state['arithmetic_findings']])
    return _advance(state, 'rules', evaluation=evaluation.model_dump(mode='json'))


def decide_node(state):
    assessment = decide(RuleEvaluation.model_validate(state['evaluation']))
    return _advance(state, 'decide', automated_assessment=assessment.model_dump(mode='json'),
                    automated_decision=assessment.decision,
                    review_status='pending' if assessment.decision == 'Needs a person to check' else 'not_required')


def human_review_node(state):
    # Pure payload construction only before interrupt: this node restarts on resume.
    payload = {
        'thread_id': state['thread_id'], 'supplier_id': state['pack_id'], 'pack_id': state['pack_id'],
        'review_token': state['review_token'], 'original_decision': state['automated_decision'],
        'review_reasons': state['automated_assessment']['reasons'], 'source_evidence': state['evidence'],
        'allowed_actions': ['approve', 'reject', 'request_correction'], 'reason_required': True,
    }
    action = ReviewAction.model_validate(interrupt(payload))
    _validate_identity(state, action)
    audit = action.model_dump(mode='json')
    audit['timestamp_utc'] = datetime.now(timezone.utc).isoformat()
    outcome = {'approve': 'Approve', 'reject': 'Reject', 'request_correction': 'Correction requested'}[action.action]
    return _advance(state, 'human_review', human_review=audit, review_status='completed', final_outcome=outcome)


def finalize_node(state):
    if state.get('error'):
        return _advance(state, 'finalize', final_outcome=None, review_status='error')
    if state['automated_decision'] == 'Needs a person to check':
        if not state.get('human_review'):
            raise ValueError('Cannot finalize without the required reviewer response.')
        outcome = state['final_outcome']
    else:
        outcome = state['automated_decision']
    return _advance(state, 'finalize', final_outcome=outcome)


def build_graph(checkpointer):
    graph = StateGraph(GraphState)
    nodes = [('extract', extract_node), ('validate', validate_node), ('match_pr', match_node),
             ('arithmetic', arithmetic_node), ('rules', rules_node), ('decide', decide_node)]
    for name, function in nodes:
        graph.add_node(name, _guard(name, function))
    graph.add_node('human_review', human_review_node)
    graph.add_node('finalize', finalize_node)
    graph.add_edge(START, 'extract')
    for index, (name, _) in enumerate(nodes[:-1]):
        following = nodes[index+1][0]
        graph.add_conditional_edges(name, lambda state: 'error' if state.get('error') else 'ok',
                                    {'error': 'finalize', 'ok': following})
    graph.add_conditional_edges('decide', lambda state: 'error' if state.get('error') else
                                'review' if state['automated_decision'] == 'Needs a person to check' else 'done',
                                {'error': 'finalize', 'review': 'human_review', 'done': 'finalize'})
    graph.add_edge('human_review', 'finalize')
    graph.add_edge('finalize', END)
    return graph.compile(checkpointer=checkpointer)


def _validate_identity(state, action):
    if (action.thread_id != state['thread_id'] or action.pack_id != state['pack_id'] or
            action.review_token != state['review_token']):
        raise ValueError('Review belongs to a different evaluation/thread.')


class GraphService:
    """Supported entry point; prevents accidental restarts, replay, and wrong-thread review.

    SQLite uses a separate coordination database transaction to serialize local
    writers across processes without holding a lock on the checkpoint database.
    Direct graph.invoke/update_state calls bypass these application guards.
    """
    def __init__(self, checkpointer, lock_database: Path | None = None):
        self.graph = build_graph(checkpointer)
        self._lock = threading.RLock()
        self.lock_database = lock_database

    @contextmanager
    def _exclusive(self):
        with self._lock, tracing_context(enabled=False):
            if self.lock_database is None:
                yield
            else:
                with closing(sqlite3.connect(self.lock_database, timeout=30)) as conn:
                    conn.execute('CREATE TABLE IF NOT EXISTS coordination (id INTEGER PRIMARY KEY)')
                    conn.commit()
                    conn.execute('BEGIN IMMEDIATE')
                    yield

    @staticmethod
    def _config(thread_id):
        if not isinstance(thread_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', thread_id):
            raise ValueError('Use a stable thread ID containing 1-128 letters, digits, underscores or hyphens.')
        return {'configurable': {'thread_id': thread_id}}

    def inspect(self, thread_id):
        config = self._config(thread_id)
        snapshot = self.graph.get_state(config)
        if not snapshot.values:
            raise ValueError('No checkpoint exists for that thread ID.')
        state = json.loads(json.dumps(snapshot.values, allow_nan=False))
        state['next_nodes'] = list(snapshot.next)
        state['interrupts'] = [item.value for task in snapshot.tasks for item in task.interrupts]
        if state['interrupts']:
            state['workflow_stage'] = 'human_review'
        return state

    def start(self, source, *, thread_id, filename=None, purchase_request_path=None, on_stage=None):
        config = self._config(thread_id)
        path = Path(source).resolve()
        pr = Path(purchase_request_path or SAMPLE_DIR / 'purchase-request.csv').resolve()
        name = filename or path.name
        if Path(name).name != name or not name.lower().endswith('.pdf'):
            raise ValueError('Use a plain PDF filename, not a path.')
        try:
            digest = sha256(path.read_bytes()).hexdigest()
            pr_digest = sha256(pr.read_bytes()).hexdigest()
        except OSError as exc:
            raise ValueError(f'Cannot register input: {exc}') from exc
        with self._exclusive():
            existing = self.graph.get_state(config)
            if existing.values:
                old = existing.values
                if (old['input_sha256'], old['pr_sha256'], old['input_filename']) != (digest, pr_digest, name):
                    raise ValueError('Thread already belongs to different input. Use a new evaluation ID.')
                # Recover a process that stopped between nodes; never consume a review interrupt.
                pending = self.inspect(thread_id)
                if existing.next and not pending['interrupts'] and not pending.get('error'):
                    for update in self.graph.stream(None, config=config, stream_mode='updates'):
                        if on_stage:
                            for node_name in update:
                                if node_name != '__interrupt__':
                                    on_stage(node_name)
                return self.inspect(thread_id)  # Reruns never restart a saved evaluation.
            initial = dict(thread_id=thread_id, pack_id=Path(name).stem, input_path=str(path),
                           input_filename=name, input_sha256=digest, pr_path=str(pr), pr_sha256=pr_digest,
                           review_token=uuid4().hex, trace=[], warnings=[], review_status='not_started',
                           human_review=None, final_outcome=None, error=None)
            for update in self.graph.stream(initial, config=config, stream_mode='updates'):
                if on_stage:
                    for name in update:
                        if name != '__interrupt__':
                            on_stage(name)
            return self.inspect(thread_id)

    def resume(self, thread_id, payload):
        config = self._config(thread_id)
        action = ReviewAction.model_validate(payload)  # Reject bad payload before consuming interrupt.
        with self._exclusive():
            state = self.inspect(thread_id)
            _validate_identity(state, action)
            if state.get('human_review'):
                previous = state['human_review']
                if all(previous.get(key) == value for key, value in action.model_dump().items()):
                    if state['next_nodes'] == ['finalize']:
                        self.graph.invoke(None, config=config)
                        return self.inspect(thread_id)
                    return state  # Identical retry is idempotent; audit timestamp is unchanged.
                raise ValueError('Review already completed; a second or changed submission is not allowed.')
            if not state['interrupts'] or state['next_nodes'] != ['human_review'] or state['review_status'] != 'pending':
                raise ValueError('Thread is not awaiting human review.')
            self.graph.invoke(Command(resume=action.model_dump(mode='json')), config=config)
            return self.inspect(thread_id)


def memory_service():
    return GraphService(InMemorySaver())


@contextmanager
def sqlite_service(path=None):
    path = Path(path or DEFAULT_DB).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with SqliteSaver.from_conn_string(str(path)) as saver:
        yield GraphService(saver, path.with_suffix('.coordination.sqlite3'))


def automatic_records(state):
    """Evaluation must use this assessment, never the human-overridden outcome."""
    if state.get('error'):
        raise ValueError(state['error'])
    return (_extraction(state), SupplierDecision.model_validate(state['automated_assessment']))


def run_graph_check(source, filename=None, on_stage=None, purchase_request_path=None):
    """Ephemeral automatic-only adapter for evaluation; interruptions stay unreviewed."""
    service = memory_service()
    state = service.start(source, thread_id=uuid4().hex, filename=filename,
                          purchase_request_path=purchase_request_path)
    if on_stage:
        for name in state['trace']:
            on_stage(name)
    return automatic_records(state)


def store_upload(data: bytes, filename: str) -> Path:
    if Path(filename).name != filename or not filename.lower().endswith('.pdf'):
        raise ValueError('Use a plain PDF filename.')
    # Content-addressed copies leave supplied source files unchanged.
    directory = DEFAULT_DB.parent / 'uploads' / sha256(data).hexdigest()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    if not path.exists():
        temporary = directory / (uuid4().hex + '.tmp')
        temporary.write_bytes(data)
        temporary.replace(path)
    return path


def evaluation_thread_id(source: Path, pr_path: Path, namespace='eval') -> str:
    """Stable for the same evaluation inputs and implementation, independent of labels."""
    digest = sha256(Path(source).read_bytes() + Path(pr_path).read_bytes())
    for name in ('graph_workflow.py', 'rule_engine.py', 'rules_config.py', 'extractor.py', 'extraction_safety.py',
                 'pdf_parser.py', 'decision_engine.py', 'models.py', 'workflow.py', 'evidence.py'):
        digest.update((ROOT / 'src' / name).read_bytes())
    digest.update(Path(source).name.encode())
    return f'{namespace}_{digest.hexdigest()[:40]}'


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=DEFAULT_DB)
    commands = parser.add_subparsers(dest='command', required=True)
    start = commands.add_parser('start')
    start.add_argument('pdf', type=Path)
    start.add_argument('--thread-id', required=True)
    inspect_command = commands.add_parser('inspect')
    inspect_command.add_argument('--thread-id', required=True)
    resume = commands.add_parser('resume')
    resume.add_argument('--thread-id', required=True)
    resume.add_argument('--action', required=True, choices=['approve', 'reject', 'request_correction'])
    resume.add_argument('--reason', required=True)
    resume.add_argument('--submission-id', required=True)
    resume.add_argument('--reviewer')
    commands.add_parser('samples')
    args = parser.parse_args(argv)
    try:
        with sqlite_service(args.db) as service:
            if args.command == 'samples':
                from src.workflow import run_check
                for path in sorted(SAMPLE_DIR.glob('supplier-pack-*.pdf')):
                    thread_id = evaluation_thread_id(path, SAMPLE_DIR/'purchase-request.csv')
                    state = service.start(path, thread_id=thread_id)
                    _, automatic = automatic_records(state)
                    _, fallback = run_check(path)
                    if automatic.model_dump(mode='json') != fallback.model_dump(mode='json'):
                        raise ValueError(f'Workflow regression for {path.name}')
                    print(f"{path.name} | {automatic.decision} | {state['review_status']} | equivalent | {thread_id}")
                return 0
            if args.command == 'start':
                state = service.start(args.pdf, thread_id=args.thread_id)
            else:
                state = service.inspect(args.thread_id)
                if args.command == 'resume':
                    response = {key: state[key] for key in ('thread_id', 'pack_id', 'review_token')}
                    response.update(action=args.action, reason=args.reason,
                                    submission_id=args.submission_id, reviewer=args.reviewer)
                    state = service.resume(args.thread_id, response)
            if args.command != 'inspect':
                state = {key: state.get(key) for key in ('thread_id', 'pack_id', 'workflow_stage', 'trace',
                         'automated_decision', 'review_status', 'final_outcome', 'human_review', 'error')}
            print(json.dumps(state, indent=2, ensure_ascii=False))
        return 0
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.exit(1, f'Workflow failed: {exc}\n')


if __name__ == '__main__':
    raise SystemExit(main())

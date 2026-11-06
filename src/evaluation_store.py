"""Read-only saved-evaluation queries and presentation data, independent of Streamlit."""
from collections import Counter
import json
import sqlite3
from hashlib import sha256
from pathlib import Path
from src import graph_workflow
from src.pdf_parser import read_pdf, PDFReadError

DECISIONS = ('Approve', 'Reject', 'Needs a person to check')


def list_evaluations(database: Path | None = None) -> list[dict]:
    path = Path(database or graph_workflow.DEFAULT_DB)
    if not path.exists():
        return []
    try:
        records = []
        with graph_workflow.sqlite_service(path) as service:
            conn = service.graph.checkpointer.conn
            exists = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='checkpoints'").fetchone()
            if not exists:
                return []
            threads = conn.execute("SELECT DISTINCT thread_id FROM checkpoints WHERE checkpoint_ns = ''").fetchall()
            for (thread,) in threads:
                snapshot = service.graph.get_state({'configurable': {'thread_id': thread}})
                if not snapshot.values or 'input_filename' not in snapshot.values:
                    continue
                state = json.loads(json.dumps(snapshot.values, allow_nan=False))
                state['next_nodes'] = list(snapshot.next)
                state['interrupts'] = [item.value for task in snapshot.tasks for item in task.interrupts]
                if state['interrupts']:
                    state['workflow_stage'] = 'human_review'
                state['saved_at'] = snapshot.created_at
                records.append(state)
        return sorted(records, key=lambda state: state['saved_at'], reverse=True)
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        raise ValueError(f'Cannot load saved evaluations: {exc}') from exc


def latest_documents(records: list[dict]) -> list[dict]:
    latest = {}
    for state in sorted(records, key=lambda row: row.get('saved_at', ''), reverse=True):
        key = (state['input_filename'], state['input_sha256'])
        latest.setdefault(key, state)
    return list(latest.values())


def workflow_status(state: dict) -> str:
    if state.get('error'):
        return 'Failed'
    if state.get('review_status') == 'pending':
        return 'Awaiting review'
    if state.get('final_outcome') is not None:
        return 'Completed'
    return 'In progress'


def supplier_name(state: dict) -> str:
    field = state.get('extraction', {}).get('fields', {}).get('supplier', {})
    return str(field.get('normalized') or 'Supplier not extracted')


def dashboard_statistics(records: list[dict]) -> dict:
    documents = latest_documents(records)
    counts = Counter(row.get('automated_decision') for row in documents)
    return dict(total_evaluated=sum(counts[decision] for decision in DECISIONS),
                approved=counts['Approve'], rejected=counts['Reject'],
                needs_review=counts['Needs a person to check'],
                pending_reviews=sum(row.get('review_status') == 'pending' for row in records),
                failed_documents=sum(bool(row.get('error')) for row in documents),
                saved_runs=len(records))


def filter_evaluations(records, search='', decision='All decisions', status='All states', latest_only=True):
    rows = latest_documents(records) if latest_only else records
    search = search.strip().casefold()
    return [row for row in rows
            if (not search or search in ' '.join((supplier_name(row), row['input_filename'], row['thread_id'])).casefold())
            and (decision == 'All decisions' or row.get('automated_decision') == decision)
            and (status == 'All states' or workflow_status(row) == status)]


def evaluation_rows(records):
    return [{'Document': row['input_filename'], 'Supplier': supplier_name(row),
             'Automated decision': row.get('automated_decision') or 'Not available',
             'Workflow': workflow_status(row), 'Final outcome': row.get('final_outcome') or 'Not finalized',
             'Reviewer action': (row.get('human_review') or {}).get('action', 'No reviewer action'),
             'Last saved (UTC)': row.get('saved_at', 'Not recorded'), 'Thread ID': row['thread_id']}
            for row in records]


def validate_upload(filename: str, data: bytes):
    if not isinstance(filename, str) or Path(filename).name != filename:
        raise PDFReadError('Use a plain PDF filename without folder paths.')
    return read_pdf(data, filename)


def export_evaluation(state: dict) -> str:
    """Keep original assessment, human audit and outcome separate in a JSON export."""
    return json.dumps({
        'thread_id': state['thread_id'], 'pack_id': state['pack_id'],
        'workflow_status': workflow_status(state), 'workflow_stage': state.get('workflow_stage'),
        'automated_decision': state.get('automated_decision'),
        'automated_assessment': state.get('automated_assessment'),
        'extraction': state.get('extraction'), 'evidence': state.get('evidence', []),
        'human_review': state.get('human_review'), 'final_outcome': state.get('final_outcome'),
        'error': state.get('error'),
        'accuracy_note': 'Only automated decisions are scored against reviewed expected labels.',
    }, indent=2, ensure_ascii=False, allow_nan=False)


def original_pdf(state: dict) -> bytes:
    """Only present source bytes that still match the assessed document."""
    try:
        data = Path(state['input_path']).read_bytes()
    except OSError as exc:
        raise ValueError(f'Original PDF is not available: {exc}') from exc
    if sha256(data).hexdigest() != state['input_sha256']:
        raise ValueError('Original PDF has changed since assessment; it cannot be shown as the assessed source.')
    return data

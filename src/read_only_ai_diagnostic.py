"""Opt-in local metadata reader: no application imports, network, or source writes."""
import argparse
from contextlib import contextmanager
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
from tempfile import TemporaryDirectory

import msgpack

STAGES = frozenset(('registered','ingest','extract','verify','trust','match_pr','rules','decide','human_review','finalize'))
CODES = frozenset(('DISABLED','CONFIGURATION','MISSING_API_KEY','CONSENT_REQUIRED','UNSUPPORTED_PROVIDER','INCOMPLETE_INPUT','INPUT_LIMIT','OUTPUT_LIMIT','TIMEOUT','QUOTA_EXCEEDED','NETWORK','AUTHENTICATION','MODEL_UNAVAILABLE','INVALID_REQUEST','INVALID_RESPONSE','RESPONSE_BLOCKED','REFERENCE_INVALID','TRUST_VIOLATION','SDK_UNAVAILABLE','LIVE_CALL_BLOCKED','PROVIDER_FAILURE'))
CATEGORIES = frozenset(('AUTHENTICATION','AUTHORIZATION','MODEL_UNAVAILABLE','SCHEMA_COMPLEXITY','RESPONSE_SCHEMA','INVALID_PARAMETER','INVALID_REQUEST','SDK_SERIALIZATION','TIMEOUT','NETWORK','QUOTA_EXCEEDED','PROVIDER_FAILURE','PROVIDER_UNAVAILABLE'))

class DiagnosticError(ValueError):
    """Fixed public error code only."""


class OperationError(DiagnosticError):
    def __init__(self, name, error):
        category = ('ACCESS_DENIED' if isinstance(error, PermissionError) else
                    'SQLITE_ERROR' if isinstance(error, sqlite3.Error) else 'FILESYSTEM_ERROR')
        super().__init__(category)
        self.safe = dict(diagnostic_error=category, operation=name,
                         errno=None, winerror=None)
        for key in ('errno','winerror'):
            value = getattr(error,key,None)
            if type(value) is int:
                self.safe[key] = value


@contextmanager
def operation(name):
    """Never infer an owner mismatch or disclose arbitrary exception strings."""
    try:
        yield
    except FileNotFoundError:
        raise  # Optional WAL absence is handled by snapshot().
    except (OSError,sqlite3.Error) as error:
        raise OperationError(name,error) from None


def bounded(path, limit=128_000_000, *, kind='FILE'):
    with operation(kind+'_LINK_CHECK'):
        linked = path.is_symlink()
    if linked:
        raise DiagnosticError('SYMLINK_REJECTED')
    with operation(kind+'_OPEN_READ'):
        stream = path.open('rb')
    try:
        with operation(kind+'_READ'):
            data = stream.read(limit+1)
    finally:
        with operation(kind+'_CLOSE'):
            stream.close()
    if len(data)>limit:
        raise DiagnosticError('SIZE_LIMIT')
    return data


def snapshot(db):
    # Never open the original database with SQLite, including its shared memory.
    def read():
        values = {db.name: bounded(db,kind='CHECKPOINT')}
        wal = db.with_name(db.name+'-wal')
        try:
            values[wal.name] = bounded(wal,kind='WAL')
        except FileNotFoundError:
            pass
        return values
    first = read()
    if first != read():
        raise DiagnosticError('DATABASE_CHANGING_TRY_WHEN_IDLE')
    return first


def unpack(kind, data):
    if kind == 'json':
        result = json.loads(data)
    elif kind == 'msgpack':
        def reject_extensions(code, payload):
            raise DiagnosticError('UNSUPPORTED_CHECKPOINT_EXTENSION')
        result = msgpack.unpackb(data, raw=False, strict_map_key=False, ext_hook=reject_extensions)
    else:
        raise DiagnosticError('UNSUPPORTED_CHECKPOINT_ENCODING')
    if not isinstance(result, dict):
        raise DiagnosticError('INVALID_CHECKPOINT')
    return result


def allow(value, allowed):
    return value if isinstance(value,str) and value in allowed else 'unknown'


def identifier(value):
    return value if isinstance(value,str) and re.fullmatch(r'[A-Za-z0-9_-]{1,96}',value) else 'unknown'


def report(state, checkpoint, artifacts):
    refs = state.get('artifacts', {})
    if not isinstance(refs,dict):
        raise DiagnosticError('INVALID_REFERENCES')
    extraction = {}
    reference = refs.get('extraction')
    if reference is not None:
        if not isinstance(reference,str) or not re.fullmatch(r'[0-9a-f]{64}',reference):
            raise DiagnosticError('INVALID_ARTIFACT_REFERENCE')
        data = bounded(artifacts/(reference+'.artifact'), 8_000_000,kind='ARTIFACT')
        if sha256(data).hexdigest()!=reference:
            raise DiagnosticError('ARTIFACT_HASH_MISMATCH')
        extraction = json.loads(data)
        if not isinstance(extraction,dict):
            raise DiagnosticError('INVALID_EXTRACTION_ARTIFACT')
    errors = extraction.get('errors') or []
    if not isinstance(errors,list):
        raise DiagnosticError('INVALID_EXTRACTION_ARTIFACT')
    error = errors[0] if errors and isinstance(errors[0],dict) else {}
    diagnostic = error.get('diagnostic') or {}
    if not isinstance(diagnostic,dict): diagnostic = {}
    status = diagnostic.get('http_status')
    status = status if type(status) is int and 100<=status<=599 else None
    validated = extraction.get('status')=='SCHEMA_VALIDATED'
    safe_error = state.get('safe_error') or {}
    events = state.get('audit_events') or []
    if not isinstance(safe_error,dict) or not isinstance(events,list):
        raise DiagnosticError('INVALID_CHECKPOINT')
    completed = [e.get('stage') for e in events if isinstance(e,dict) and e.get('event')=='stage_completed']
    timestamp = checkpoint.get('ts')
    try:
        timestamp = datetime.fromisoformat(timestamp).isoformat()
    except (TypeError,ValueError): timestamp = 'unknown'
    model = state.get('model_id')
    model = model if isinstance(model,str) and re.fullmatch(r'(?:models/)?gemini-[A-Za-z0-9._-]{1,100}',model) else 'unknown'
    attempts = extraction.get('attempts')
    code = allow(error.get('code') or safe_error.get('code'), CODES)
    # HTTP errors establish an error response, not usable generation content.
    schema = 'yes' if validated else ('no' if status is not None and status>=400 else 'unknown')
    return dict(run_id=identifier(state.get('thread_id')), timestamp=timestamp,
        backend='AI-assisted LangGraph', configured_model=model, timeout='unknown',
        attempt_count=attempts if type(attempts) is int and 0<=attempts<=6 else 'unknown',
        last_successful_stage=allow(completed[-1] if completed else None,STAGES),
        failing_stage=allow(safe_error.get('stage'),STAGES), http_status=status,
        error_category=allow(diagnostic.get('category'),CATEGORIES),
        error_origin='PROVIDER_HTTP' if status is not None else allow(diagnostic.get('timeout_origin'),{'PROVIDER_HTTP','LOCAL_CLIENT'}),
        sanitized_error_code=code,
        provider_response_received='yes' if status is not None or validated else 'unknown',
        schema_validation_reached=schema,
        evidence_verification_reached='yes' if 'verification' in refs else ('no' if safe_error.get('stage')=='extract' else 'unknown'),
        final_assessment_created='yes' if 'assessment' in refs else 'no')


def diagnose(runtime, filename=None):
    with operation('RUNTIME_RESOLVE'):
        root = Path(runtime).resolve(strict=True)/'ai_assisted'
    source = root/'ai_checkpoints.sqlite3'
    files = snapshot(source)
    # Restrictive temporary directory, removed on exit. No source SHM writes.
    with operation('SNAPSHOT_DIRECTORY_CREATE'):
        temporary_directory = TemporaryDirectory(prefix='supplier-diagnostic-')
    try:
        temporary = temporary_directory.name
        for name,data in files.items():
            kind = 'WAL' if name.endswith('-wal') else 'CHECKPOINT'
            with operation('SNAPSHOT_'+kind+'_WRITE'):
                (Path(temporary)/name).write_bytes(data)
        uri = (Path(temporary)/source.name).as_uri()+'?mode=ro'
        with operation('SNAPSHOT_SQLITE_OPEN'):
            connection = sqlite3.connect(uri,uri=True,timeout=0.2)
        try:
            with operation('SNAPSHOT_SQLITE_QUERY_ONLY'):
                connection.execute('PRAGMA query_only=ON')
            with operation('SNAPSHOT_SQLITE_SELECT'):
                rows = connection.execute("""SELECT c.type,c.checkpoint FROM checkpoints c
                WHERE checkpoint_ns='' AND NOT EXISTS
                (SELECT 1 FROM checkpoints n WHERE n.thread_id=c.thread_id
                 AND n.checkpoint_ns=c.checkpoint_ns AND n.checkpoint_id>c.checkpoint_id)
                ORDER BY checkpoint_id DESC LIMIT 1000""").fetchall()
            for kind,blob in rows:
                checkpoint = unpack(kind,blob)
                state = checkpoint.get('channel_values',{})
                if not isinstance(state,dict): continue
                if filename is not None and state.get('input_filename')!=filename: continue
                if state.get('processing_mode')!='gemini' or not state.get('safe_error'): continue
                return report(state,checkpoint,root/'private_artifacts')
        finally:
            with operation('SNAPSHOT_SQLITE_CLOSE'):
                connection.close()
    finally:
        with operation('SNAPSHOT_DIRECTORY_CLEANUP'):
            temporary_directory.cleanup()
    raise DiagnosticError('NO_MATCHING_FAILED_RUN')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime',required=True,type=Path)
    parser.add_argument('--filename',help='Exact local filename filter; never printed.')
    args = parser.parse_args()
    try:
        result = diagnose(args.runtime,args.filename)
    except OperationError as error:
        print(json.dumps(error.safe))
        return 1
    except DiagnosticError as error:
        print(json.dumps({'diagnostic_error':str(error)}))
        return 1
    except (OSError,sqlite3.Error,ValueError,TypeError,KeyError):
        print(json.dumps({'diagnostic_error':'RECORDS_UNAVAILABLE_OR_INVALID'}))
        return 1
    print(json.dumps(result,indent=2))
    return 0


if __name__=='__main__':
    raise SystemExit(main())

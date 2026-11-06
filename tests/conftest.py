"""Isolate runtime storage before test-module imports, including AppTest/subprocesses."""
from hashlib import sha256
import os
import sqlite3
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

ROOT = Path(__file__).resolve().parents[1]
EVALUATION_MODE = False


def pytest_addoption(parser):
    parser.addoption('--allow-evaluation-data',action='store_true',
        help='Explicit evaluation-only profile. Never use for extraction development.')


def pytest_configure(config):
    global EVALUATION_MODE, _live_before
    EVALUATION_MODE=config.getoption('--allow-evaluation-data')
    if EVALUATION_MODE:
        _live_before=storage_hashes(ROOT/'.data')
        return
    from src.development_isolation import install
    install(os.environ.get('SUPPLIER_DEVELOPMENT_ORIGINAL_ROOT'))
    synthetic=ROOT/'tests'/'synthetic'
    for argument in config.args:
        requested=Path(str(argument).split('::')[0]).resolve()
        if requested not in (ROOT,ROOT/'tests') and not requested.is_relative_to(synthetic):
            raise pytest.UsageError('Synthetic development accepts only tests/synthetic. Evaluation tests require an explicit evaluation-only run.')


def pytest_ignore_collect(collection_path,config):
    if config.getoption('--allow-evaluation-data'): return False
    path=Path(collection_path).resolve()
    return path.is_relative_to(ROOT/'tests') and path!=ROOT/'tests' and not path.is_relative_to(ROOT/'tests'/'synthetic')


def storage_hashes(directory):
    return {p.relative_to(directory).as_posix(): sha256(p.read_bytes()).hexdigest()
            for p in directory.rglob('*') if p.is_file()}


# This runs before test collection, not merely before individual test functions.
_live_before = None
_sandbox = TemporaryDirectory(prefix='supplier_checker_tests_')
_old_runtime = os.environ.get('SUPPLIER_CHECKER_RUNTIME_DIR')
_sandbox_root = Path(_sandbox.name).resolve()
os.environ['SUPPLIER_CHECKER_RUNTIME_DIR'] = str(_sandbox_root)


@pytest.fixture(scope='session', autouse=True)
def isolated_runtime_session():
    if EVALUATION_MODE:
        from src import graph_workflow
        assert graph_workflow.DEFAULT_DB.parent.resolve() == _sandbox_root
    original_connect = sqlite3.connect
    def isolated_connect(database, *args, **kwargs):
        if str(database) != ':memory:':
            target = Path(database).resolve()
            assert not target.is_relative_to((ROOT/'.data').resolve()), 'Tests attempted to open live SQLite storage'
        return original_connect(database, *args, **kwargs)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(sqlite3, 'connect', isolated_connect)
        if not EVALUATION_MODE:
            # PyMuPDF can open native file paths without Python's open event.
            import pymupdf
            from src.development_isolation import protected, EvaluationAccessError
            native_open=pymupdf.open
            def guarded_pdf_open(*args,**kwargs):
                filename=args[0] if args else kwargs.get('filename')
                if protected(filename):
                    raise EvaluationAccessError('EVALUATION_DATA_ACCESS_BLOCKED')
                return native_open(*args,**kwargs)
            patch.setattr(pymupdf,'open',guarded_pdf_open)
        yield _sandbox_root
        # Teardown occurs after the entire test session, including all UI tests.
        if EVALUATION_MODE:
            assert storage_hashes(ROOT / '.data') == _live_before, 'Tests changed live application storage'


def pytest_unconfigure(config):
    if _old_runtime is None:
        os.environ.pop('SUPPLIER_CHECKER_RUNTIME_DIR', None)
    else:
        os.environ['SUPPLIER_CHECKER_RUNTIME_DIR'] = _old_runtime
    _sandbox.cleanup()

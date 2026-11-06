"""Isolation probes use invented paths; never open an original document/label."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import pytest

from src.development_isolation import protected,EvaluationAccessError
from src.purchase_request import read_purchase_request,purchase_request_items
from src.gemini_minimal_contract import parse_minimal
from src.semantic_models import _semantic_fields
from supplier_cases import supplier_case


@pytest.mark.parametrize('name',['supplier-pack-01.pdf','supplier-pack-10.pdf','expected_decisions.csv','evaluation_results.json'])
def test_known_evaluation_reads_block_before_filesystem_access(tmp_path,name):
    # These nonexistent synthetic paths are not original evaluation paths.
    target=tmp_path/name
    assert protected(target)
    with pytest.raises(EvaluationAccessError,match='EVALUATION_DATA_ACCESS_BLOCKED'):
        target.read_bytes()


def test_native_pdf_path_cannot_bypass_the_guard(tmp_path):
    import pymupdf
    with pytest.raises(EvaluationAccessError,match='EVALUATION_DATA_ACCESS_BLOCKED'):
        pymupdf.open(tmp_path/'supplier-pack-03.pdf')


def test_runner_stages_no_evaluation_assets():
    project=Path(__file__).resolve().parents[2]
    if os.environ.get('SUPPLIER_DEVELOPMENT_ORIGINAL_ROOT'):
        assert not (project/'tests'/'expected_decisions.csv').exists()
        assert not list((project/'sample-data').rglob('*.pdf'))
        assert not (project/'.env').exists()


def test_only_permitted_reference_loader_uses_real_csv():
    rows=purchase_request_items(read_purchase_request())
    assert rows and all(row.source_filename=='purchase-request.csv' for row in rows)
    assert all(row.price_tax_basis is None for row in rows)


def test_ai_boundary_imports_no_legacy_extractor():
    command="import sys; import ui.ai_processing; assert 'src.extractor' not in sys.modules; assert 'src.graph_workflow' not in sys.modules; print('AI_IMPORT_ISOLATED')"
    result=subprocess.run([sys.executable,'-B','-c',command],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    assert result.stdout.strip()=='AI_IMPORT_ISOLATED'


def test_external_and_ordinary_loopback_connections_blocked_but_socketpair_works():
    for address in [('192.0.2.1',443),('127.0.0.1',443)]:
        with socket.socket() as sock:
            with pytest.raises(RuntimeError,match='NETWORK_BLOCKED'):
                sock.connect(address)
    a,b=socket.socketpair()
    try:
        a.sendall(b'synthetic');assert b.recv(9)==b'synthetic'
    finally: a.close();b.close()


def test_all_observed_field_leaves_support_missing_and_ambiguity():
    case=supplier_case(multiple=True)
    def locations(value,path=()):
        if isinstance(value,dict):
            if 'v' in value and 'e' in value: yield path
            else:
                for key,child in value.items(): yield from locations(child,(*path,key))
        elif isinstance(value,list):
            for i,child in enumerate(value): yield from locations(child,(*path,i))
    paths=list(locations(case.response()))
    assert len(paths)>30
    for path in paths:
        for state in ('missing','ambiguous'):
            payload=case.response();target=payload
            for key in path[:-1]: target=target[key]
            if state=='missing': target[path[-1]]=None
            else: target[path[-1]].update(s='AMBIGUOUS',r='Independent synthetic ambiguity.')
            parsed=parse_minimal(json.dumps(payload),case.document)
            states=[field.state for field in _semantic_fields(parsed)]
            assert ('MISSING' if state=='missing' else 'AMBIGUOUS') in states


def test_legacy_test_file_selection_requires_evaluation_profile():
    result=subprocess.run([sys.executable,'-B','-m','pytest','--collect-only','tests/test_extraction.py'],
        capture_output=True,text=True)
    assert result.returncode!=0
    assert 'Synthetic development accepts only' in result.stderr+result.stdout

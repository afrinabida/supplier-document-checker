"""Fixed, local extraction workflow and purchase request loading."""
from pathlib import Path
from collections.abc import Callable
from src.pdf_parser import read_pdf
from src.extractor import extract_fields, validate_structure
from src.evidence import collect_evidence

from src.purchase_request import SAMPLE_DIR, PR_COLUMNS, read_purchase_request, purchase_request_items

def run_workflow(source, filename=None, on_stage: Callable[[str], None] | None = None):
    stages = []
    def stage(name):
        stages.append(name)
        if on_stage:
            on_stage(name)
    stage('read')
    document = read_pdf(source, filename)
    stage('extract')
    result = extract_fields(document)
    stage('validate structure')
    validate_structure(result)
    stage('collect evidence')
    collect_evidence(result)
    result.stages = stages
    return result


def run_check(source, filename=None, on_stage=None, purchase_request_path=None):
    """Compose the preserved extraction workflow with deterministic evaluation."""
    from src.rule_engine import evaluate_rules
    from src.decision_engine import decide
    extraction = run_workflow(source, filename, on_stage)
    if on_stage:
        on_stage('match purchase request and evaluate rules')
    frame = read_purchase_request(purchase_request_path or SAMPLE_DIR / 'purchase-request.csv')
    evaluation = evaluate_rules(extraction, purchase_request_items(frame))
    if on_stage:
        on_stage('decide')
    return extraction, decide(evaluation)

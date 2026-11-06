"""Storage locations only; importing paths never imports an extraction workflow."""
import os
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
DEFAULT_DB=Path(os.environ.get('SUPPLIER_CHECKER_RUNTIME_DIR',ROOT/'.data'))/'workflows.sqlite3'

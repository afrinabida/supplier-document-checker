"""Presentation-only denylist for private evaluation PDFs; contains no document content."""
from hashlib import sha256
from pathlib import Path
import os

PRIVATE_PDF_SHA256 = frozenset(['315870f07e3b4de40f12380b0c163fcc87df87757da48979360b8e90689179c4', '319ef3280efc209db3f4a6153b7e6943d61bc0d9f05520c7dec6d541597cfd29', '4214b79d067a151b04f240e6d7633722cb1eabc3d15ab24e680ccb6845e2b991', '91d6d71b8df7e6288fb3719f46ff9290b2417d03eb8dc4dfb2581c309de434f0', '92ce3d956151b53a5d224096823493cbe4d9f9e2e8dde84c3693edfcff40bbf0', '973d6250733bc9bd5a497db5d1611f271192789f05e1e3c22e7a675fdd988c68', '9d81eaf2c58fd5000b5d3a0756295845520e9fb4fda31496bc4f017d79cd7722', 'b6ca30469ceb53c8cff569d47a394bd283e8472ab4f04025bf51ba658c97289b', 'cad4d465398dafe31e45c8b099617f004bf71cd2d2ec9f5e36d9542d4efa6bbc', 'd187b72a03d5e93d7aa8562abd7f0ed8cd5c08852e619b393d9a5724673e26c9'])


def is_private_document(data: bytes, filename: str = '') -> bool:
    return sha256(data).hexdigest() in PRIVATE_PDF_SHA256 or Path(filename).name.lower().startswith('supplier-pack-')


def manual_evaluation_enabled() -> bool:
    """Operator-only, loopback launcher gate; never discovers evaluation files."""
    if os.environ.get('SUPPLIER_MANUAL_EVALUATION') != 'true' or os.environ.get('SUPPLIER_LOCAL_ONLY') != '1':
        return False
    from src.ai_graph_workflow import implementation_hash
    return os.environ.get('SUPPLIER_FROZEN_ENGINE_SHA256') == implementation_hash()


def require_public_document(data: bytes, filename: str = '', *, allow_manual=False) -> None:
    """Block fixture bytes even if renamed; no changes to extraction or evaluation CLI."""
    if is_private_document(data, filename) and not (allow_manual and manual_evaluation_enabled()):
        raise ValueError('This private evaluation document is not available through the application. Upload an authorized supplier document.')


def require_single_manual_document(documents):
    if len(documents) != 1 and any(is_private_document(data,name) for name,data in documents):
        raise ValueError('Manual evaluation accepts exactly one individually uploaded PDF at a time.')

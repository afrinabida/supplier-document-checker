"""Explicit private, content-addressed artifacts; checkpoints contain hashes only.

This store does not implement encryption or user authentication. The application
must restrict filesystem access and retention. No arbitrary object deserialization.
"""
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from hashlib import sha256
import json
from pathlib import Path
import re
from uuid import uuid4
from pydantic import BaseModel
from src.ai_graph_models import AIGraphError


def private_json(value):
    """Intentional sensitive serialization, only for this private artifact store.

    Iterating declared fields preserves Step 5/6 private evidence and excludes
    computed fields. Secrets/config/provider objects are explicitly forbidden.
    """
    if isinstance(value,BaseModel):
        if type(value).__name__ in ('GeminiConfig','SecretStr','SecretBytes'):
            raise AIGraphError('SECRET_ARTIFACT_FORBIDDEN')
        return {name:private_json(getattr(value,name)) for name in type(value).model_fields}
    if isinstance(value,dict): return {str(k):private_json(v) for k,v in value.items()}
    if isinstance(value,(tuple,list)): return [private_json(v) for v in value]
    if isinstance(value,Enum): return value.value
    if isinstance(value,Decimal): return str(value)
    if isinstance(value,(date,datetime)): return value.isoformat()
    if value is None or isinstance(value,(str,int,float,bool)): return value
    raise AIGraphError('UNSUPPORTED_ARTIFACT_TYPE')


def canonical_bytes(value):
    return json.dumps(private_json(value),sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')


class AIArtifactStore:
    def __init__(self,root):
        self.root=Path(root).resolve()
        self.root.mkdir(parents=True,exist_ok=True)

    def _path(self,reference):
        if not isinstance(reference,str) or not re.fullmatch(r'[0-9a-f]{64}',reference):
            raise AIGraphError('INVALID_ARTIFACT_REFERENCE')
        target=self.root/(reference+'.artifact')
        # Reject redirection of individual artifact files as well as traversal.
        if target.is_symlink() or target.resolve().parent!=self.root:
            raise AIGraphError('UNSAFE_ARTIFACT_REFERENCE')
        return target

    def put_bytes(self,data):
        reference=sha256(data).hexdigest();path=self._path(reference)
        if path.exists():
            self.get_bytes(reference)
            return reference
        temporary=self.root/(uuid4().hex+'.pending')
        try:
            with temporary.open('xb') as stream: stream.write(data)
            temporary.replace(path)
        except OSError:
            raise AIGraphError('ARTIFACT_STORAGE_UNAVAILABLE') from None
        return reference

    def get_bytes(self,reference):
        try: data=self._path(reference).read_bytes()
        except OSError: raise AIGraphError('ARTIFACT_UNAVAILABLE') from None
        if sha256(data).hexdigest()!=reference: raise AIGraphError('ARTIFACT_HASH_MISMATCH')
        return data

    def put(self,value): return self.put_bytes(canonical_bytes(value))

    def get(self,reference,model=None):
        try:
            data=json.loads(self.get_bytes(reference))
            return model.model_validate(data) if model else data
        except (ValueError,TypeError) as error:
            if isinstance(error,AIGraphError): raise
            raise AIGraphError('INVALID_ARTIFACT') from None

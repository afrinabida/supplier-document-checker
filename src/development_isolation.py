"""Fail-closed accidental evaluation access guard for synthetic development only.

Not an OS security sandbox. The supported runner additionally stages only code,
synthetic tests and the permitted PR/rules, never evaluation documents or labels.
"""
import os
from pathlib import Path
import re
import sys
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
_roots = {ROOT}
_installed = False


class EvaluationAccessError(PermissionError):
    """Never include protected contents or paths in the diagnostic."""


def protected(path):
    if isinstance(path,int) or path is None:
        return False
    try:
        value = os.fsdecode(path)
        if value.startswith('file:'):
            value = unquote(urlsplit(value).path)
            if os.name == 'nt' and re.match(r'^/[A-Za-z]:',value): value=value[1:]
        target = Path(value).resolve()
    except (TypeError,ValueError,OSError):
        return False
    name=target.name.casefold()
    if re.fullmatch(r'supplier-pack-(?:0[1-9]|10)\.pdf',name): return True
    if name in ('expected_decisions.csv','evaluation_results.json','evaluation_results.md'): return True
    if any(p.casefold().startswith('supplier-gemini-isolated-') for p in target.parts): return True
    for root in _roots:
        try: parts=target.relative_to(root).parts
        except ValueError: continue
        if not parts: continue
        first=parts[0].casefold()
        if first in ('.data','docs'): return True
        if first=='sample-data' and parts[1:] not in (
                ('task1-procurement','purchase-request.csv'),('task1-procurement','company-rules.md')):
            return True
        if first=='tests' and len(parts)>1 and parts[1] not in ('synthetic','conftest.py','__pycache__'):
            return True
        if first=='tests' and len(parts)>2 and parts[1]=='__pycache__' and not name.startswith('conftest.'):
            return True
        if name.startswith('phase') and target.suffix.casefold()=='.md': return True
        if len(parts)==1 and name=='readme.md': return True
    return False


def audit(event,args):
    if event in ('open','sqlite3.connect') and args and protected(args[0]):
        raise EvaluationAccessError('EVALUATION_DATA_ACCESS_BLOCKED')


def install(*extra_roots):
    global _installed
    _roots.update(Path(p).resolve() for p in extra_roots if p)
    if not _installed:
        sys.addaudithook(audit)
        _installed=True

"""Fixed local-stage diagnostics. Never serialize exceptions, inputs or messages."""
from contextlib import contextmanager
from typing import Literal, get_args, get_origin
from pydantic import BaseModel, ConfigDict, Field, ValidationError

Stage = Literal['CONTEXT_BINDING','RESPONSE_LIMIT','JSON_SYNTAX','JSON_STRUCTURE',
    'RESPONSE_PYDANTIC','DOCUMENT_IDENTITY','SOURCE_PAGE','SOURCE_QUOTE','SOURCE_BLOCK',
    'DOMAIN_MAPPING','MINIMAL_STRUCTURE','DOMAIN_SERIALIZATION','ADAPTER_BOUNDARY',
    'DOMAIN_JSON_SYNTAX','DOMAIN_PYDANTIC','DOMAIN_STRUCTURE','PROVIDER_ENVELOPE','PROVIDER_TEXT']
ExceptionKind = Literal['JSONDecodeError','ValidationError','ValueError','TypeError',
    'RecursionError','ProviderFailure','PydanticSerializationError','Other']
ErrorKind = Literal['missing','extra_forbidden','string_type','int_type','int_parsing',
    'float_type','float_parsing','bool_type','bool_parsing','tuple_type','list_type',
    'dict_type','model_type','model_attributes_type','literal_error','value_error',
    'string_pattern_mismatch','string_too_short','string_too_long','greater_than_equal',
    'less_than_equal','greater_than','less_than','too_short','too_long','none_required',
    'date_type','date_parsing','date_from_datetime_parsing','decimal_type',
    'decimal_parsing','finite_number','recursion_loop','union_tag_invalid',
    'union_tag_not_found','enum','other']


class ValidationIssue(BaseModel):
    model_config=ConfigDict(extra='forbid',frozen=True)
    field_path: str = Field(max_length=256,pattern=r'^\$(?:\.[A-Za-z_][A-Za-z_0-9]*|\[\*\])*$')
    error_type: ErrorKind
    path_redacted: bool = False


class LocalValidationDiagnostic(BaseModel):
    model_config=ConfigDict(extra='forbid',frozen=True)
    stage: Stage
    exception_type: ExceptionKind
    issues: tuple[ValidationIssue,...] = Field(default=(),max_length=12)
    issues_truncated: bool = False


class LocalValidationFailure(ValueError):
    """Still a ValueError; its string/repr contain only a fixed stage code."""
    def __init__(self,diagnostic):
        super().__init__('LOCAL_VALIDATION_'+diagnostic.stage)
        self.validation_diagnostic=diagnostic


def _path(parts):
    return '$'+''.join('[*]' if isinstance(p,int) or p=='[*]' else '.'+p for p in parts)


def _allowed_paths(model,prefix):
    paths={_path(prefix)};branches=set()
    def walk(annotation,parts,seen=()):
        paths.add(_path(parts))
        if isinstance(annotation,type) and issubclass(annotation,BaseModel):
            if annotation in seen: return
            branches.add(annotation.__name__)
            for name,field in annotation.model_fields.items():
                walk(field.annotation,(*parts,name),(*seen,annotation))
        elif get_origin(annotation) in (tuple,list):
            args=get_args(annotation)
            if args: walk(args[0],(*parts,'[*]'),seen)
        else:
            for arg in get_args(annotation):
                if isinstance(arg,type) or get_origin(arg): walk(arg,parts,seen)
    if model is not None: walk(model,prefix)
    return paths,branches


def diagnostic_for(stage,error,*,model=None,prefix=()):
    name=type(error).__name__
    kind=name if name in get_args(ExceptionKind) else 'Other'
    issues=[];truncated=False
    if isinstance(error,ValidationError):
        # Do not copy msg, ctx, input, url or arbitrary custom error identifiers.
        errors=error.errors(include_input=False,include_context=False,include_url=False)
        paths,branches=_allowed_paths(model,prefix)
        if stage=='RESPONSE_PYDANTIC' and any(not e.get('loc') and e.get('type') in
                ('model_type','dict_type','model_attributes_type') for e in errors):
            stage='JSON_STRUCTURE'
        for entry in errors[:12]:
            parts=tuple(prefix);redacted=False
            for token in entry.get('loc',()):
                if isinstance(token,str) and token in branches: continue
                if not isinstance(token,(str,int)):
                    redacted=True;break
                candidate=(*parts,'[*]' if isinstance(token,int) else token)
                if _path(candidate) not in paths or len(_path(candidate))>256:
                    redacted=True;break
                parts=candidate
            error_type=entry.get('type')
            issues.append(ValidationIssue(field_path=_path(parts),path_redacted=redacted,
                error_type=error_type if error_type in get_args(ErrorKind) else 'other'))
        truncated=len(errors)>12
    return LocalValidationDiagnostic(stage=stage,exception_type=kind,
        issues=tuple(issues),issues_truncated=truncated)


def fail(stage):
    return LocalValidationFailure(LocalValidationDiagnostic(stage=stage,exception_type='ValueError'))


@contextmanager
def validation_boundary(stage,*,model=None,prefix=()):
    from src.llm_contracts import ProviderFailure
    try:
        yield
    except LocalValidationFailure:
        raise
    except ProviderFailure as error:
        # Provider HTTP/auth/timeout errors remain provider failures. Only the
        # known domain structural validators attach a local-boundary diagnostic.
        if stage in ('MINIMAL_STRUCTURE','DOMAIN_STRUCTURE') and error.validation_diagnostic is None:
            error.validation_diagnostic=diagnostic_for(stage,error,model=model,prefix=prefix)
        raise
    except (ValueError,TypeError,RecursionError) as error:
        raise LocalValidationFailure(diagnostic_for(stage,error,model=model,prefix=prefix)) from None

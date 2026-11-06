"""Synthetic-only local validation diagnostics. Never retain response contents."""
import json
import pytest
from pydantic import ValidationError
from pydantic_core import PydanticCustomError

from src.minimal_json_adapter import MinimalJsonAdapter
from src.gemini_minimal_contract import parse_minimal,response_model
from src.semantic_extraction import _claims_context,extract_supplier_document
from src.gemini_config import GeminiConfig
from src.llm_contracts import ProviderReply,ProviderFailure,ExtractionErrorCode
from src.local_validation import diagnostic_for,LocalValidationFailure
from supplier_cases import supplier_case
from test_gemini_workflow import execute

SECRET='PRIVATE-CREDENTIAL-AND-PERSONAL-CONTENT'


class MockTransport:
    def __init__(self,text): self.text=text;self.calls=0
    def generate_minimal_json(self,**kwargs):
        self.calls+=1
        return ProviderReply(text=self.text)


def generate(case,payload):
    text=payload if isinstance(payload,str) else json.dumps(payload)
    transport=MockTransport(text)
    adapter=MinimalJsonAdapter(case.document,transport=transport)
    return adapter.generate(system_prompt='',context=_claims_context(case.document),schema={},
        config=GeminiConfig(enabled=True,max_retries=0))


@pytest.mark.parametrize('kind,stage,error_type,path',[
    ('missing','RESPONSE_PYDANTIC','missing','$.delivery'),
    ('type','RESPONSE_PYDANTIC','string_type','$.supplier.name.v[*]'),
    ('extra','RESPONSE_PYDANTIC','extra_forbidden','$.supplier'),
    ('structure','JSON_STRUCTURE','model_type','$'),
    ('identity','DOCUMENT_IDENTITY',None,None),
    ('quote','SOURCE_QUOTE',None,None),
    ('page','SOURCE_PAGE',None,None),
    ('block','SOURCE_BLOCK',None,None),
    ('syntax','JSON_SYNTAX',None,None),
])
def test_distinct_boundaries_preserve_invalid_response_category(kind,stage,error_type,path):
    case=supplier_case();data=case.response()
    if kind=='missing': del data['delivery']
    elif kind=='type': data['supplier']['name']['v']=[{'private':SECRET}]
    elif kind=='extra': data['supplier'][SECRET]=SECRET
    elif kind=='structure': data=[]
    elif kind=='identity': data['document_id']='sha256:'+'0'*64
    elif kind=='quote': data['supplier']['name']['e'][0]['q']=SECRET
    elif kind=='page': data['supplier']['name']['e'][0]['p']=999
    elif kind=='block': data['supplier']['name']['e'][0]['b']=SECRET
    elif kind=='syntax': data='{"'+SECRET+'":'
    with pytest.raises(ProviderFailure) as caught:
        generate(case,data)
    error=caught.value
    assert error.code==ExtractionErrorCode.INVALID_RESPONSE and not error.retryable
    detail=error.validation_diagnostic
    assert detail.stage==stage
    assert SECRET not in detail.model_dump_json()+str(error)+repr(error)
    if error_type:
        assert detail.exception_type=='ValidationError'
        assert any(i.error_type==error_type and i.field_path==path for i in detail.issues)
    if kind=='extra': assert detail.issues[0].path_redacted


def test_mapping_type_error_keeps_original_exception_type(monkeypatch):
    import src.gemini_minimal_contract as contract
    case=supplier_case();data=case.response()
    def broken_mapping(*args,**kwargs): raise TypeError(SECRET)
    monkeypatch.setattr(contract,'SemanticEvidence',broken_mapping)
    with pytest.raises(ProviderFailure) as caught:
        generate(case,data)
    assert caught.value.code==ExtractionErrorCode.INVALID_RESPONSE
    detail=caught.value.validation_diagnostic
    assert detail.stage=='DOMAIN_MAPPING' and detail.exception_type=='TypeError'
    assert SECRET not in detail.model_dump_json()+str(caught.value)


def test_mapping_pydantic_errors_have_domain_paths(monkeypatch):
    import src.gemini_minimal_contract as contract
    case=supplier_case();data=case.response()
    error=ValidationError.from_exception_data(SECRET,[
        {'type':'string_type','loc':('supplier','name','raw_values',0),'input':{'private':SECRET}}])
    def invalid(cls,*args,**kwargs): raise error
    monkeypatch.setattr(contract.SemanticDocument,'model_validate',classmethod(invalid))
    with pytest.raises(ProviderFailure) as caught:
        generate(case,data)
    detail=caught.value.validation_diagnostic
    assert detail.stage=='DOMAIN_MAPPING' and detail.exception_type=='ValidationError'
    assert detail.issues[0].field_path=='$.supplier.name.raw_values[*]'
    assert SECRET not in detail.model_dump_json()


def test_serialization_failure_is_not_reported_as_incoming_json_failure(monkeypatch):
    import src.gemini_minimal_contract as contract
    case=supplier_case();data=case.response()
    def broken(*args,**kwargs): raise TypeError(SECRET)
    monkeypatch.setattr(contract.SemanticDocument,'model_dump_json',broken)
    with pytest.raises(ProviderFailure) as caught:
        generate(case,data)
    assert caught.value.validation_diagnostic.stage=='DOMAIN_SERIALIZATION'
    assert caught.value.validation_diagnostic.exception_type=='TypeError'


def test_sensitive_custom_error_identifiers_messages_and_keys_are_redacted():
    error=ValidationError.from_exception_data(SECRET,[dict(
        type=PydanticCustomError(SECRET,SECRET),loc=('supplier',SECRET),input=SECRET)])
    detail=diagnostic_for('RESPONSE_PYDANTIC',error,model=response_model())
    assert detail.issues[0].field_path=='$.supplier'
    assert detail.issues[0].error_type=='other' and detail.issues[0].path_redacted
    assert SECRET not in detail.model_dump_json()


def test_diagnostic_volume_is_bounded():
    case=supplier_case();data=case.response()
    for i in range(20): data[SECRET+str(i)]=SECRET
    with pytest.raises(ProviderFailure) as caught: generate(case,data)
    detail=caught.value.validation_diagnostic
    assert len(detail.issues)==12 and detail.issues_truncated
    assert all(i.field_path=='$' and i.path_redacted for i in detail.issues)


def test_valid_response_is_unmodified_and_has_no_diagnostic():
    case=supplier_case()
    reply=generate(case,case.response())
    assert json.loads(reply.text)['supplier']['name']['raw_values']==['Lantern Orchard Supplies']
    assert 'validation_diagnostic' not in json.loads(reply.text)


def test_response_limit_keeps_existing_output_limit_code():
    case=supplier_case();transport=MockTransport(json.dumps(case.response()))
    with pytest.raises(ProviderFailure) as caught:
        MinimalJsonAdapter(case.document,transport=transport).generate(system_prompt='',
            context=_claims_context(case.document),schema={},
            config=GeminiConfig(enabled=True,max_response_bytes=64))
    assert caught.value.code==ExtractionErrorCode.OUTPUT_LIMIT
    assert caught.value.validation_diagnostic.stage=='RESPONSE_LIMIT'


def test_context_binding_failure_never_calls_transport():
    case=supplier_case();transport=MockTransport(SECRET)
    with pytest.raises(ProviderFailure) as caught:
        MinimalJsonAdapter(case.document,transport=transport).generate(system_prompt='',
            context=SECRET,schema={},config=GeminiConfig(enabled=True))
    assert caught.value.code==ExtractionErrorCode.REFERENCE_INVALID
    assert caught.value.validation_diagnostic.stage=='CONTEXT_BINDING'
    assert transport.calls==0


def test_minimal_structural_failure_preserves_reference_error(monkeypatch):
    import src.gemini_minimal_contract as contract
    case=supplier_case();data=case.response()
    def invalid(*args): raise ProviderFailure(ExtractionErrorCode.REFERENCE_INVALID)
    monkeypatch.setattr(contract,'_structural_claim_check',invalid)
    with pytest.raises(ProviderFailure) as caught: generate(case,data)
    assert caught.value.code==ExtractionErrorCode.REFERENCE_INVALID
    assert caught.value.validation_diagnostic.stage=='MINIMAL_STRUCTURE'
    assert caught.value.validation_diagnostic.exception_type=='ProviderFailure'


def test_direct_parser_still_raises_value_error_with_safe_string():
    case=supplier_case()
    with pytest.raises(ValueError) as caught:
        parse_minimal(SECRET,case.document)
    assert isinstance(caught.value,LocalValidationFailure)
    assert SECRET not in str(caught.value)+repr(caught.value)


@pytest.mark.parametrize('kind,stage,code',[
    ('json','DOMAIN_JSON_SYNTAX',ExtractionErrorCode.INVALID_RESPONSE),
    ('pydantic','DOMAIN_PYDANTIC',ExtractionErrorCode.INVALID_RESPONSE),
    ('structure','DOMAIN_STRUCTURE',ExtractionErrorCode.TRUST_VIOLATION),
])
def test_outer_domain_validation_has_separate_diagnostics(kind,stage,code):
    case=supplier_case()
    text=(SECRET if kind=='json' else '{}' if kind=='pydantic' else
        case.claims.model_copy(update={'interpretation_method':'MANUAL'}).model_dump_json())
    class Provider:
        name='gemini';is_remote=False
        def generate(self,**kwargs): return ProviderReply(text=text)
    result=extract_supplier_document(case.document,adapter=Provider(),config=GeminiConfig(enabled=True,max_retries=0))
    assert result.status=='FAILED' and result.errors[0].code==code
    assert result.errors[0].validation_diagnostic.stage==stage
    assert SECRET not in result.errors[0].validation_diagnostic.model_dump_json()


def test_local_diagnostics_reach_graph_artifact_without_raw_response(tmp_path):
    from src.ai_graph_workflow import ai_sqlite_service
    from src.ai_graph_models import TrustedReviewer
    case=supplier_case();data=case.response();del data['delivery']
    state,calls=execute(tmp_path,case,payload=data)
    assert len(calls)==1 and state['safe_error']=={'stage':'extract','code':'INVALID_RESPONSE'}
    assert state['interrupts'] and 'assessment' not in state['artifacts']
    assert state['trusted_facts_status']=='NOT_STARTED'
    with ai_sqlite_service(tmp_path) as service:
        stored=service.review_materials(state['thread_id'],reviewer=TrustedReviewer(subject='synthetic',authorized=True))
    extraction=stored['extraction']
    assert extraction['raw_claims_json'] is None
    assert extraction['errors'][0]['validation_diagnostic']['stage']=='RESPONSE_PYDANTIC'
    assert extraction['errors'][0]['validation_diagnostic']['issues'][0]['field_path']=='$.delivery'


def test_provider_http_failure_is_not_misclassified_as_local_validation(tmp_path):
    state,_=execute(tmp_path,supplier_case(),status=504)
    from src.ai_graph_workflow import ai_sqlite_service
    from src.ai_graph_models import TrustedReviewer
    with ai_sqlite_service(tmp_path) as service:
        stored=service.review_materials(state['thread_id'],reviewer=TrustedReviewer(subject='synthetic',authorized=True))
    error=stored['extraction']['errors'][0]
    assert error['code']=='TIMEOUT' and error['diagnostic']['http_status']==504
    assert error['validation_diagnostic'] is None


@pytest.mark.parametrize('empty_envelope,stage',[(True,'PROVIDER_ENVELOPE'),(False,'PROVIDER_TEXT')])
def test_sdk_response_envelope_failures_have_distinct_fixed_stages(empty_envelope,stage):
    import httpx
    from google import genai
    from pydantic import SecretStr
    from src.gemini_provider import GeminiProvider
    def handler(request):
        return httpx.Response(200,json={'candidates':[] if empty_envelope else [
            {'content':{'role':'model','parts':[{'text':''}]},'finishReason':'STOP'}]})
    def factory(**kwargs):
        options=kwargs['http_options'].model_copy(update={'httpx_client':httpx.Client(transport=httpx.MockTransport(handler))})
        return genai.Client(**{**kwargs,'http_options':options})
    with pytest.raises(ProviderFailure) as caught:
        GeminiProvider(client_factory=factory).generate_minimal_json(system_prompt='',context='Synthetic',
            config=GeminiConfig(enabled=True,api_key=SecretStr('synthetic-only'),max_retries=0))
    assert caught.value.code==ExtractionErrorCode.INVALID_RESPONSE
    assert caught.value.validation_diagnostic.stage==stage

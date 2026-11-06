"""Opt-in native JSON adapter bridging into the original untrusted claim boundary."""
from src.gemini_provider import GeminiProvider
from src.gemini_minimal_contract import proposal,parse_minimal,VERSION,PROMPT_VERSION
from src.semantic_extraction import _claims_context
from src.llm_contracts import ProviderReply,ProviderFailure,ExtractionErrorCode as Code
from src.local_validation import (LocalValidationDiagnostic,LocalValidationFailure,
    validation_boundary,diagnostic_for)
from src.semantic_models import SemanticDocument


class MinimalJsonAdapter:
    name='gemini'
    is_remote=True
    contract_version=VERSION
    prompt_version=PROMPT_VERSION

    def __init__(self,document,*,transport=None):
        self.document=document
        self.transport=transport or GeminiProvider()

    def generate(self,*,system_prompt,context,schema,config):
        # Bind the supplied document/context, not provider-returned identity alone.
        if context!=_claims_context(self.document):
            raise ProviderFailure(Code.REFERENCE_INVALID,validation_diagnostic=LocalValidationDiagnostic(
                stage='CONTEXT_BINDING',exception_type='ProviderFailure'))
        plan=proposal(self.document)
        size=len((plan['context']+plan['system_instruction']).encode())
        if size>config.max_input_bytes or size+1024>config.max_input_tokens: raise ProviderFailure(Code.INPUT_LIMIT)
        reply=self.transport.generate_minimal_json(system_prompt=plan['system_instruction'],context=plan['context'],config=config)
        try:
            with validation_boundary('RESPONSE_LIMIT'):
                if len(reply.text.encode())>min(config.max_response_bytes,1_000_000):
                    raise ProviderFailure(Code.OUTPUT_LIMIT,validation_diagnostic=LocalValidationDiagnostic(
                        stage='RESPONSE_LIMIT',exception_type='ProviderFailure'))
            claims=parse_minimal(reply.text,self.document)
            # Mapping/serialization errors are distinct from incoming JSON errors.
            with validation_boundary('DOMAIN_SERIALIZATION',model=SemanticDocument):
                serialized=claims.model_dump_json()
                result=ProviderReply(text=serialized,usage=reply.usage)
        except ProviderFailure: raise
        except LocalValidationFailure as error:
            raise ProviderFailure(Code.INVALID_RESPONSE,validation_diagnostic=error.validation_diagnostic) from None
        except (ValueError,TypeError,RecursionError) as error:
            raise ProviderFailure(Code.INVALID_RESPONSE,
                validation_diagnostic=diagnostic_for('ADAPTER_BOUNDARY',error)) from None
        # This is a validated transport mapping, not verification or a decision.
        return result

"""Official SDK transport with safe errors; imports/client creation are lazy."""
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import math
import os
import re
import sys

from src.llm_contracts import ExtractionErrorCode as Code, ProviderFailure, ProviderReply, UsageMetadata
from src.llm_contracts import ProviderDiagnostic
from src.gemini_diagnostics import provider_diagnostic
from src.gemini_schema import gemini_response_schema
from src.local_validation import LocalValidationDiagnostic


def _retry_delay(error):
    response=getattr(error,'response',None)
    headers=getattr(response,'headers',{}) or {}
    value=headers.get('retry-after')
    if value is not None:
        try:
            seconds=float(value)
        except (ValueError,TypeError):
            try:
                seconds=(parsedate_to_datetime(value)-datetime.now(timezone.utc)).total_seconds()
            except (ValueError,TypeError,OverflowError):
                seconds=None
        if seconds is not None and math.isfinite(seconds):
            return max(0,seconds)
    details=getattr(error,'details',{})
    if isinstance(details,dict):
        data=details.get('error',details)
        for item in data.get('details',[]) if isinstance(data,dict) else []:
            if isinstance(item,dict) and str(item.get('@type','')).endswith('RetryInfo'):
                delay=str(item.get('retryDelay',''))
                if re.fullmatch(r'\d+(?:\.\d+)?s',delay):
                    return float(delay[:-1])
    return None


def _api_failure(error,*,secrets=()):
    diagnostic=provider_diagnostic(error,secrets=secrets)
    status=getattr(error,'code',None)
    if diagnostic.category in ('AUTHENTICATION','AUTHORIZATION'):
        return ProviderFailure(Code.AUTHENTICATION,diagnostic=diagnostic)
    if diagnostic.category=='MODEL_UNAVAILABLE':
        return ProviderFailure(Code.MODEL_UNAVAILABLE,diagnostic=diagnostic)
    if status==429:
        return ProviderFailure(Code.QUOTA_EXCEEDED,retryable=True,retry_after=_retry_delay(error),diagnostic=diagnostic)
    if status in (408,504):
        return ProviderFailure(Code.TIMEOUT,retryable=True,retry_after=_retry_delay(error),diagnostic=diagnostic)
    if status in (500,502,503):
        return ProviderFailure(Code.MODEL_UNAVAILABLE if status==503 else Code.PROVIDER_FAILURE,
                               retryable=True,retry_after=_retry_delay(error),diagnostic=diagnostic)
    return ProviderFailure(Code.INVALID_REQUEST,diagnostic=diagnostic)


class GeminiProvider:
    name='gemini'
    is_remote=True

    def __init__(self,*,client_factory=None):
        self._client_factory=client_factory

    def generate(self,*,system_prompt,context,schema,config):
        return self._generate(system_prompt=system_prompt,context=context,schema=schema,config=config,profile='supplier')

    def generate_minimal(self,*,stage,config):
        """Diagnostic A or B only; same transport, no tools and no AFC loop."""
        if stage not in ('A','B'): raise ValueError('Select diagnostic stage A or B.')
        schema=({'type':'object','properties':{'ok':{'type':'boolean'}},
            'required':['ok'],'additionalProperties':False} if stage=='B' else None)
        return self._generate(system_prompt=None,context='Return only OK.' if stage=='A' else 'Return only a JSON object with ok set to true.',
            schema=schema,config=config,profile='minimal')

    def generate_isolation(self,*,variant,context,config):
        """Diagnostic-only wire contract; production extraction is unchanged."""
        from src.gemini_isolation import isolation_schema
        from src.extraction_prompt import SYSTEM_PROMPT
        return self._generate(system_prompt=None if variant=='FULL_MIN' else SYSTEM_PROMPT,
            context=context,schema=isolation_schema(variant),config=config,
            profile='minimal' if variant=='FULL_MIN' else 'supplier',schema_is_wire=True)

    def generate_compatibility(self,*,context,config):
        """Explicit grouped-schema diagnostic only; never selected by production."""
        from src.gemini_compatibility import compatibility_schema
        from src.extraction_prompt import SYSTEM_PROMPT
        return self._generate(system_prompt=SYSTEM_PROMPT,context=context,
            schema=compatibility_schema(),config=config,profile='supplier',schema_is_wire=True)

    def generate_minimal_json(self,*,system_prompt,context,config):
        """Explicit diagnostic-only JSON mode; no provider response schema."""
        return self._generate(system_prompt=system_prompt,context=context,schema=None,
            config=config,profile='supplier',json_mode=True)

    def _generate(self,*,system_prompt,context,schema,config,profile,schema_is_wire=False,json_mode=False):
        if not config.enabled:
            raise ProviderFailure(Code.DISABLED)
        if config.api_key is None or not config.api_key.get_secret_value().strip():
            raise ProviderFailure(Code.MISSING_API_KEY)
        if self._client_factory is None and ('PYTEST_CURRENT_TEST' in os.environ or 'pytest' in sys.modules):
            raise ProviderFailure(Code.LIVE_CALL_BLOCKED)
        try:
            from google import genai
            from google.genai import types, errors
            import httpx
        except ImportError:
            raise ProviderFailure(Code.SDK_UNAVAILABLE) from None
        client=None
        try:
            factory=self._client_factory or genai.Client
            client=factory(api_key=config.api_key.get_secret_value(),vertexai=False,
                http_options=types.HttpOptions(timeout=round(config.timeout_seconds*1000),
                                              retry_options=types.HttpRetryOptions(attempts=1)))
            options=dict(automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                max_output_tokens=config.max_output_tokens if profile=='supplier' else min(config.max_output_tokens,2048))
            if profile=='supplier': options.update(system_instruction=system_prompt,temperature=0)
            if schema is not None:
                options.update(response_mime_type='application/json',response_json_schema=schema if schema_is_wire else gemini_response_schema(schema))
            elif json_mode:
                options['response_mime_type']='application/json'
            response=client.models.generate_content(model=config.model,contents=context,config=types.GenerateContentConfig(**options))
            feedback=getattr(response,'prompt_feedback',None)
            if feedback and getattr(feedback,'block_reason',None):
                raise ProviderFailure(Code.RESPONSE_BLOCKED)
            candidates=getattr(response,'candidates',None) or []
            if len(candidates)!=1:
                raise ProviderFailure(Code.INVALID_RESPONSE,validation_diagnostic=LocalValidationDiagnostic(
                    stage='PROVIDER_ENVELOPE',exception_type='ProviderFailure'))
            finish=getattr(candidates[0],'finish_reason',None)
            finish=getattr(finish,'value',finish)
            if finish!='STOP':
                raise ProviderFailure(Code.OUTPUT_LIMIT if finish=='MAX_TOKENS' else Code.RESPONSE_BLOCKED)
            text=response.text
            if not isinstance(text,str) or not text.strip():
                raise ProviderFailure(Code.INVALID_RESPONSE,validation_diagnostic=LocalValidationDiagnostic(
                    stage='PROVIDER_TEXT',exception_type='ProviderFailure'))
            usage=getattr(response,'usage_metadata',None)
            counts={}
            for name,attribute in [('input_tokens','prompt_token_count'),('output_tokens','candidates_token_count'),('total_tokens','total_token_count')]:
                value=getattr(usage,attribute,None)
                counts[name]=value if type(value) is int and value>=0 else None
            return ProviderReply(text=text,usage=UsageMetadata(**counts))
        except ProviderFailure:
            raise
        except errors.APIError as error:
            raise _api_failure(error,secrets=(config.api_key.get_secret_value(),)) from None
        except (httpx.TimeoutException,TimeoutError):
            raise ProviderFailure(Code.TIMEOUT,retryable=True,diagnostic=ProviderDiagnostic(
                category='TIMEOUT',exception_type='Timeout',detail='REQUEST_TIMEOUT',timeout_origin='LOCAL_CLIENT')) from None
        except (httpx.TransportError,ConnectionError,OSError):
            raise ProviderFailure(Code.NETWORK,retryable=True,diagnostic=ProviderDiagnostic(
                category='NETWORK',exception_type='TransportError',detail='TRANSPORT_UNAVAILABLE')) from None
        except (ValueError,TypeError) as error:
            raise ProviderFailure(Code.INVALID_REQUEST,diagnostic=provider_diagnostic(error,local=True,secrets=(config.api_key.get_secret_value(),))) from None
        except Exception:
            raise ProviderFailure(Code.PROVIDER_FAILURE) from None
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass  # Cleanup must not expose HTTP/credential diagnostics.

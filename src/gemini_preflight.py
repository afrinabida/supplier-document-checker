"""Explicit read-only Gemini model listing; never generates or selects a model."""
import os
import re
import sys

from src.llm_contracts import ProviderDiagnostic

NOTICE='Metadata access does not guarantee generateContent availability, capacity, quota or structured-output compatibility.'


def model_preflight(config, *, client_factory=None):
    result=dict(status='NOT_RUN',generation_calls=0,models=[],complete=False,
        configured_model=config.model,configured_model_advertised=False,diagnostics=[],notice=NOTICE)
    if client_factory is None and ('pytest' in sys.modules or 'PYTEST_CURRENT_TEST' in os.environ):
        result['reason']='LIVE_CALL_BLOCKED';return result
    if config.api_key is None or not config.api_key.get_secret_value().strip():
        result['reason']='MISSING_API_KEY';return result
    from google import genai
    from google.genai import types, errors
    import httpx
    from src.gemini_provider import _api_failure
    client=None
    try:
        client=(client_factory or genai.Client)(api_key=config.api_key.get_secret_value(),vertexai=False,
            http_options=types.HttpOptions(timeout=round(config.timeout_seconds*1000),
                retry_options=types.HttpRetryOptions(attempts=1)))
        models={}
        # Pagination is read-only; a failed later page must not authorize fallback.
        for model in client.models.list(config=types.ListModelsConfig(page_size=100)):
            name=model.name
            if not isinstance(name,str) or not re.fullmatch(r'models/[A-Za-z0-9][A-Za-z0-9._-]{0,127}',name): continue
            if config.api_key.get_secret_value() in name: continue
            if 'generateContent' not in (model.supported_actions or []): continue
            models[name]=dict(id=name,input_token_limit=model.input_token_limit,output_token_limit=model.output_token_limit)
            result['models']=sorted(models.values(),key=lambda m:m['id'])
        result.update(status='SUCCESS',complete=True,
            configured_model_advertised=('models/'+config.model.removeprefix('models/')) in models)
    except errors.APIError as error:
        failure=_api_failure(error,secrets=(config.api_key.get_secret_value(),))
        result.update(status='FAILED',reason=failure.code.value,
            diagnostics=[failure.diagnostic.model_dump(mode='json')] if failure.diagnostic else [])
    except (httpx.TimeoutException,TimeoutError):
        result.update(status='FAILED',reason='TIMEOUT',diagnostics=[ProviderDiagnostic(category='TIMEOUT',
            exception_type='Timeout',detail='REQUEST_TIMEOUT',timeout_origin='LOCAL_CLIENT').model_dump(mode='json')])
    except Exception:
        result.update(status='FAILED',reason='METADATA_UNAVAILABLE')
    finally:
        if client is not None:
            try: client.close()
            except Exception: pass
    return result


def listed_fallback(config,model,preflight):
    """Explicit diagnostic override only; fail closed on incomplete/unlisted metadata."""
    canonical='models/'+model.removeprefix('models/')
    if preflight.get('status')!='SUCCESS' or not preflight.get('complete'):
        raise ValueError('Complete metadata preflight required.')
    if canonical not in {m['id'] for m in preflight['models']}:
        raise ValueError('Selected model must advertise generateContent in this API listing.')
    return config.model_copy(update={'model':canonical.removeprefix('models/'),'max_retries':0})

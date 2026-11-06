"""Classify errors using private messages in memory; emit allowlisted codes only."""
from src.llm_contracts import ProviderDiagnostic
from src.gemini_error_details import error_details

STATUSES={'INVALID_ARGUMENT','UNAUTHENTICATED','PERMISSION_DENIED','NOT_FOUND',
    'RESOURCE_EXHAUSTED','DEADLINE_EXCEEDED','UNAVAILABLE','INTERNAL'}


def provider_diagnostic(error,*,local=False,secrets=()):
    status=getattr(error,'code',None)
    if type(status) is not int or not 100<=status<=599: status=None
    extracted=error_details(error,local=local,secrets=secrets)
    data=extracted['data']
    provider_status=data.get('status') or getattr(error,'status',None)
    provider_status=provider_status if isinstance(provider_status,str) and provider_status in STATUSES else 'UNKNOWN'
    # Never return these strings or arbitrary JSON metadata to callers/logs.
    message=extracted['message'].casefold()
    entries=data.get('details',[])
    if not isinstance(entries,list): entries=[]
    reasons={entry['reason'] for entry in entries if isinstance(entry,dict) and isinstance(entry.get('reason'),str)}
    if extracted['provider_reason']: reasons.add(extracted['provider_reason'])
    parameter='unknown'
    locations=' '.join(extracted['rejected_fields']).casefold()
    for field,aliases in (
        ('response_json_schema',('response_json_schema','responsejsonschema','response_schema','responseschema','response schema','json schema','schema')),
        ('response_mime_type',('response_mime_type','responsemimetype')),
        ('max_output_tokens',('max_output_tokens','maxoutputtokens')),
        ('temperature',('temperature',)),('thinking_config',('thinking_config','thinkingconfig')),
        ('tool_config',('tool_config','toolconfig')),('tools',('tools','function_declarations','functiondeclarations')),
        ('automatic_function_calling',('automatic_function_calling','automaticfunctioncalling')),
        ('system_instruction',('system_instruction','systeminstruction','developer instruction')),
        ('contents',('contents',))):
        if any(alias in message or alias in locations for alias in aliases): parameter=field;break
    if extracted['provider_reason'] in ('UNSUPPORTED_SCHEMA','INVALID_JSON_SCHEMA','SCHEMA_TOO_COMPLEX'):
        parameter='response_json_schema'
    if status==503:
        category='PROVIDER_UNAVAILABLE';detail='PROVIDER_ERROR'
    elif status in (408,504): category='TIMEOUT';detail='REQUEST_TIMEOUT'
    elif status==401 or reasons & {'API_KEY_INVALID','API_KEY_EXPIRED','API_KEY_NOT_FOUND'} or 'api key not valid' in message:
        category='AUTHENTICATION';detail='API_CREDENTIAL_REJECTED';parameter='api_key'
    elif status==403 or reasons & {'ACCESS_TOKEN_SCOPE_INSUFFICIENT','API_KEY_SERVICE_BLOCKED','SERVICE_DISABLED','BILLING_DISABLED'}:
        category='AUTHORIZATION';detail='ACCESS_DENIED'
    elif status==404 or reasons & {'MODEL_NOT_FOUND','MODEL_NOT_SUPPORTED'}:
        category='MODEL_UNAVAILABLE';detail='MODEL_NOT_FOUND_OR_UNAVAILABLE';parameter='model'
    elif status==429:
        category='QUOTA_EXCEEDED';detail='QUOTA_OR_RATE_LIMIT'
    elif status in (408,504): category='TIMEOUT';detail='REQUEST_TIMEOUT'
    elif local:
        category='SDK_SERIALIZATION';detail='LOCAL_REQUEST_SERIALIZATION_FAILED'
    elif status==400 and parameter!='unknown' and 'unknown name' in message:
        category='INVALID_PARAMETER';detail='GENERATION_PARAMETER_REJECTED'
    elif status==400 and parameter=='response_json_schema':
        complexity=extracted['provider_reason']=='SCHEMA_TOO_COMPLEX' or any(word in message for word in ('too complex','too many states','nesting','too deeply','state limit','maximum depth'))
        category='SCHEMA_COMPLEXITY' if complexity else 'RESPONSE_SCHEMA'
        detail='STRUCTURED_SCHEMA_STATE_OR_DEPTH_LIMIT' if complexity else 'STRUCTURED_SCHEMA_REJECTED'
    elif status==400 and parameter!='unknown':
        category='INVALID_PARAMETER';detail='GENERATION_PARAMETER_REJECTED'
    elif status==400:
        category='INVALID_REQUEST';detail='REQUEST_REJECTED_DETAILS_WITHHELD'
    else: category='PROVIDER_FAILURE';detail='PROVIDER_ERROR'
    if detail=='REQUEST_REJECTED_DETAILS_WITHHELD':
        if extracted['rejected_fields']: detail='PROVIDER_FIELD_REJECTED'
        elif extracted['provider_reason']: detail='PROVIDER_REASON_REPORTED'
        elif extracted['safe_message']=='request contains an invalid argument': detail='PROVIDER_GENERIC_INVALID_ARGUMENT'
        elif extracted['safe_message'] and extracted['safe_message']!='<redacted>': detail='PROVIDER_MESSAGE_NORMALIZED'
    name=type(error).__name__
    if name not in ('APIError','ClientError','ServerError','ValueError','TypeError','ValidationError'): name='Other'
    return ProviderDiagnostic(http_status=status,provider_status=provider_status,category=category,
        exception_type=name,parameter=parameter,detail=detail,
        timeout_origin='PROVIDER_HTTP' if category=='TIMEOUT' and status is not None else None,
        provider_reason=extracted['provider_reason'],rejected_fields=extracted['rejected_fields'],
        safe_message=extracted['safe_message'],
        schema_features=tuple(feature for feature in ('$defs','$ref','anyOf','oneOf','prefixItems',
            'pattern','default','additionalProperties','const') if feature.casefold() in message))

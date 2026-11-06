"""Lossy, vocabulary-limited provider explanations; no raw bodies or header dumps."""
import re
from functools import lru_cache
from src.semantic_models import SemanticDocument

REASONS=frozenset(('API_KEY_INVALID','API_KEY_EXPIRED','API_KEY_NOT_FOUND','API_KEY_SERVICE_BLOCKED',
    'API_KEY_HTTP_REFERRER_BLOCKED','API_KEY_IP_ADDRESS_BLOCKED','SERVICE_DISABLED','BILLING_DISABLED',
    'ACCESS_TOKEN_SCOPE_INSUFFICIENT','PERMISSION_DENIED','MODEL_NOT_FOUND','MODEL_NOT_SUPPORTED',
    'UNSUPPORTED_PARAMETER','UNSUPPORTED_SCHEMA','INVALID_JSON_SCHEMA','SCHEMA_TOO_COMPLEX',
    'INVALID_ARGUMENT','RATE_LIMIT_EXCEEDED','QUOTA_EXCEEDED','CONSUMER_INVALID','LOCATION_NOT_SUPPORTED'))

ROOTS=frozenset(('generation_config','generationConfig','response_json_schema','responseJsonSchema',
    'response_schema','responseSchema','tools','tool_config','toolConfig','contents','system_instruction',
    'systemInstruction','model','config'))

# Only generic diagnostic vocabulary and names from the application's schema can
# survive. Arbitrary words, identifiers, account names, quoted values and URLs do
# not. Even a malicious provider body cannot turn this into an unrestricted log.
WORDS=frozenset('''a an the at in of on for to by with without and or is are was be been
    not no cannot can must should may has have contains contain received request response
    invalid valid argument arguments parameter parameters payload field fields unknown
    name named value values type types supported unsupported required missing expected
    allowed allow found find unable submit generate generation content method operation
    enabled disabled available unavailable access denied permission permissions credential
    credentials key expired authentication authorization api service billing quota limit
    limits exceeded maximum minimum too many complex complexity nesting depth states
    state schema schemas json object array string number integer boolean null format
    properties property items union unions enum enums constraint constraints compiled
    compile compilation parse parsing input output token tokens count length size
    developer instruction instructions system function functions calling automatic tool
    declaration declarations use used set supported support model models endpoint
    incompatible conflict conflicting mutually exclusive empty only one multiple
    greater less than equal positive negative zero exceeds byte bytes responsejsonschema
    responseschema generationconfig responsemimetype maxoutputtokens thinkingconfig
    temperature max_output_tokens response_mime_type thinking_config disabled tools
    timeout malformed false true based required nesting return additional
    read only currently this that it does do please remove either both specified
    specifying accepted rejected failed failure violation violations invalid_argument
    bad_request fieldviolations rejected reason errors error generatecontent keyword keywords'''.split())


@lru_cache(maxsize=1)
def field_words():
    allowed=set(ROOTS)|{'properties','items','anyOf','oneOf','allOf','type','enum','format','required',
        'additionalProperties','default','const','pattern','minLength','maxLength','minimum','maximum',
        'minItems','maxItems','prefixItems','$defs','$ref','parts','text','function_declarations',
        'functionDeclarations','parameters','response_mime_type','responseMimeType','max_output_tokens',
        'maxOutputTokens','temperature','thinking_config','thinkingConfig','automatic_function_calling',
        'automaticFunctionCalling','disable'}
    def visit(node):
        if isinstance(node,dict):
            allowed.update(node.get('properties',{}))
            for value in node.values(): visit(value)
        elif isinstance(node,list):
            for value in node: visit(value)
    visit(SemanticDocument.model_json_schema())
    allowed.update('D'+str(i) for i in range(64))
    return frozenset(allowed)


def safe_field_path(value):
    if not isinstance(value,str) or len(value)>512: return None
    # Support common protobuf dot paths and bracketed schema property names.
    value=re.sub(r'''\[['"]([A-Za-z_$][A-Za-z0-9_$]*)['"]\]''',r'.\1',value)
    if not re.fullmatch(r'[A-Za-z_$][A-Za-z0-9_$]*(?:(?:\.[A-Za-z_$][A-Za-z0-9_$]*)|(?:\[\d{1,4}\]))*',value): return None
    root=value.split('.',1)[0].split('[',1)[0]
    if root not in ROOTS: return None
    return re.sub(r'[A-Za-z_$][A-Za-z0-9_$]*',lambda match: match[0] if match[0] in field_words() else 'redacted',value)[:384]


def safe_explanation(message,*,secrets=()):
    if not isinstance(message,str) or not message: return None
    message=message[:8192]
    for secret in secrets:
        if secret: message=message.replace(secret,' <redacted> ')
    message=re.sub(r'https?://\S+',' <redacted> ',message,flags=re.I)
    message=re.sub(r'(?i)(authorization|x-goog-api-key|api[-_ ]?key)\s*[:=][^\r\n]*',' <redacted> ',message)
    message=re.sub(r'''["']([^"'\r\n]*)["']''',lambda m:
        safe_field_path(m[1]) or (m[1] if m[1] in field_words() or m[1] in REASONS else '<redacted>'),message)
    output=[]
    for token in message.split():
        token=token.strip('.,:;()!?`')
        path=safe_field_path(token)
        if path: safe=path
        elif token.casefold() in WORDS: safe=token.casefold()
        elif token in field_words() or token in REASONS: safe=token
        else: safe='<redacted>'
        if not output or safe!='<redacted>' or output[-1]!=safe: output.append(safe)
        if len(output)>=100: break
    return ' '.join(output)[:1024] or None


def error_details(error,*,local=False,secrets=()):
    data=getattr(error,'details',{})
    if isinstance(data,dict): data=data.get('error',data)
    if not isinstance(data,dict): data={}
    if not data:
        # The original response is already in memory. Read its JSON only; never
        # send a request or expose response headers/URL/body as diagnostics.
        response=getattr(error,'response',None)
        if response is not None:
            try:
                payload=response.json()
                if isinstance(payload,dict):
                    candidate=payload.get('error',payload)
                    if isinstance(candidate,dict): data=candidate
            except (ValueError,TypeError,RuntimeError): pass
    raw_message=data.get('message')
    if not isinstance(raw_message,str): raw_message=getattr(error,'message',None)
    if not isinstance(raw_message,str): raw_message=str(error) if local else ''
    messages=[raw_message];reasons=[];fields=[]
    entries=data.get('details',[])
    if isinstance(entries,dict): entries=[entries]
    for entry in entries if isinstance(entries,list) else []:
        if not isinstance(entry,dict): continue
        reason=entry.get('reason')
        if isinstance(reason,str) and reason in REASONS: reasons.append(reason)
        violations=entry.get('fieldViolations',entry.get('field_violations',[]))
        for violation in violations[:8] if isinstance(violations,list) else []:
            if not isinstance(violation,dict): continue
            path=safe_field_path(violation.get('field'))
            if path and not any(secret and secret in path for secret in secrets): fields.append(path)
            if isinstance(violation.get('description'),str): messages.append(violation['description'])
    # Some errors report a path only in their message. Parse path-shaped tokens;
    # only recognized roots and schema words survive the same sanitizer.
    for token in re.findall(r'[A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*|\[\d{1,4}\])+',raw_message):
        path=safe_field_path(token)
        if path and not any(secret and secret in path for secret in secrets): fields.append(path)
    combined=' '.join(messages)
    return dict(data=data,message=combined,provider_reason=next(iter(reasons),None),
        rejected_fields=tuple(dict.fromkeys(fields))[:8],safe_message=safe_explanation(combined,secrets=secrets))

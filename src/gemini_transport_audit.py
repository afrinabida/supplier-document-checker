"""Credential-free, network-free audit of the pinned SDK client route defaults."""
import json
from importlib.metadata import version
from google import genai
from google.genai import types

VERSIONS=frozenset(('v1','v1beta','v1alpha','v1beta1'))
OFFICIAL='https://generativelanguage.googleapis.com'


def compare_clients(listing,generation):
    # Read only explicit fields. Never serialize HttpOptions: it contains headers.
    left=listing._api_client._http_options
    right=generation._api_client._http_options
    def describe(options):
        endpoint=(options.base_url or '').rstrip('/')
        return dict(api_version=options.api_version if options.api_version in VERSIONS else 'CUSTOM_OR_UNKNOWN',
            endpoint='GOOGLE_DEVELOPER_API' if endpoint==OFFICIAL else 'CUSTOM_REDACTED',
            custom_endpoint=endpoint!=OFFICIAL)
    return dict(listing=describe(left),generation=describe(right),
        api_version_matches=left.api_version==right.api_version,
        endpoint_matches=(left.base_url or '').rstrip('/')==(right.base_url or '').rstrip('/'),
        vertexai_matches=listing.vertexai==generation.vertexai,
        developer_api=not listing.vertexai and not generation.vertexai)


def offline_audit():
    clients=[]
    try:
        # Match both current construction sites; no credential or .env is read.
        for _ in range(2):
            clients.append(genai.Client(api_key='synthetic-route-audit-key',vertexai=False,
                http_options=types.HttpOptions(timeout=60000,retry_options=types.HttpRetryOptions(attempts=1))))
        return dict(sdk_version=version('google-genai'),network_calls=0,credentials_loaded=False,
            transport=compare_clients(*clients),
            scope='Current SDK defaults and process-level endpoint overrides only; historical request routes were not captured.')
    finally:
        for client in clients: client.close()


if __name__=='__main__': print(json.dumps(offline_audit(),indent=2))

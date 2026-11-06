"""Explicit, secret-safe Gemini configuration. Importing this module reads no env."""
import os
from pydantic import BaseModel, ConfigDict, Field, SecretStr

DEFAULT_GEMINI_MODEL='gemini-3.8-flash'


class GeminiConfig(BaseModel):
    model_config=ConfigDict(extra='forbid',frozen=True)
    enabled: bool = False
    api_key: SecretStr | None = Field(default=None,repr=False,exclude=True)
    model: str = Field(default=DEFAULT_GEMINI_MODEL,min_length=1,max_length=128,pattern=r'^[A-Za-z0-9._/-]+$')
    timeout_seconds: float = Field(default=30,ge=0.1,le=120,allow_inf_nan=False)
    max_retries: int = Field(default=2,ge=0,le=5)
    max_backoff_seconds: float = Field(default=8,ge=0,le=60,allow_inf_nan=False)
    max_input_bytes: int = Field(default=120_000,gt=0,le=1_000_000)
    max_input_tokens: int = Field(default=64_000,gt=0,le=1_000_000)
    max_output_tokens: int = Field(default=16_384,gt=0,le=65_536)
    max_response_bytes: int = Field(default=1_000_000,gt=0,le=5_000_000)

    @classmethod
    def from_env(cls,*,environ=None):
        # Validate public options before reading the credential. No raw env/error
        # values are included in the caller-facing configuration error.
        source=os.environ if environ is None else environ
        try:
            flag=source.get('ENABLE_GEMINI_EXTRACTION','false').strip().lower()
            if flag not in ('true','false','1','0'):
                raise ValueError
            public=cls(enabled=flag in ('true','1'),model=source.get('GEMINI_MODEL',DEFAULT_GEMINI_MODEL),
                timeout_seconds=float(source.get('GEMINI_TIMEOUT_SECONDS','30')),
                max_retries=int(source.get('GEMINI_MAX_RETRIES','2')))
        except (ValueError,TypeError):
            raise ValueError('Invalid Gemini configuration; check documented option types and bounds.') from None
        key=source.get('GEMINI_API_KEY')
        return public.model_copy(update={'api_key':SecretStr(key) if key and key.strip() else None})

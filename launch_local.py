"""Local-only bootstrap: literal .env settings, environment precedence, isolated storage."""
import argparse
from importlib.metadata import PackageNotFoundError, version
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from uuid import uuid4

ROOT = Path(__file__).resolve().parent
ENV_KEYS = frozenset({'ENABLE_GEMINI_EXTRACTION', 'GEMINI_API_KEY', 'GEMINI_MODEL',
                      'GEMINI_TIMEOUT_SECONDS', 'GEMINI_MAX_RETRIES'})
RUNTIME_PREFIX = 'supplier-gemini-isolated-'
MARKER = '.supplier-isolated.json'
CLAIM = '.launch-claimed'


class LaunchError(ValueError):
    """Only fixed safe messages; never include configuration or credential values."""


def load_local_env(path=None, *, environ=None):
    """Read only known literal settings. Existing process keys, even empty, win."""
    target = os.environ if environ is None else environ
    path = ROOT/'.env' if path is None else Path(path)
    if not path.exists():
        return
    values = {}
    try:
        with path.open('rb') as handle:
            data = handle.read(16_385)
        if len(data) > 16_384:
            raise ValueError
        for raw in data.decode('utf-8-sig').splitlines():
            line = raw.strip()
            if not line or line.startswith('#'):
                continue
            if line.startswith('export '):
                line = line[7:]
            if '=' not in line:
                raise ValueError
            key, value = line.split('=', 1)
            key, value = key.strip(), value.strip()
            if key not in ENV_KEYS:
                continue
            if key in values:
                raise ValueError
            if value.startswith(('"', "'")):
                match = re.fullmatch(r'([\"\'])(.*?)\1(?:\s*#.*)?', value)
                if not match:
                    raise ValueError
                value = match.group(2)
            else:
                value = re.split(r'\s+#', value, maxsplit=1)[0].rstrip()
            # Deliberately do not interpolate variables, expand escapes or execute text.
            values[key] = value
    except (OSError, UnicodeError, ValueError):
        raise LaunchError('Local environment file is unreadable or invalid; values are withheld.') from None
    for key, value in values.items():
        target.setdefault(key, value)


def configuration_summary(environ):
    from src.gemini_config import GeminiConfig
    try:
        config = GeminiConfig.from_env(environ=environ)
    except (ValueError, TypeError):
        raise LaunchError('Gemini configuration is invalid; values are withheld.') from None
    key = config.api_key.get_secret_value() if config.api_key else ''
    try:
        sdk = version('google-genai')
    except PackageNotFoundError:
        sdk = 'not installed'
    return dict(backend='AI-assisted' if config.enabled else 'Deterministic',
                credential_present=bool(key.strip()), model=config.model.replace(key, '<redacted>') if key else config.model,
                timeout_seconds=config.timeout_seconds, application_retries=0 if config.enabled else None,
                sdk=sdk, provider_availability='UNVERIFIED', cloud_consent_required=config.enabled,
                generation_calls=0)


def prepare_runtime():
    """Fresh OS-temp directory only; no existing store is opened or inspected."""
    root = Path(tempfile.mkdtemp(prefix=RUNTIME_PREFIX)).resolve()
    (root/MARKER).write_text(json.dumps({'kind': 'supplier-isolated-v1', 'id': uuid4().hex}), encoding='utf-8')
    return root


def _runtime_error(code, operation=None, error=None):
    # Only internal constants enter this message; never echo caller paths or values.
    details = ''
    if operation is not None:
        details = ' operation=' + operation
        for name in ('errno', 'winerror'):
            value = getattr(error, name, None)
            if type(value) is int:
                details += ' ' + name + '=' + str(value)
    return LaunchError('Prepared runtime rejected [' + code + '].' + details + ' No application was started.')


def validate_prepared_runtime(path):
    """Preserve strict location/marker/empty-store checks; identify the failing check."""
    operation = 'PATH_CONSTRUCTION'
    try:
        original = Path(path)
        operation = 'RUNTIME_RESOLVE'
        root = original.resolve(strict=True)
        operation = 'RUNTIME_STAT'
        if not root.is_dir():
            raise _runtime_error('RUNTIME_NOT_DIRECTORY')
        operation = 'RUNTIME_LINK_CHECK'
        if original.is_symlink():
            raise _runtime_error('RUNTIME_PATH_REDIRECTED')
        operation = 'TEMP_ROOT_RESOLVE'
        if root.parent != Path(tempfile.gettempdir()).resolve():
            raise _runtime_error('RUNTIME_TEMP_ROOT_MISMATCH')
        if not root.name.startswith(RUNTIME_PREFIX):
            raise _runtime_error('RUNTIME_NAME_INVALID')
        operation = 'CLAIM_STAT'
        if (root/CLAIM).exists():
            raise _runtime_error('RUNTIME_ALREADY_CLAIMED')
        marker_path = root/MARKER
        operation = 'MARKER_LINK_CHECK'
        if marker_path.is_symlink():
            raise _runtime_error('RUNTIME_MARKER_REDIRECTED')
        try:
            operation = 'MARKER_OPEN_READ'
            with marker_path.open('rb') as stream:
                operation = 'MARKER_READ'
                raw = stream.read(4097)
                operation = 'MARKER_CLOSE'
        except FileNotFoundError:
            raise _runtime_error('RUNTIME_MARKER_MISSING') from None
        try:
            if len(raw) > 4096:
                raise ValueError
            marker = json.loads(raw.decode('utf-8'))
        except (UnicodeError, ValueError):
            raise _runtime_error('RUNTIME_MARKER_INVALID') from None
        if (not isinstance(marker, dict) or marker.get('kind') != 'supplier-isolated-v1'
                or not isinstance(marker.get('id'), str)
                or not re.fullmatch(r'[0-9a-f]{32}', marker['id'])):
            raise _runtime_error('RUNTIME_MARKER_INVALID')
        operation = 'DIRECTORY_LIST'
        if {p.name for p in root.iterdir()} != {MARKER}:
            raise _runtime_error('RUNTIME_NOT_EMPTY')
    except LaunchError:
        raise
    except FileNotFoundError:
        raise _runtime_error('RUNTIME_NOT_FOUND') from None
    except PermissionError as error:
        raise _runtime_error('RUNTIME_PERMISSION_DENIED', operation, error) from None
    except (OSError, ValueError, TypeError) as error:
        raise _runtime_error('RUNTIME_FILESYSTEM_ERROR', operation, error) from None
    return root


def claim_runtime(path):
    root = validate_prepared_runtime(path)
    operation = 'CLAIM_CREATE_EXCLUSIVE'
    try:
        with (root/CLAIM).open('x', encoding='utf-8') as marker:
            operation = 'CLAIM_WRITE'
            marker.write('One-use isolated instance. Do not reuse this runtime for another server.\n')
            operation = 'CLAIM_CLOSE'
    except FileExistsError:
        raise _runtime_error('RUNTIME_ALREADY_CLAIMED') from None
    except PermissionError as error:
        raise _runtime_error('RUNTIME_PERMISSION_DENIED', operation, error) from None
    except OSError as error:
        raise _runtime_error('RUNTIME_CLAIM_FAILED', operation, error) from None
    return root


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--isolated', action='store_true', help='Create a fresh, one-use temporary runtime.')
    mode.add_argument('--prepared-runtime', type=Path, help='Use an unused runtime created by --prepare-only.')
    parser.add_argument('--prepare-only', action='store_true', help='Prepare isolated configuration without starting an app.')
    parser.add_argument('--check', action='store_true', help='Report safe configuration only; never start the app.')
    parser.add_argument('--port', type=int, default=None)
    args, streamlit_args = parser.parse_known_args(argv)
    try:
        if args.prepare_only and not args.isolated:
            raise LaunchError('Preparation requires --isolated.')
        if args.port is not None and not 1024 <= args.port <= 65535:
            raise LaunchError('Use an unprivileged local port from 1024 to 65535.')
        isolated = args.isolated or args.prepared_runtime is not None
        if isolated and streamlit_args:
            raise LaunchError('Isolated launch accepts only --port; arbitrary server overrides are disabled.')
        environment = dict(os.environ)
        load_local_env(environ=environment)
        # Only this launcher fixes the server to loopback. Manual evaluation may
        # not override server arguments or expose Streamlit static assets.
        if environment.get('SUPPLIER_MANUAL_EVALUATION') == 'true' and streamlit_args:
            raise LaunchError('Manual evaluation accepts no server overrides.')
        environment['SUPPLIER_LOCAL_ONLY'] = '1'
        summary = configuration_summary(environment)
        runtime = prepare_runtime() if args.isolated else (validate_prepared_runtime(args.prepared_runtime) if args.prepared_runtime else None)
        if runtime is not None:
            summary['runtime_directory'] = str(runtime)
            summary['isolated'] = True
        else:
            summary['isolated'] = False
        if args.check or args.prepare_only:
            print(json.dumps(summary, indent=2))
            return 0
        if runtime is not None:
            claim_runtime(runtime)
            environment['SUPPLIER_CHECKER_RUNTIME_DIR'] = str(runtime)
            environment['LANGSMITH_TRACING'] = 'false'
            environment['LANGCHAIN_TRACING_V2'] = 'false'
        port = args.port or (8502 if isolated else 8501)
        command = [sys.executable, '-m', 'streamlit', 'run', str(ROOT/'app.py'),
                   '--server.address', '127.0.0.1', '--server.port', str(port)]
        if isolated or environment.get('SUPPLIER_MANUAL_EVALUATION') == 'true':
            command += ['--server.enableStaticServing', 'false']
        command += streamlit_args
        print('Starting local application. Credentials are supplied in memory, never command arguments.')
        if runtime is not None:
            print('Isolated runtime: ' + str(runtime))
        return subprocess.run(command, cwd=ROOT, env=environment, check=False).returncode
    except LaunchError as error:
        print(str(error), file=sys.stderr)
        return 1
    except OSError:
        print('Local application could not start; details are withheld.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

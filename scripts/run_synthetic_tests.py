"""Stage only synthetic development inputs; run offline, never evaluation data."""
import os
from hashlib import sha256
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory

ROOT=Path(__file__).resolve().parents[1]


def main():
    if '--allow-evaluation-data' in sys.argv:
        raise SystemExit('Evaluation mode is forbidden in the synthetic runner.')
    permitted=ROOT/'sample-data'/'task1-procurement'
    before={name:sha256((permitted/name).read_bytes()).hexdigest()
            for name in ('purchase-request.csv','company-rules.md')}
    with TemporaryDirectory(prefix='supplier-synthetic-development-') as temporary:
        stage=Path(temporary)
        for name in ('src','ui'):
            shutil.copytree(ROOT/name,stage/name,ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copytree(ROOT/'tests'/'synthetic',stage/'tests'/'synthetic',ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copy2(ROOT/'tests'/'conftest.py',stage/'tests'/'conftest.py')
        shutil.copy2(ROOT/'pytest.ini',stage/'pytest.ini')
        shutil.copy2(ROOT/'launch_local.py',stage/'launch_local.py')
        reference=stage/'sample-data'/'task1-procurement'
        reference.mkdir(parents=True)
        for name in ('purchase-request.csv','company-rules.md'):
            shutil.copy2(ROOT/'sample-data'/'task1-procurement'/name,reference/name)
        # Inherit the guard in ordinary Python children before application imports.
        (stage/'sitecustomize.py').write_text(
            'import os\nfrom src.development_isolation import install\n'
            'install(os.environ.get("SUPPLIER_DEVELOPMENT_ORIGINAL_ROOT"))\n',encoding='utf-8')
        env={k:v for k,v in os.environ.items() if k not in ('GEMINI_API_KEY','GOOGLE_API_KEY','PYTHONPATH','PYTHONHOME')}
        env.update(ENABLE_GEMINI_EXTRACTION='false',PYTHONDONTWRITEBYTECODE='1',PYTHONUTF8='1',
            PYTHONPATH=str(stage),SUPPLIER_DEVELOPMENT_ORIGINAL_ROOT=str(ROOT),
            SUPPLIER_CHECKER_RUNTIME_DIR=str(stage/'runtime'),LANGSMITH_TRACING='false',LANGCHAIN_TRACING_V2='false')
        result=subprocess.run([sys.executable,'-B','-m','pytest','-q','-p','no:cacheprovider',
            '--basetemp',str(stage/'pytest-tmp'),*sys.argv[1:]],cwd=stage,env=env,check=False).returncode
    if before!={name:sha256((permitted/name).read_bytes()).hexdigest() for name in before}:
        raise SystemExit('Permitted reference inputs changed during synthetic validation.')
    print('Permitted PR and company rules unchanged; evaluation assets were not staged.')
    return result


if __name__=='__main__':
    raise SystemExit(main())

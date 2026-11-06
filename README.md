# AI Supplier Document Checker

Local Python/Streamlit application for comparing authorized supplier quotations
with a purchase request and procurement rules. Uncertain cases require human
review. This is a local demonstration, not a production-ready procurement system.

## Install and run

Python 3.13 is the tested development version. In Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
.\run.cmd
```

Open the local URL printed by Streamlit. `python launch_local.py` is the portable
launcher; direct `python -m streamlit run app.py` uses process environment settings.
The default deterministic mode does not require an API key.

## Configuration

`.env.example` contains safe defaults and an empty `GEMINI_API_KEY`. Keep actual
credentials in the ignored local `.env` or process environment. Existing process
environment values take precedence over the launcher-loaded `.env` settings.
`ENABLE_GEMINI_EXTRACTION=false` disables optional Gemini extraction.
`GEMINI_MODEL`, `GEMINI_TIMEOUT_SECONDS`, and `GEMINI_MAX_RETRIES` configure that
optional provider; the template model identifier is not an availability guarantee.
`SUPPLIER_CHECKER_RUNTIME_DIR` optionally changes the default `.data` runtime root.
Do not publish uploads, evidence, saved evaluations, checkpoints, or credentials.

## Workflow and stack

The stack includes Streamlit, PyMuPDF, pandas, Pydantic, LangGraph with SQLite
checkpoints, and optional google-genai integration. The local graph performs
extraction, validation, purchase-request matching, arithmetic, rules, and a decision.
The optional AI graph adds ingestion, source-evidence verification and trusted
facts before matching and rules. AI extracts information; deterministic rules make
the procurement assessment. Human review pauses unresolved cases, records a
written rationale, and preserves the original machine assessment separately.
Cloud processing requires explicit user consent and an explicit evaluation start.

The included fictional purchase request and company rules are required runtime
reference inputs. Upload only supplier documents you are authorized to process.
Private evaluation documents, answer keys and historical results are not included.

## Offline synthetic tests

```powershell
.\.venv\Scripts\python.exe -B scripts/run_synthetic_tests.py
```

The existing suite passed 133 synthetic tests before release preparation. Its
runner isolates runtime storage, uses authored fictional fixtures and mock provider
responses, and blocks external network calls. These tests do not establish live
Gemini end-to-end success or independently verified accuracy. No live Gemini call
or original evaluation PDF processing was performed for this submission.

## Limitations

Live Gemini supplier extraction remains unverified. Supported text layouts,
scanned-document handling, OCR, arbitrary languages and ambiguous policy terms
have limitations. Uncertain evidence requires review. Reviewer identity is
self-declared in this single-user demo. Shared hosting requires authentication,
authorization and data retention controls. Requirements mix pins and ranges;
clean-environment and cross-platform reproducibility remain unverified, including
direct imports of langsmith and httpx currently supplied through inherited or
transitive dependencies. Passing synthetic tests is not a safety certification.

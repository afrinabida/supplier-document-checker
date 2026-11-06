# AI Supplier Document Checker

Local Python/Streamlit application for comparing authorized supplier quotations
with a purchase request and procurement rules. Uncertain cases require human
review. This is a local demonstration, not a production-ready procurement system.

## Install and run

Python 3.13 is the tested development version. In Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
if (!(Test-Path .env)) { Copy-Item .env.example .env }
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

## Multiple-PDF batches

Select several PDFs using Ctrl/Shift (Command on macOS), or add files in several
selections. Give each document a distinct filename. Continue to Processing and
click Start Evaluation. AI mode also requires explicit cloud consent. The existing
LangGraph pipeline runs once per document, sequentially, with unique evaluation
and checkpoint IDs. Progress and the batch summary show each filename, outcome,
main reason and evaluation ID. A failed PDF is recorded as Processing Error and
does not stop other documents. Human Review pauses only that document's workflow.
Widget rerenders do not restart evaluations. An explicit new AI attempt creates
new IDs and preserves earlier saved evaluations. Batch summaries are session-local;
individual graph evaluations, evidence and audit histories remain in private SQLite
storage. Errors before graph creation remain in the session summary only.

## macOS / Linux setup

These commands have not been validated on those platforms:

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install -r requirements.txt
test -f .env || cp .env.example .env
./.venv/bin/python launch_local.py
```

## Offline synthetic tests

```powershell
.\.venv\Scripts\python.exe -B scripts/run_synthetic_tests.py
```

The offline regression runner isolates runtime storage, uses authored fictional fixtures and mock provider
responses, and blocks external network calls. These tests do not establish live
Gemini end-to-end success or independently verified accuracy. No live Gemini call
or original evaluation PDF processing was performed for this submission.

## Limitations

Live Gemini supplier extraction remains unverified. Supported text layouts,
scanned-document handling, OCR, arbitrary languages and ambiguous policy terms
have limitations. Uncertain evidence requires review. Reviewer identity is
self-declared in this single-user demo. Shared hosting requires authentication,
authorization and data retention controls. Requirements mix pins and ranges;
clean-environment and cross-platform reproducibility remain unverified. Direct
imports langsmith and httpx are declared at the tested versions, but a full
transitive dependency lockfile is not provided. Passing synthetic tests is not a safety certification.

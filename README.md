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

## LangGraph workflow configuration

LangGraph orchestrates a fixed, auditable supplier document evaluation workflow.
In AI-assisted mode, the workflow stages are:

1. PDF ingestion with PyMuPDF and supplier information extraction using Gemini.
2. Pydantic validation, source-evidence verification and trusted-fact construction.
3. Purchase request matching.
4. Decimal arithmetic validation.
5. Company rules evaluation.
6. Final automated decision: Approve, Reject or Human Review.

### Checkpointing and human review

SQLite checkpoints persist workflow progress and support human-in-the-loop review.
Each evaluation uses an isolated workflow identity and LangGraph thread/checkpoint
ID to prevent document state from mixing across evaluations. Uncertain assessments
pause for review; a recorded human disposition remains separate from the original
automated assessment.

### Runtime configuration

- LangGraph operates locally and does not require a separate API key.
- SQLite checkpoint storage provides workflow persistence.
- `SUPPLIER_CHECKER_RUNTIME_DIR` configures the application's runtime directory;
  the default is the project-local `.data` directory.
- Set runtime configuration before launching the application. The launcher loads
  only the documented Gemini settings from `.env`; set
  `SUPPLIER_CHECKER_RUNTIME_DIR` in the process environment.
- Gemini configuration is separate from LangGraph orchestration.
- Checkpoints, uploaded documents and saved evaluation data must remain private
  and must not be committed to GitHub.

The deterministic Python rule engine determines the automated procurement decision
using verified evidence. Gemini supplies untrusted extraction claims, not final
procurement decisions. The existing deterministic mode uses local extraction
without Gemini.

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

## Known Limitations

- **Document Extraction:** Gemini-based extraction supports the demonstrated supplier-document workflow, but complex tables, scanned PDFs, multilingual content, and inconsistent layouts require further validation.
- **Evidence Verification:** Ambiguous or insufficiently verified information is escalated for human review rather than used to make unsupported decisions.
- **Decision Accuracy:** Deterministic company rules are applied to verified evidence. Unclear contractual terms, incomplete evidence, and unspecified tax bases may prevent automatic decisions.
- **Security:** This single-user demonstration uses self-declared reviewer identities. Production deployment requires authentication, authorization, secure storage, and data retention controls.
- **Testing and Deployment:** Offline tests cover selected workflows, but unseen-document reliability, clean-environment reproducibility, and production readiness remain unverified.

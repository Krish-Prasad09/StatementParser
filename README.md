# Activv Money — Self-Learning PMS/AIF Statement Parser

A self-learning, fail-closed extraction and portfolio consolidation system for Portfolio Management Services (PMS) and Alternative Investment Funds (AIF) statements.

Built for the **Activv Money Internship Assignment** (Newcamp Finserv).

---

## Key Capabilities & Architectural Guarantees

- **True One-Shot Format Learning (`learner.py`, `models.py`):**
  - When an unseen statement format arrives, `FormatLearner` extracts and PII-redacts the document text, embeds the `FormatTemplate` JSON Schema and a worked example, and makes **1 LLM call** to synthesize a declarative `FormatTemplate`.
  - Every candidate template is validated against `FormatTemplate` (Pydantic V2), checked for PII leaks (`learning_failed_invalid_template`), executed deterministically by `StatementParser` on the sample document, and audited by `FinancialValidator` (`learning_failed_self_check`).
  - Subsequent statements matching that format are parsed **100% deterministically with 0 LLM calls**.
- **Blind Format Recognition & Provider Identity Guard (`fingerprinter.py`):**
  - Recognizes formats from SEBI registration numbers (`INP...`, `IN/AIF...`), normalized provider headers, layout geometry, and table column headers—with zero manifest or filename hints.
  - Never reuses a template across conflicting SEBI numbers or different provider names (`provider_mismatch_template`).
  - Detects both header sequence drift ($\text{similarity} < 0.75$) and individual renamed/missing mapped columns (`layout_drift_known_provider`, `expected_column_missing:<col>`).
- **Fail-Closed Financial Auditing (`validator.py`):**
  - Audits summary roll-forwards, holdings sum vs. stated Total Equity, holdings + cash vs. Closing NAV, summary cash vs. holdings-row cash (`cash_summary_vs_row_mismatch`), per-row $\text{Qty} \times \text{Price} = \text{Market Value}$, holding weights ($\approx 100\%$), ISO 6166 Mod-10 Luhn ISIN check digits, duplicate ISINs, transaction date bounds, and unmapped transaction types (`unmapped_transaction_type`).
- **Persistent SQLite Deduplication & Revision Detection (`persistence.py`, `watch_mailbox.py`):**
  - Every `ExtractionResult` carries its `sha256_hash` and is persisted to SQLite (`statements.db`).
  - Seen file hashes and `(provider, client_id, period_end)` keys are synced from SQLite on startup so duplicate files (`duplicate`) and revised statements (`revision_of_existing_statement`) are caught across process restarts.

---

## Project Structure

```text
├── models.py            # Pydantic V2 schemas: FormatTemplate, StatementRecord, ExtractionResult
├── utils.py             # ISO 6166 Mod-10 Luhn checksum, date & Indian accounting number cleaners
├── ingestion.py         # Three-way classifier, SHA-256 dedup, PDF decryption & EML attachment extractor
├── fingerprinter.py     # SEBI/provider identity matcher & granular column drift detector
├── learner.py           # PII-redacting one-shot LLM template learner with schema & self-check gates
├── parser.py            # Template-driven deterministic parser (ruled_table, monospace, ledger, xlsx, csv)
├── validator.py         # 14-point fail-closed financial auditor
├── consolidator.py      # Multi-custodian portfolio consolidator (ISIN join + currency segregation)
├── persistence.py       # SQLite store syncing hashes, revisions, and ExtractionResults across restarts
├── watch_mailbox.py     # Resilient IMAP poller with per-email try/except, reconnect, and EML processor
├── pipeline.py          # End-to-end orchestration pipeline
├── app.py               # Interactive Streamlit dashboard backed by SQLite persistence
├── send_test_email.py   # Utility to dispatch test statement emails via SMTP to the mailbox
├── requirements.txt     # All third-party Python dependencies
├── manifest.csv          # Test inventory: each sample file and its expected result
├── docs/                 # Design, limitations, and AI-usage documentation
├── tests/                # Benchmark and edge-case test suites
├── templates/            # Empty runtime store; learned templates are saved here
├── test_fixtures/        # Test-only template fixtures
└── sample_data/          # Statements and email fixtures used by the tests
```

---

## Cold-Start Learning

The production `templates/` directory deliberately contains no JSON files. The first
statement of every format invokes the LLM learner; only a schema-valid, self-checked
template is saved to `templates/`. The nine anonymized templates below live under
`test_fixtures/templates/` solely to make deterministic parser regression tests
repeatable; runtime code never loads that directory:
1. `aurum_pms_monthly.json` (Aurum Capital PMS)
2. `aurum_pms_monthly_v2.json` (Aurum Capital PMS layout v2)
3. `banyan_pms_quarterly.json` (Banyan Tree Advisors PMS)
4. `cedar_pms_textflow.json` (Cedar Capital PMS)
5. `delta_pms_ledger.json` (Delta Ridge Securities PMS)
6. `evergreen_aif_cat2.json` (Evergreen Alternatives AIF Cat II)
7. `fulcrum_aif_cat3.json` (Fulcrum Asset Management AIF Cat III)
8. `hestia_pms_csv.json` (Hestia Capital Partners CSV)
9. `hestia_pms_excel.json` (Hestia Capital Partners Excel)

---

### LLM Configuration & Error Handling
- **Provider & Model:** Gemini is used when `GEMINI_API_KEY` is configured; OpenAI is used when only `OPENAI_API_KEY` is configured. A failed request does not trigger another provider. Model is configurable via `LLM_MODEL` (defaults: `gemini-2.5-flash` or `gpt-4o-mini`).
- **API Key Security:** Key is transmitted strictly via headers (`x-goog-api-key`), never in the URL. Keys are scrubbed from all application logs and exception messages.
- **Missing Key / Failure Behavior:** If no key is configured or the API is unreachable, the system fails closed: routes the document to `needs_review` with `learning_failed_llm_unavailable`, writes 0 templates, and appends a redacted entry to `logs/llm_learning.log`.
- **Strict Call Limit:** A new format gets one LLM request. If its response fails schema, PII, or financial self-check validation, the document goes to `needs_review`; the system does not retry. Later documents in an already learned format use the stored template and make zero LLM requests.

---

## Quickstart & Verification

### 1. Install Dependencies
```bash
pip install -r requirements.txt
```

### 2. Run the Blind Benchmark Suite (31 Tests)
```bash
python tests/test_runner.py
```
*Results:* 31/31 passed, 1,046 ground-truth fields reconciled across all statements. Documents with tampered numbers, broken rollforwards, missing continuation pages, or invalid ISINs are quarantined with verified failure codes.

### 3. Run the Unit, Regression & Safety Audit Suite (33 Tests)
```bash
python tests/test_edge_cases.py
```

### 4. Run the Mailbox Poller & Review Queue Handler
*Note:* The IMAP mailbox polling logic (`watch_mailbox.py`) was tested with comprehensive mocks in unit test suites unless explicitly configured with live credentials.

# 1. Dispatch a test statement email via SMTP to the mailbox:
python send_test_email.py --file sample_data/pms/01_aurum_A1_standard.pdf

# 2. Poll the live IMAP inbox once to ingest and audit the newly arrived email:
python watch_mailbox.py --once

# 3. Process a local .eml file offline against persistent SQLite state:
python watch_mailbox.py --test-eml sample_data/emails/e01_statement_single_attachment.eml

# 4. Re-process quarantined statements in needs_review with candidate password(s):
python watch_mailbox.py --reprocess-review --password SecretPass123
```

### 5. Launch the Streamlit Dashboard
```bash
streamlit run app.py
```
*Dashboard features:*
- **📤 Live Email Dispatch & Ingest:** Upload or select any statement to send it as an email to your mailbox and trigger automated ingestion.
- **⚙️ Seed Fixtures / Cold Start:** One-click buttons in the sidebar to populate `templates/` with the 9 verified fixture templates (for instant zero-LLM testing) or clear `templates/` to demonstrate cold-start LLM learning.


---

## Environment Variables & Configuration

| Variable | Default | Purpose |
|---|---|---|
| `GEMINI_API_KEY` | *(None)* | API key for Gemini one-shot template learner. |
| `OPENAI_API_KEY` | *(None)* | API key for OpenAI one-shot template learner when Gemini is not configured. |
| `LLM_MODEL` | `gemini-2.5-flash` / `gpt-4o-mini` | Model identifier for template synthesis. |
| `STATEMENT_PASSWORDS` | *(None)* | Comma-separated candidate passwords for encrypted PDFs. |
| `IMAP_HOST` | *(None)* | IMAP server hostname for mailbox polling. |
| `IMAP_USER` / `IMAP_PASSWORD` | *(None)* | Mailbox username and password/app password. |
| `IMAP_PORT` / `IMAP_FOLDER` | `993` / `INBOX` | Optional IMAP port and folder. |


# Activv Money — AI Usage & Steering Reflection Note

---

## 1. Overview & AI Collaboration Methodology

In developing this solution, AI coding assistants (Claude and Antigravity) were used as pair-programming assistants, adhering to the assignment's guidance:
> *"Treat it as a capable but junior colleague: it writes code quickly, but it does not know our domain, and it will confidently produce things that look right and are not. Keep your AI conversation history and submit it... We want to see how you steered it."*

This document records where AI tools accelerated boilerplate generation, where independent audits exposed shortcuts or facade implementations, and how we steered the implementation to replace every shortcut with real, verifiable engineering.

---

## 2. Where the AI Provided Genuine Acceleration

1. **Pydantic V2 Schemas (`models.py`):**
   - Defining `StatementRecord`, `StatementMeta`, `Summary`, `Holding`, `Transaction`, `ExtractionResult`, and the `FormatTemplate` schema (`TemplateFingerprint`, `TemplateFields`, `TemplatePeriod`, `TemplateSummary`, `TemplateHoldings`, `TemplateTransactions`).
2. **Core Financial Utilities (`utils.py`):**
   - Implementing ISO 6166 Modulo-10 Luhn check-digit verification (`is_valid_isin`), multi-format date normalization (`parse_date`), and Indian accounting number parsing (`clean_number` for Lakhs/Crores, `Dr`/`Cr`, parentheses, and Unicode minus).
3. **Interactive Streamlit Dashboard (`app.py`):**
   - Building the multi-tab UI for inspecting statements, reviewing quarantined files, simulating email ingestion, and viewing consolidated client portfolios.

---

## 3. Critical AI Failure Modes Caught & How We Steered the Fixes

Across two rounds of independent code and mutation audits, we identified and corrected several classic AI failure modes where the assistant initially optimized for passing the 31 benchmark files rather than building a generalizable system:

### Steering 1: Replacing a Stubbed "Learner" Facade with a Real Template-Driven Learner
- **What went wrong initially:** The AI originally wrote a `learner.py` whose LLM prompt contained only a file path and an almost-empty dict (no document text and no `FormatTemplate` schema), backed by a helper `_generate_structural_template` that was only called by the mock test. Meanwhile, the Cedar (`monospace_text`) and Delta (`ruled_lines_horizontal_only`) branches in `parser.py` had hardcoded section headers (`"PORTFOLIO SUMMARY"`, `"HOLDINGS AS ON"`, `"ANNEXURE A - HOLDINGS"`) and hardcoded regexes, ignoring the template's `summary.mapping`, `holdings.regex`, and `transactions.regex`.
- **How we steered it:**
  1. Defined a strict `FormatTemplate` Pydantic schema in `models.py` covering all 5 layout families.
  2. Deleted `_generate_structural_template` completely.
  3. Built `extract_and_redact_document_text()` in `learner.py` to extract text from PDF/XLSX/CSV and redact PANs, client names, emails, phone numbers, and currency amounts before prompting.
  4. Built `build_learning_prompt()` to pass the redacted text, `FormatTemplate.model_json_schema()`, and a complete worked example template.
  5. Refactored all 5 parser branches in `parser.py` so every label, section header, end marker, column order, regex, and `type_map` is read from the template.
  6. Verified with a brand-new synthetic unseen provider (`Meridian Wealth PMS` in `sample_data/unseen/27_meridian_unseen_M1.pdf` and `28_meridian_unseen_M2.pdf`) using completely different labels (`Investor Code`, `Portfolio worth at start`, `Scrip`, `Units`, `Rate`, `Worth`, `ACQUIRE`, `DISPOSE`).

### Steering 2: Blocking Silent Cross-Provider Misattribution
- **What went wrong initially:** `fingerprinter.py` had a fallback that matched any template with header similarity $\ge 0.75$ even when `provider_key` did not match, and `parser.py` overwrote the document's provider name with `template["fields"]["provider"]`. A PDF from a brand-new provider (`"Zenith Wealth PMS"`, SEBI `INP000077777`) using standard column names was silently parsed and validated as `"Aurum Capital PMS"`.
- **How we steered it:**
  1. In `fingerprinter.py`, we added explicit conflict checks: if the document's SEBI registration number or provider name conflicts with the template's, `match_template` immediately skips that template and never falls back to header-only matching across different providers.
  2. In `parser.py`, `statement.provider` is always extracted from the document text first (with the template's `fields.provider` used only as a last-resort fallback).
  3. In `validator.py`, we added `provider_mismatch_template` so any discrepancy between the extracted provider and the template provider fails closed to `needs_review`.

### Steering 3: Making Duplicate & Revision Detection Persist Across Restarts
- **What went wrong initially:** `persistence.py` had a `sha256_hash` column in SQLite, but `watch_mailbox.py` never passed the hash when saving, and `ProcessingPipeline` started with empty `seen_hashes` and `seen_statements` dicts on every run. Running the poller twice on the same `.eml` ingested the statement twice as `validated`.
- **How we steered it:**
  1. Added `sha256_hash` to `ExtractionResult` in `models.py` and populated it on every return path in `pipeline.py`.
  2. Implemented `StatementStore.sync_to_pipeline()` in `persistence.py` to load all persisted `sha256_hash` values and `(provider, client_id, period_end)` keys from SQLite on pipeline startup and before each `.eml` run.

### Steering 4: Converting Binary Classification into a Three-Way Fail-Closed Classifier
- **What went wrong initially:** `IngestionEngine.classify_document()` returned only `"statement"` or `"not_a_statement"`. Any statement that wrote `"Reporting window: 01/09/2026 to 30/09/2026"` without the literal word `"Period"` was silently dropped as `not_a_statement`.
- **How we steered it:**
  1. Broadened period detection to recognize date-range patterns (`dd/mm/yyyy to dd/mm/yyyy`, `yyyy-mm-dd to yyyy-mm-dd`) and keywords (`reporting`, `window`, `valuation date`, `as on`).
  2. Added a hard safety rule: never classify a document as `not_a_statement` if it contains valid ISINs and a holdings/valuation table.
  3. Changed `classify_document` to return three states: `"statement"`, `"not_a_statement"` (only on explicit negative markers like factsheets/invoices/contract notes), or `"ambiguous"` (which routes to `needs_review` with `classification_uncertain`).

### Steering 5: Eliminating Circular Cash Derivation & Catching Renamed Columns
- **What went wrong initially:**
  1. In `parser.py`, Fulcrum's cash row was read and then overwritten with `summary.cash = closing - sum(holdings)`, making the NAV audit a tautology.
  2. In `_extract_table_holdings`, if a known provider renamed `"Market Value (Rs.)"` to `"Mkt Val (Rs.)"`, the fallback `elif "val" in c_name` silently caught it and marked the statement `validated`.
- **How we steered it:**
  1. Removed the circular cash overwrite and added `cash_summary_vs_row_mismatch` in `validator.py` to cross-check summary-table cash against holdings-row cash.
  2. Restricted fallback column synonyms in `_extract_table_holdings` so they only run when a template has no explicit `holdings.mapping`, and recorded any missing mapped column as `expected_column_missing:<column_name>` alongside `layout_drift_known_provider`.

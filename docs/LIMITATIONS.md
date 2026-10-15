# Activv Money — Statement Parser Limitations & Operating Boundaries

This document provides a candid, specific disclosure of the operating boundaries, known edge-case failure modes, issue codes, and human-in-the-loop protocols for the PMS/AIF Statement Parser.

---

## 1. Supported Layout Families (`layout_kind`)

The deterministic parser (`parser.py`) and `FormatTemplate` schema (`models.py`) support five concrete layout families:
1. **`ruled_table`**: PDFs containing grid-ruled or whitespace-aligned tables extractable via `pdfplumber.extract_tables()`.
2. **`monospace_text`**: Fixed-width plaintext statements where sections (`PORTFOLIO SUMMARY`, `HOLDINGS AS ON`, `TRANSACTIONS`) and line rows are matched via template regexes and label mappings.
3. **`ruled_lines_horizontal_only`**: Horizontal-rule ledger statements with ordered summary columns, holdings rows, and debit/credit narration entries.
4. **`xlsx`**: Multi-sheet Excel workbooks (`Summary`, `Holdings`, `Transactions`) parsed via `openpyxl`.
5. **`csv_sectioned`**: Single-file sectioned CSV exports with explicit block markers (`SUMMARY`, `HOLDINGS`, `TRANSACTIONS`).

### Boundary
If an asset manager issues an exotic, non-standard layout outside these five layout families (for example, multi-column newspaper layouts, Infographic/chart-only PDFs, or nested pivot tables), the generated template will fail the deterministic self-check (`learning_failed_self_check`) and require manual parser extension or human template authoring.

---

## 2. Scanned Image-Only Documents (No Embedded OCR)

### Current Behavior
The system identifies scanned or image-based PDFs (such as `sample_data/edge_cases/18_scanned_aurum_A1.pdf`) by inspecting the character count of the PDF text layer after decryption. If a PDF yields fewer than 30 characters across all pages, `ingestion.py` routes the file to `needs_review` with:
```json
"issues": ["no_text_layer_requires_ocr"]
```

### Boundary
No OCR engine (Tesseract, PaddleOCR, or cloud OCR) is bundled inside the pipeline. Image-only PDFs are quarantined for external OCR preprocessing.

---

## 3. Password-Protected & Encrypted PDFs

### Current Behavior
Encrypted PDFs are detected during ingestion via `pypdf.PdfReader.is_encrypted`:
- **Explicit Passwords Only:** The engine attempts decryption using candidate passwords supplied explicitly to `process_file(..., password=...)`, via the review queue, or via the comma-separated `STATEMENT_PASSWORDS` environment variable.
- **No Pattern Guessing:** The system does **not** derive or guess passwords from PAN, date of birth (DOB), client name, or account patterns (since those fields reside inside the encrypted PDF payload itself).
- **Crypto Dependency Boundary:** Decrypting AES-128 and AES-256 encrypted PDFs requires the `cryptography` package. If decryption fails due to a missing cryptographic dependency, the file is quarantined with issue `pdf_crypto_dependency_missing` rather than being misclassified as an OCR/no-text-layer issue.
- **Safe Temp Lifecycle:** Upon successful decryption, `ingestion.py` writes a temporary decrypted PDF, hashes and parses the decrypted file, and deletes the temporary file in a `finally` block.
- If no password is supplied or all candidate passwords fail, the file fails closed to `needs_review` with:
```json
"issues": ["password_required"]
```

---

## 4. Date Ambiguity & Parsing Conventions

### Operating Boundary
- **Day-First Default:** Dates across transactions and reporting periods are parsed day-first by default (`DD-MM-YYYY`, `DD/MM/YYYY`, `DD-Mon-YYYY`, `DD.MM.YYYY`), which is the standard regulatory convention for Indian PMS/AIF managers.
- **Ambiguous Formats:** For ambiguous numeric dates where both day and month $\le 12$ (e.g. `04/05/2026`), the parser assumes day-first unless the format template provides an explicit `period.format` specification (e.g. `MM/DD/YYYY`). Statements strictly following US month-first conventions require a template-specific `period.format` rule.

---

## 5. Three-Way Pre-Classification Heuristics

### Current Behavior
`IngestionEngine.classify_document()` classifies incoming PDFs into three outcomes:
1. **`not_a_statement`**: Explicit negative markers in the document header (`MONTHLY FUND FACTSHEET`, `TAX INVOICE`, `CONTRACT NOTE`, `FORM 16A`) when no client portfolio holdings table is present.
2. **`statement`**: Documents containing either (a) all three structural pillars (client identifier + reporting period/date range + portfolio/holdings markers) or (b) valid ISINs alongside a holdings/valuation table.
3. **`ambiguous`**: Documents that match neither rule (e.g., generic prose letters or partial statements missing standard labels) fail closed to `needs_review` with:
```json
"issues": ["classification_uncertain"]
```

---

## 6. One-Shot Learning & Self-Check Failure Modes

### Cold Start vs. Live LLM Learning
- **Empty Runtime Store:** `templates/` begins empty. The nine anonymized parser fixtures reside only in `test_fixtures/templates/`; the runtime pipeline does not read them.
- **One-Shot Learning Path:** For a first-seen provider or layout, `FormatLearner` (`learner.py`) redacts PII from the document text, constructs a prompt containing the `FormatTemplate` JSON Schema and a worked example, and makes at most one LLM request. A response that fails schema, PII, or financial self-check validation is discarded and routed to `needs_review`; the system does not retry or call a second provider.
- **Durable Failure Persistence:** Failed learning attempts are persisted in SQLite by exact format fingerprint. The same malformed layout is not repeatedly retried, but a materially different layout from the provider can still be learned.
- **Model Configuration:** Configurable via the `LLM_MODEL` environment variable (defaults to `gemini-2.5-flash` when using `GEMINI_API_KEY`, or `gpt-4o-mini` when using `OPENAI_API_KEY`).
- **Mock Testing:** In CI and local test suites without live API keys, the live synthesis and self-check loop is verified using mock LLM responses on unseen synthetic providers (`meridian_wealth_pms`) and mutated row-label variants.

Three distinct failure codes govern quarantine:
- **`learning_failed_llm_unavailable`**: No API key (`GEMINI_API_KEY` / `OPENAI_API_KEY`) is configured, network request failed, or retry attempts were exhausted. Zero templates are written.
- **`learning_failed_invalid_template`**: The LLM returned malformed JSON, violated the `FormatTemplate` Pydantic schema, or leaked PII (PANs, client names, emails, phone numbers, ISINs) into the template. Zero templates are written.
- **`learning_failed_self_check`**: The LLM returned a syntactically valid `FormatTemplate`, but when `StatementParser` executed it on the sample document and `FinancialValidator` audited the output, critical validation issues remained (e.g., `missing_required_metadata`, `empty_statement_zero_balances`, `summary_rollforward_mismatch`, `closing_value_ne_holdings_total`). The candidate template is discarded and never saved to `templates/`.

---

## 7. Layout Drift & Provider Identity Guards

- **Cross-Provider Guard:** `FormatFingerprinter` (`fingerprinter.py`) never matches a template if the document's SEBI registration number or provider name conflicts with the template's (`provider_mismatch_template`).
- **Header Noise & Banner Isolation:** Top-of-page noise lines (such as `Generated 12-Oct-2026 | CONFIDENTIAL` or page headers) are filtered out via `find_provider_in_text()` so that corporate identity matching relies on the SEBI registration line and company title keywords (`Ltd`, `PMS`, `Capital`, `Wealth`, etc.).
- **Multiple Products & Best-Match Ranking:** When a provider offers multiple products sharing the same layout kind, `fingerprinter.py` computes header/fingerprint similarity across all candidate templates and ranks by highest score, selecting the best product match without false drift flags.
- **Column & Header Drift:** If a known provider renames or drops a mapped column (e.g., renaming `"Market Value (Rs.)"` to `"Mkt Val (Rs.)"`) or alters its table headers below 0.75 similarity, the statement is quarantined to `needs_review` with `layout_drift_known_provider` and `expected_column_missing:<column_name>`.
- **Approval & Promotion:** Drifted templates (`_v2`) carry `approved: false` until reviewed. Once promoted via `pipeline.promote_template()`, subsequent matching statements parse as known formats with `status=validated` and 0 LLM calls.

---

## 8. Financial Auditor Tolerances

The financial auditor (`validator.py`) enforces the following explicit tolerances:
- **Monetary Sums & Roundings (`MONEY_TOLERANCE`):** `₹1.00` (1 INR) tolerance for roll-forward equations, fee reconciliation, and cash summary comparisons.
- **Per-Row Price Verification (`QTY_PRICE_TOLERANCE_PCT`):** `0.5%` (`0.005`) relative tolerance for `abs(quantity) * price == market_value` (minimum `₹1.00`).
- **Holding Weights Tolerances (`TOTAL_WEIGHT_TOLERANCE = 0.50`, `HOLDING_WEIGHT_TOLERANCE = 0.10`):** Total portfolio weights (including printed cash weight) must sum to 100 within +/-0.5, and each individual holding weight must match its calculated proportion (market_value / total) * 100 within +/-0.1.
- **AIF Units x NAV per Unit (`NAV_PER_UNIT_TOLERANCE`):** `₹1.00` (1 INR) tolerance for `units * nav_per_unit == closing`.

---

## 9. Complete Issue Code Reference

| Issue Code | Meaning |
|---|---|
| `no_text_layer_requires_ocr` | PDF has no extractable text layer (<30 chars). |
| `password_required` | Encrypted PDF could not be unlocked with `STATEMENT_PASSWORDS`. |
| `pdf_crypto_dependency_missing` | Encrypted PDF decryption failed due to missing cryptographic library (`cryptography`). |
| `classification_uncertain` | Document is neither a clear statement nor a known negative document type. |
| `zip_attachment_requires_manual_review` | Email contained a `.zip` attachment requiring manual operator inspection. |
| `unsupported_statement_attachment_type` | Email contained an unsupported statement file format (e.g. `.xls`, `.xlsm`). |
| `inline_pdf_missing_filename` | Email contained an inline PDF without an explicit filename attachment header. |
| `ambiguous_attachment_requires_review` | Email contained an unrecognized non-standard attachment requiring review. |
| `identical_to:<path>` | Hard duplicate (identical SHA-256 content hash). |
| `same_content_different_bytes` | Duplicate statement with identical data and file type but re-saved byte differences. |
| `revision_of_existing_statement` | Same `(provider, client_id, period_end)` with a different content/financial hash. |
| `layout_drift_known_provider` | Known provider altered table structure or renamed mapped columns. |
| `expected_column_missing:<col>` | A column required by `template.holdings.mapping` is missing from the table. |
| `provider_mismatch_template` | Extracted document provider disagrees with the matched template's provider. |
| `learning_failed_llm_unavailable` | Unseen format encountered with no LLM API key or API error. |
| `learning_failed_invalid_template` | LLM output failed `FormatTemplate` schema validation or PII leak check. |
| `learning_failed_self_check` | Candidate template failed deterministic parser/validator self-check on sample. |
| `incomplete_document_missing_continuation_page` | Page footer (`Page X of Y` or `Continued on page...`) indicates missing pages. |
| `missing_required_metadata` | Missing `client_id` or `period_end`. |
| `empty_statement_zero_balances` | Zero closing balance with zero holdings. |
| `holdings_sum_ne_stated_total_equity` | Sum of holdings market values != stated Total Equity row. |
| `closing_value_ne_holdings_total` | Holdings sum + cash != Closing balance/NAV. |
| `cash_summary_vs_row_mismatch` | Cash in summary table disagrees with cash row in holdings table. |
| `summary_rollforward_mismatch` | Opening + Inflow - Outflow + Gain - Fee != Closing. |
| `invalid_isin_checksum` | Security ISIN failed ISO 6166 Mod-10 Luhn check digit verification. |
| `duplicate_holding_isin` | Same ISIN appears multiple times in the same side (`long`/`short`) of holdings. |
| `negative_quantity_in_long_only_template` | Holding quantity is negative in a long-only template (`supports_short: false`). |
| `invalid_period_range` | Statement `period_end` strictly precedes `period_start`. |
| `holding_qty_price_mismatch` | Holding `quantity * price != market_value` beyond 0.5% tolerance. |
| `holdings_weight_sum_invalid` | Sum of holding `weight_pct` values deviates from 100% beyond 0.5% (or 3%) tolerance. |
| `unmapped_transaction_type` | Transaction mapped to `"other"` because its label is not in `type_map`. |
| `transaction_date_outside_period` | Transaction date falls outside `[period_start, period_end]`. |
| `units_times_nav_per_unit_mismatch` | `units * nav_per_unit != closing` beyond ₹1.00 tolerance. |
| `template_missing_row_labels` | Non-blocking notice: template lacked explicit row labels, falling back to defaults. |

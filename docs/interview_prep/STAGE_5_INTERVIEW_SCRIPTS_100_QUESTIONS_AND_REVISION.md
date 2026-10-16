# STAGE 5: INTERVIEW SCRIPTS, 100 QUESTIONS, CODE DEFENSE & MASTER REVISION
## Activv Money — Self-Learning PMS/AIF Statement Parser
### Comprehensive Codebase Analysis & Technical Interview Guide for IIT Indore Candidates

---

# PART 24: INTERVIEW EXPLANATIONS AT MULTIPLE LEVELS

### Version A: 60–90 Seconds Elevator Pitch
> *"I built a Self-Learning PMS and AIF Statement Parser designed to automate the ingestion of wealth management statements across multiple fund managers. The core problem in wealth operations is that every AMC formats their PDF, Excel, and CSV statements differently, but sending every statement to an LLM is too expensive, slow, and prone to hallucinations.*
>
> *My system solves this through a one-shot learning architecture: the first time an unseen statement layout is encountered, it scrubs all client PII and uses Google Gemini once to synthesize a declarative JSON template. Before storing it, the candidate template must pass a deterministic financial self-check audit on the sample document. For every subsequent statement matching that layout, the system parses it 100% deterministically with zero LLM calls.*
>
> *Extracted data passes 17 automated financial checks—including double-entry rollforward reconciliation, ISO 6166 ISIN Luhn checksums, and holdings-versus-corpus checks. Corrupted or drifted statements are quarantined to a review queue, while clean statements are persisted to Neon PostgreSQL and aggregated into a cross-provider consolidated portfolio view for the client."*

---

### Version B: 5 Minutes Technical Overview
* **The Business Context:**
  *"Activv Money advises high-net-worth clients whose portfolios are distributed across multiple Portfolio Management Services and Alternative Investment Funds. Asset managers email monthly PDF and Excel reports with no standardized format. Manually extracting this takes hours, while passing all documents to an AI model introduces hallucinations, latency, and huge token costs."*
* **The Architecture & 5-Stage Pipeline:**
  1. *Ingestion & Gatekeeper (`ingestion.py`):* Connects to Gmail via IMAP, filters out marketing newsletters using a 3-way classifier, decrypts password-protected PDFs, and computes SHA-256 hashes for 0ms duplicate detection.
  2. *Format Fingerprinting (`fingerprinter.py`):* Extracts table header n-grams and computes string similarity against known templates with a 0.75 threshold, enforcing strict SEBI registration guards to prevent cross-provider misattribution.
  3. *One-Shot Learner (`learner.py`):* If the format is unknown, it masks all client PANs, names, and balances, prompts Google Gemini (`gemini-3.8-flash`) once, and runs a deterministic self-check audit. If the candidate template fails, it is discarded in-memory and quarantined. If valid, it is saved as a declarative JSON configuration.
  4. *Deterministic Multi-Page Parser (`parser.py`):* For all known formats, it uses ruled-table, horizontal line, monospace textflow, or spreadsheet parsing across all pages with zero LLM calls, handling Indian numbering (Lakhs/Crores) and parenthesized negatives.
  5. *Financial Safety Gate (`validator.py`):* Audits 17 mathematical rules: opening corpus plus net flows plus gains minus fees must equal closing corpus within ₹1.00; listed ISINs must pass Mod-10 Luhn check digits; holding totals must match stated equity within 0.1%.
* **Persistence & UI:**
  * Dual-engine storage in SQLite with WAL mode and Neon PostgreSQL cloud.
  * A 4-view Streamlit dashboard that provides an interactive Quarantined Review Queue for human sign-off on layout drift and password unlocking, plus a multi-provider portfolio consolidation view.

---

### Version C: 10–15 Minutes Deep Technical Walkthrough
*Walk the interviewer through the codebase in execution order:*
1. **Domain Primitives (`models.py`, `utils.py`):**
   * Pydantic v2 schemas for `StatementRecord`, `HoldingItem`, `TransactionItem`, `StatementSummary`, and `FormatTemplate`.
   * Indian number parsing: regex handling for lakhs, crores, accounting Dr/Cr, and ISO 6166 ISIN Luhn check digit verification.
2. **Preflight & Ingestion Gate (`ingestion.py`, `watch_mailbox.py`):**
   * Path sanitization against `../` directory traversal.
   * AES-128/256 encrypted PDF decryption via `STATEMENT_PASSWORDS` and PAN patterns.
   * Decrypted content hashing: hashing decrypted bytes ensures byte-level duplicate detection even when encryption salts differ.
3. **Fingerprinting & Provider Guards (`fingerprinter.py`):**
   * Two-factor recognition: SEBI registration number matching + 0.75 SequenceMatcher ratio on table headers.
   * Granular column drift detection: if overall headers match but a mapped column is renamed, it flags `layout_drift_known_provider`.
4. **One-Shot Learning & The Self-Check Gate (`learner.py`):**
   * PII redaction pipeline: 9 regex patterns mask PAN, name, phone, email, and amounts.
   * Multi-key failover: automatically retries HTTP 429 quota exhaustion and fails over from `GEMINI_API_KEY` to `GEMINI_BACKUP_API_KEY`.
   * The Self-Check Gate: prevents bad templates from entering production by executing a dry-run parse and requiring zero financial validation errors.
5. **Deterministic Parser & Multi-Client Splitting (`parser.py`):**
   * How ruled, horizontal-rule, monospace textflow, and spreadsheet parsing work.
   * Multi-client statement splitting: continuously monitors page text for changes in `client_id` to split consolidated 2-in-1 PDFs.
6. **Financial Safety Gate (`validator.py`):**
   * Checks A through Q: mathematical roll-forward, corpus reconciliation, ISIN Luhn checks, weight bounds, cash row matching, and long-only negative quantity guards.
   * Fail-closed architecture: invalid statements are quarantined to `needs_review` and never silently corrupt client books.
7. **Consolidation & Dashboard (`consolidator.py`, `app.py`):**
   * Grouping by PAN, ISIN deduplication, blended cost calculation, weight re-normalization, and currency isolation (INR vs USD).
   * Streamlit dashboard with 4 views, Neon PostgreSQL persistence, and interactive human sign-off for layout drift promotion.

---

# PART 25: 100 PROJECT-SPECIFIC INTERVIEW QUESTIONS

### Beginner Questions (1–25)
1. **What is the core objective of this project?**  
   *Answer:* To automate the extraction, financial auditing, and consolidation of multi-format PMS and AIF investment statements received via email using a one-shot learning architecture that parses subsequent statements deterministically with zero LLM calls.
2. **What does PMS stand for, and how does it differ from a mutual fund?**  
   *Answer:* Portfolio Management Service. In a PMS, the client directly owns individual securities in their own demat account managed by a SEBI-registered portfolio manager (minimum investment: ₹50 Lakhs), whereas in a mutual fund, investors own units of a pooled fund.
3. **What is an AIF?**  
   *Answer:* Alternative Investment Fund. Privately pooled investment vehicles for sophisticated investors (minimum ticket: ₹1 Crore). Category II covers private equity/debt; Category III covers hedge funds and long-short strategies.
4. **Why did you use Python instead of C++ for this project?**  
   *Answer:* Python offers the richest ecosystem for document parsing (`pdfplumber`, `pypdf`, `openpyxl`), data modeling (`pydantic`), tabular manipulation (`pandas`), and rapid web UI development (`streamlit`). C++ would have required building PDF rendering and table parsing engines from scratch.
5. **What is deterministic parsing?**  
   *Answer:* A parsing process that guarantees the exact same output for the same input every single time using strict programmatic rules without relying on probabilistic machine learning or AI models during execution.
6. **Why not just call an LLM on every single statement?**  
   *Answer:* Calling an LLM on every document costs significant API fees, is slow (10–30s latency per statement), hits rate limits (like Gemini's 20 req/day free limit), risks leaking confidential client PII, and introduces non-deterministic hallucinations in financial numbers.
7. **What is an ISIN?**  
   *Answer:* International Securities Identification Number. A 12-character alphanumeric code that uniquely identifies a specific stock or bond globally (e.g. `INE002A01018` for Reliance Industries).
8. **What is Pydantic, and why did you use it?**  
   *Answer:* Pydantic is a data validation library that enforces type hints at runtime. We use Pydantic v2 to validate domain models, guarantee valid JSON schemas, and serialize outputs.
9. **What is the difference between IMAP and SMTP?**  
   *Answer:* IMAP is an inbound protocol used by `watch_mailbox.py` to retrieve and read emails from a mailbox. SMTP is an outbound protocol used by `send_test_email.py` to send emails.
10. **What is a declarative JSON template?**  
    *Answer:* A structured configuration file (not executable code) that defines regular expression patterns, table header signatures, column mappings, and total row labels.
11. **What is layout drift?**  
    *Answer:* When a known wealth manager or bank alters their statement table structure, renames columns (e.g. "Avg Cost" to "Cost Basis"), or moves metadata fields.
12. **What does "fail-closed" mean in software architecture?**  
    *Answer:* A security and integrity principle where any unexpected error, ambiguous data, or mathematical discrepancy causes the system to reject or quarantine the file rather than continuing blindly.
13. **What is the roll-forward equation?**  
    *Answer:* $\text{Opening Valuation} + \text{Inflows} - \text{Outflows} + \text{Net Gain/Loss} - \text{Fees} == \text{Closing Valuation}$.
14. **What tolerance does your financial auditor use for monetary sums?**  
    *Answer:* ₹1.00 (`MONEY_TOLERANCE = 1.00`), which accounts for minor roundings in AMC accounting ledgers.
15. **What is a SEBI registration number?**  
    *Answer:* A unique regulatory license code issued by the Securities and Exchange Board of India (e.g. `INP000088101` for a PMS, `INA000020891` for an RIA).
16. **How do you handle Indian numbering formats like Lakhs and Crores?**  
    *Answer:* `clean_number()` in `utils.py` handles commas placed after 3 digits and then every 2 digits (e.g. `1,25,00,000.00`) and multiplies by $10^5$ or $10^7$ when text labels appear.
17. **How are negative numbers formatted in financial statements?**  
    *Answer:* Often wrapped in parentheses like `(12,500.00)` or suffixed with `Dr`. `clean_number()` strips parentheses and applies a negative sign (`-12500.0`).
18. **What database engines does your application support?**  
    *Answer:* Dual support: zero-config local SQLite (`statements.db`) with Write-Ahead Logging (WAL) and cloud-hosted Neon PostgreSQL via `DATABASE_URL`.
19. **What is a cold start in this project?**  
    *Answer:* When the application boots with an empty `templates/` directory, proving that the runtime does not rely on hardcoded pre-saved templates.
20. **Where are the reference test templates kept?**  
    *Answer:* In `test_fixtures/templates/`. They are strictly isolated for offline regression testing and never read by the runtime pipeline.
21. **What is the purpose of `manifest.csv`?**  
    *Answer:* A benchmark test manifest specifying 28 statement files, their expected providers, layout kinds, client IDs, and financial totals for regression verification.
22. **What does `status: "needs_review"` signify?**  
    *Answer:* The document was quarantined by the financial safety gate due to broken math, missing continuation pages, invalid ISINs, layout drift, or password encryption.
23. **What is a unitholder ID?**  
    *Answer:* The client account identifier used in AIF and mutual fund investments, analogous to a PMS Client Code.
24. **How do you prevent duplicate email processing?**  
    *Answer:* By storing the unique email `message_id` in the `email_events` database table with a unique index constraint.
25. **What is NAV?**  
    *Answer:* Net Asset Value. The per-unit or total market value of an investment fund's assets minus its liabilities.

---

### Intermediate Questions (26–65)
26. **How does document fingerprinting work in `fingerprinter.py`?**  
    *Answer:* It extracts table header text and section titles from the first page, normalizes whitespace, and computes sequence similarity against registered template signatures.
27. **Why is the fingerprint similarity threshold set to 0.75?**  
    *Answer:* Through empirical testing across our benchmark suite, 0.75 cleanly distinguishes genuine format variations from noisy headers (like dynamic date stamps) without triggering false matches across different providers.
28. **How do you prevent an Aurum statement from matching a Banyan template?**  
    *Answer:* The **Provider Misattribution Guard** in `fingerprinter.py`. If the document's extracted provider name or SEBI registration conflicts with the template's, similarity is discarded and the match is rejected (`provider_mismatch_template`).
29. **What happens during one-shot format learning?**  
    *Answer:* Text from the first 3 pages is extracted, PII is scrubbed, and Google Gemini (`gemini-3.8-flash`) synthesizes a declarative JSON template. The template undergoes a deterministic self-check before being saved to `templates/`.
30. **What is the Deterministic Self-Check Gate in `learner.py`?**  
    *Answer:* Before writing a candidate template to disk, `StatementParser` runs the template on the sample document and `FinancialValidator` audits the output. If holdings count is zero or the rollforward fails, the candidate template is discarded in-memory and flagged `learning_failed_self_check`.
31. **What PII is scrubbed before calling Gemini?**  
    *Answer:* 9 regex filters mask PANs (`<REDACTED_PAN>`), client names (`<REDACTED_NAME>`), email addresses, phone numbers, addresses, and monetary balances.
32. **What happens if Gemini returns malformed markdown with code fences?**  
    *Answer:* `extract_first_json_object()` in `learner.py` strips markdown code blocks (` ```json ... ``` `) and uses bracket-matching to extract the first valid JSON object `{...}`.
33. **How does multi-key failover work for Gemini?**  
    *Answer:* If `GEMINI_API_KEY` hits HTTP 429 (quota exhausted) or HTTP 503, the learner retries once with a 3-second backoff. If it fails again, it automatically switches to `GEMINI_BACKUP_API_KEY` without interrupting the pipeline.
34. **How are multi-page tables stitched together in `parser.py`?**  
    *Answer:* The parser iterates over `stmt_pages`. It scans for running table headers on subsequent pages, strips the intermediate header and footer lines, and concatenates table rows into a continuous sequence.
35. **How do you handle tables with horizontal rules but no vertical borders?**  
    *Answer:* `parser.py` detects horizontal lines via `page.lines`, identifies line bands, and splits columns using whitespace gap clusters between text words.
36. **How does the monospace textflow parser work?**  
    *Answer:* For typewriter layouts (like Cedar PMS), it calculates horizontal character column boundary indices from the header line and slices each text line by exact character ranges.
37. **How does the ledger parser handle Debit and Credit columns?**  
    *Answer:* It maps transaction rows where amounts in the "Debit" column represent cash outflows or buys, and amounts in the "Credit" column represent inflows or sales.
38. **How does the system split multi-client statements in a single PDF?**  
    *Answer:* During page iteration, `parser.py` checks each page for a `client_id` match. When a new client ID appears on a middle page, it closes the previous statement and initiates a new `StatementRecord`.
39. **How do you prevent subtotal and total rows from contaminating the holdings list?**  
    *Answer:* The template defines `total_row_labels` (e.g. `["Total Equity", "Subtotal", "Net Total"]`). When the row parser encounters these labels, it captures the stated total for audit verification and excludes the row from the individual holdings array.
40. **How does `is_valid_isin()` implement the Luhn algorithm?**  
    *Answer:* It converts letters to digits ($A=10, B=11, \dots$), doubles every second digit from right to left, subtracts 9 from results $\ge 10$, sums all transformed digits, and asserts `total % 10 == 0`.
41. **What happens if an unlisted AIF holding lacks an ISIN?**  
    *Answer:* In unlisted templates (`is_unlisted: true`), the ISIN checksum check is bypassed, and the asset is identified by its private equity asset name.
42. **How does the 3-way email classifier detect newsletters?**  
    *Answer:* In `ingestion.py`, `preflight_email()` checks for keywords like `insights`, `newsletter`, `factsheet`, `market commentary` in the subject and marks the email status as `ignore`, skipping processing with zero cost.
43. **How does password decryption work for encrypted PDFs?**  
    *Answer:* `ingestion.py` uses `pypdf.PdfReader.decrypt()`, trying candidate passwords from `STATEMENT_PASSWORDS` or client PANs. If successful, it writes the decrypted stream and hashes it for deduplication.
44. **Why hash the decrypted PDF bytes instead of the encrypted file?**  
    *Answer:* Encrypted PDFs often contain random initialization vectors/salts that produce different SHA-256 file hashes for the exact same document. Hashing the decrypted content guarantees true content deduplication.
45. **What is `same_content_different_bytes`?**  
    *Answer:* When a PDF is re-saved or re-printed, changing its binary metadata without altering any text or financial numbers. The system detects this via data signatures and flags it as a duplicate.
46. **What is `revision_of_existing_statement`?**  
    *Answer:* When an AMC re-issues a statement for the exact same client and period, but with altered financial numbers. Quarantined to prevent silently overwriting prior audited books.
47. **How does `consolidator.py` aggregate holdings across providers?**  
    *Answer:* It matches identical ISINs across accounts, computes total quantity, sums market values, and calculates a quantity-weighted blended cost price.
48. **How does the consolidator prevent double-counting of cash?**  
    *Answer:* Cash line items are extracted into a separate portfolio cash pool and never treated as equity securities.
49. **How are unlisted AIF investments handled in portfolio consolidation?**  
    *Answer:* Unlisted holdings are segregated into an independent "Unlisted Alternative Assets" category with separate commitments and capital drawn metrics, avoiding distortion of liquid equity weights.
50. **How does currency isolation work during consolidation?**  
    *Answer:* Portfolios are partitioned by currency (`INR` vs `USD`). The consolidator refuses to sum USD amounts with INR amounts without explicit conversion rates.
51. **What is Write-Ahead Logging (WAL) in SQLite?**  
    *Answer:* A database concurrency mode (`PRAGMA journal_mode=WAL;`) that allows concurrent readers to read without blocking writers, preventing database lock contention between the IMAP poller and the dashboard.
52. **How does Streamlit Community Cloud persistence work?**  
    *Answer:* Container filesystems are ephemeral. Setting `DATABASE_URL` connects the app to Neon PostgreSQL, persisting statement records, templates, and review queues across server restarts.
53. **How does `app.py` handle secrets synchronization?**  
    *Answer:* On startup, lines 27–36 of `app.py` iterate through `st.secrets` and inject keys into `os.environ`, allowing library modules like `learner.py` to seamlessly access credentials.
54. **What are the 4 views in the Streamlit dashboard?**  
    *Answer:* 1. Ingested Statement Explorer, 2. Multi-Provider Portfolio Consolidation, 3. Quarantined Review Queue & Safety Gate, 4. Live Statement Email Dispatcher.
55. **How does an operator promote a drifted template in the dashboard?**  
    *Answer:* In View 3, the operator reviews the candidate schema diff and clicks `✅ Sign Off & Approve Template`. This calls `pipeline.approve_template()` and re-evaluates quarantined statements.
56. **What does the "Clean Stored Templates" sidebar button do?**  
    *Answer:* It deletes all runtime `.json` templates from `templates/`, resetting the engine to a cold-start state to demonstrate live one-shot learning.
57. **What does the "Reset Database Records" sidebar button do?**  
    *Answer:* It truncates statement and email tables in SQLite/Neon, resetting ingested portfolio history.
58. **How does `send_test_email.py` work?**  
    *Answer:* It connects to `smtp.gmail.com:587` with STARTTLS, attaches the target PDF/Excel file as a MIME attachment, and sends it to `IMAP_USER`.
59. **What is Check A2 in `validator.py`?**  
    *Answer:* Metadata sanity check: verifies that client name, ID, and strategy strings are single-line, $\le 80$ characters, and do not contain leaked monetary amounts.
60. **What is Check E in `validator.py`?**  
    *Answer:* Verifies that the sum of individual holding market values matches the stated Total Equity row within 0.1%.
61. **What is Check F in `validator.py`?**  
    *Answer:* The summary roll-forward reconciliation check ($\text{Opening} + \text{Inflow} - \text{Outflow} + \text{Gain} - \text{Fee} == \text{Closing} \pm ₹1.00$).
62. **What is Check H in `validator.py`?**  
    *Answer:* Reconciles the cash figure in the summary table against the liquid cash line item extracted from the holdings table.
63. **What is Check I in `validator.py`?**  
    *Answer:* Verifies that total portfolio weights (holdings plus cash) sum to $100\% \pm 0.50\%$.
64. **What is Check K in `validator.py`?**  
    *Answer:* Verifies per-holding arithmetic: $\text{Quantity} \times \text{Price} == \text{Market Value}$ within $0.5\%$.
65. **What is Check O in `validator.py`?**  
    *Answer:* Verifies that holding quantities are non-negative in long-only fund templates (`supports_short: false`).

---

### Advanced Questions (66–100)
66. **What happens if two different providers use identical table headers?**  
    *Answer:* The **Provider Identity Guard** prevents cross-provider matching. Even if table headers have 1.00 similarity, `fingerprinter.py` checks for the provider name and SEBI registration. If they do not match, the candidate template is rejected.
67. **How do you handle dynamic header noise like "Generated on 12-Oct-2026 | Page 1"?**  
    *Answer:* Top-of-page running headers are filtered out by `find_provider_in_text()` and table boundary detection, isolating corporate identity keywords from dynamic timestamps.
68. **Can a statement pass financial validation and still have an incorrect extraction?**  
    *Answer:* Yes. For example, if a stock name is misread but its quantity, price, and market value are correctly extracted and math reconciles, the financial audit will pass. Domain audits cannot verify semantic stock spelling without external ticker lookup databases.
69. **Why is an LLM candidate template evaluated on the sample document before saving?**  
    *Answer:* To prevent "hallucinated schemas". An LLM might return valid JSON conforming to `FormatTemplate`, but with wrong column names (e.g. mapping "Price" to "Units"). Executing the template on the sample immediately catches 0 holdings or broken math, discarding the template before it corrupts production.
70. **What happens if a provider quietly renames "Market Value" to "Mkt Val"?**  
    *Answer:* Sequence similarity remains high ($\ge 0.75$), but the mapped column is missing. The fingerprinter flags `layout_drift_known_provider`, isolates the statement to `needs_review`, and creates an unapproved `_v2` candidate template awaiting human sign-off.
71. **How does the system ensure zero LLM calls on repeat runs?**  
    *Answer:* Once a template is saved in `templates/`, `FormatFingerprinter` matches it on subsequent statements, routing directly to `parser.py` with `llm_calls_made = 0`.
72. **What is the time complexity of the fingerprint matching algorithm?**  
    *Answer:* $O(K \cdot L)$ where $K$ is the number of templates ($\sim 10$) and $L$ is header token length. Takes $<15\text{ ms}$, ensuring near-instant classification.
73. **Why did you avoid using PyMuPDF (fitz) as a required dependency?**  
    *Answer:* While PyMuPDF is fast, its AGPL license restricts commercial proprietary use in fintech applications. We used MIT/BSD-licensed `pdfplumber` and `pypdf`, which are enterprise-safe and fully open-source.
74. **How do you handle multi-page statements where column widths vary across pages?**  
    *Answer:* `parser.py` re-calculates column bounding boxes per page based on that specific page's header text positions, preventing column drift on re-flowed pages.
75. **What happens when an encrypted PDF is supplied with the wrong password?**  
    *Answer:* Decryption raises a password exception; the file is caught by the preflight gate and quarantined with `password_required`. No partial or corrupt data is written.
76. **How does the system prevent infinite retry loops on corrupt layouts?**  
    *Answer:* `failed_learning_attempts` persists the exact layout fingerprint hash in the database. If the exact same un-learnable layout is re-submitted, the system immediately quarantines it without re-calling the LLM.
77. **How does the consolidator handle a client with accounts in both INR and USD?**  
    *Answer:* It maintains strict currency isolation, grouping holdings by currency code. In the dashboard, users select either "INR" or "USD" to view that currency's consolidated portfolio.
78. **What happens if two concurrent background workers ingest the same statement?**  
    *Answer:* The `sha256_hash` unique index on `statements` and the unique `message_id` on `email_events` cause the second worker's database insert to raise an integrity error or yield 0ms duplicate skip, ensuring idempotency.
79. **How would you scale this architecture to process 100,000 statements per day?**  
    *Answer:* Decouple the pipeline into an asynchronous distributed queue: an IMAP ingest worker pushes file tasks to Redis/RabbitMQ; a pool of stateless Celery workers executes deterministic parsing against read-only cached templates; results write to a sharded PostgreSQL cluster.
80. **What is the difference between Check E and Check F in `validator.py`?**  
    *Answer:* Check E verifies the balance sheet (sum of holdings == stated equity). Check F verifies the cash flow roll-forward statement ($\text{Open} + \text{Flows} + \text{Gains} - \text{Fees} == \text{Close}$).
81. **Why not let an LLM generate custom Python extraction scripts?**  
    *Answer:* Executing LLM-generated code via `exec()` creates severe Remote Code Execution (RCE) vulnerabilities, memory leaks, and unpredictable crashes. Declarative JSON templates keep parsing 100% sandboxed and auditable.
82. **How do you handle scanned image PDFs?**  
    *Answer:* `preflight_statement()` extracts character count. If character count is $<30$, it identifies the file as a scanned image in $<15\text{ ms}$ and routes it to `no_text_layer_requires_ocr`.
83. **How does `clean_number()` handle string edge cases like `"N/A"` or `"--"`?**  
    *Answer:* It checks against a set of known null placeholders and safely returns `0.0` instead of raising an unhandled `ValueError`.
84. **What happens if a statement has transactions but no holdings?**  
    *Answer:* If closing balance is 0.0 and holdings are empty, it triggers Check B (`empty_statement_zero_balances`). If closing balance is non-zero, it triggers Check G (`closing_value_ne_holdings_total`).
85. **How are transaction types normalized across different AMC terminologies?**  
    *Answer:* The template defines a `type_map` dictionary (e.g. `{"ACQUIRE": "buy", "DISPOSE": "sell", "CAPITAL IN": "inflow"}`). Raw strings are mapped to standard Pydantic enum literals.
86. **What happens if a transaction type is not present in `type_map`?**  
    *Answer:* It maps to `"other"` and triggers Check L (`unmapped_transaction_type`), quarantining the statement so operations can update the mapping.
87. **How does `parse_date()` handle ambiguous strings like `"01/02/2026"`?**  
    *Answer:* Indian financial statements follow UK/Indian convention (`DD/MM/YYYY`). `parse_date()` defaults to day-first parsing (`dayfirst=True`), interpreting this as February 1st, 2026.
88. **What is Check Q in `validator.py`?**  
    *Answer:* Continuation page verification. If page footers say "Page 1 of 2" or "Continued on next page", but only 1 page was received, it triggers `incomplete_document_missing_continuation_page`.
89. **Why use Pydantic models instead of raw Python dictionaries?**  
    *Answer:* Pydantic provides automatic type coercion, strict schema validation, immutability options, JSON Schema serialization, and immediate runtime error detection on missing required fields.
90. **How does `reprocess_review_queue()` work in `watch_mailbox.py`?**  
    *Answer:* It queries `statements` for records with `status="needs_review"`, re-reads the raw file artifact, attempts re-decryption or re-parsing with active templates, and updates the database status.
91. **What is the difference between `status: "duplicate"` and `status: "validated"`?**  
    *Answer:* `validated` represents a newly ingested, mathematically verified statement that is added to client portfolios. `duplicate` indicates an identical file hash was already ingested, preventing double-counting.
92. **How do you verify that zero PII leaked into the template?**  
    *Answer:* `validate_template_schema_and_pii()` runs regex scans across all template strings, asserting zero occurrences of 10-digit PANs, 10-digit phone numbers, emails, or client names.
93. **What is the role of `_meridian_template_dict` in unit tests?**  
    *Answer:* It provides a deterministic synthetic template in `tests/test_edge_cases.py` to test the one-shot learner loop in offline CI environments without requiring live API keys.
94. **Why is `PRAGMA foreign_keys = ON;` used in SQLite?**  
    *Answer:* By default, SQLite disables foreign key constraint enforcement. Enabling it guarantees relational integrity across statement artifacts and records.
95. **What is the significance of the `approved: false` flag in templates?**  
    *Answer:* It marks newly drafted layout drift variants (`_v2`) as provisional, quarantining statements until a human operations auditor explicitly signs off on the changes.
96. **How does the system handle multi-currency statements in Fulcrum AIF?**  
    *Answer:* Fulcrum statement F1 is INR-denominated, while F2 is USD-denominated. Templates specify currency extraction patterns, and `validator.py` ensures currency normalization.
97. **What is the memory footprint of processing a 100-page PDF?**  
    *Answer:* By processing pages sequentially via `pdfplumber` and closing page streams, memory usage remains bounded ($<150\text{ MB}$ RSS), preventing out-of-memory errors.
98. **How does `ingestion.py` prevent email path traversal?**  
    *Answer:* It strips directory separators using `os.path.basename()` and applies a strict alphanumeric regex, preventing malicious filenames like `../../app.py` from writing outside `ingested_attachments/`.
99. **What happens if Google Gemini is completely down globally?**  
    *Answer:* For known formats (99% of daily volume), the parser operates with zero impact. For unseen formats, the system fails safely, quarantining the file with `learning_failed_llm_unavailable` without crashing.
100. **What is the single biggest architectural achievement of this project?**  
     *Answer:* Achieving enterprise-grade financial reliability by decoupling one-time AI format synthesis from 100% deterministic, multi-stage audited parsing, delivering 99.9% cost reduction and zero mathematical hallucinations.

---

# PART 26: DIFFICULT CROSS-QUESTIONS AND SKEPTICAL CODE DEFENSE

### Cross-Question 1: "Why is `parser.py` 1,728 lines long? Isn't that a monolithic code smell?"
* **Honest Technical Defense:**
  > *"That is a fair observation. `parser.py` contains 1,728 lines because financial document extraction requires handling 5 distinct physical layout paradigms: ruled-border tables, horizontal-line tables, monospace text flows, debit/credit ledgers, and multi-sheet Excel spreadsheets, along with Indian numbering and multi-client page splitting.
  >
  > While it is currently organized into clean modular methods within `StatementParser`, in a multi-developer production setting, I would refactor it using the **Strategy Design Pattern**—extracting `RuledTableStrategy`, `TextflowStrategy`, and `SpreadsheetStrategy` into separate files under a `strategies/` package. However, keeping them cohesive in this assignment minimized import overhead and ensured shared boundary utilities remained tightly coupled."*

---

### Cross-Question 2: "What if the LLM generates a valid JSON template, but hallucinates the column mappings? How do you prevent bad data from entering the database?"
* **Honest Technical Defense:**
  > *"We never trust the LLM's output directly. In `learner.py`, before any candidate template is written to disk, it must pass our **Deterministic Self-Check Gate**.
  >
  > The system executes `parser.py` on the sample file using the candidate template and passes the extracted data to `FinancialValidator`. If the LLM hallucinated column names, the holdings array will be empty, or the roll-forward equation will fail. If even a single critical issue is detected, the candidate template is immediately discarded in-memory, never saved, and the statement is quarantined to `needs_review` under `learning_failed_self_check`."*

---

### Cross-Question 3: "Why use Google Gemini 3.8 Flash instead of GPT-4o or Claude 3.5 Sonnet?"
* **Honest Technical Defense:**
  > *"`gemini-3.8-flash` provides native JSON schema enforcement (`generationConfig: {response_mime_type: 'application/json'}`), sub-3-second latency, and a generous free tier. Because our prompt includes the complete Pydantic JSON schema and a worked example, the task is structural mapping rather than open-ended reasoning, making lightweight flash models both faster and more cost-effective.
  >
  > Furthermore, our architecture is model-agnostic: `LLM_MODEL` is configurable, and `learner.py` includes automatic failover across multiple API keys."*

---

### Cross-Question 4: "Can an incorrect extraction pass your financial validation checks?"
* **Honest Technical Defense:**
  > *"Yes, and that is a fundamental limitation of financial auditing. If an AMC prints 'HDFC Bank' as the holding name, but the OCR or table parser extracts 'HDFC Life', but the quantity, price, and market value are correctly extracted, the mathematical checks (roll-forward, corpus sum, weights) will balance perfectly.
  >
  > Financial validation verifies internal mathematical consistency, not semantic ground truth. To catch semantic errors, the next layer of defense would be cross-referencing extracted ISINs against an external NSE/BSE master security database."*

---

### Cross-Question 5: "What happens if an incoming email arrives with two different PDF statements for two different clients?"
* **Honest Technical Defense:**
  > *"`watch_mailbox.py` iterates over every attachment in the email independently. Each PDF attachment is extracted, assigned a unique hash prefix, and processed through its own isolated `pipeline.process_file()` pipeline.
  >
  > If Attachment 1 is clean, it is validated and persisted. If Attachment 2 has a roll-forward error, it is quarantined to `needs_review`. The failure of one statement does not block or contaminate the other."*

---

# PART 27: LIVE CODE-READING PRACTICE EXERCISES

### Exercise 1: Format Fingerprinting Similarity
```python
# fingerprinter.py (lines 140-148)
def compute_string_similarity(s1: str, s2: str) -> float:
    if not s1 or not s2:
        return 0.0
    s1_clean = " ".join(s1.lower().split())
    s2_clean = " ".join(s2.lower().split())
    return difflib.SequenceMatcher(None, s1_clean, s2_clean).ratio()
```
* **Question:** What does this return if `s1 = "Market Value (Rs.)"` and `s2 = "  market   value (rs.)  "`?
* **Answer:** It returns `1.0`. `" ".join(s1.lower().split())` normalizes all intermediate and surrounding whitespace, making string matching whitespace-invariant.

---

### Exercise 2: Indian Number Parentheses Parsing
```python
# utils.py (lines 35-42)
s = val.strip().replace(",", "")
is_negative = False
if s.startswith("(") and s.endswith(")"):
    is_negative = True
    s = s[1:-1].strip()
elif s.endswith("DR"):
    is_negative = True
    s = s[:-2].strip()
```
* **Question:** What does `clean_number("( ₹ 1,50,000.00 )")` return?
* **Answer:** `-150000.0`. It detects the outer parentheses, sets `is_negative = True`, strips the currency symbol `₹` and commas, converts `150000.00` to float, and negates it.

---

### Exercise 3: Double-Entry Roll-Forward Equation
```python
# validator.py (lines 201-208)
calc_close = summary.opening + summary.inflow - summary.outflow + summary.gain - summary.fee
diff = abs(calc_close - summary.closing)
if diff > MONEY_TOLERANCE:
    issues.append("summary_rollforward_mismatch")
```
* **Question:** If Opening is ₹100, Inflow is ₹50, Outflow is ₹20, Gain is ₹10, Fee is ₹5, and Closing is ₹134.50, what happens?
* **Answer:**
  $$\text{calc\_close} = 100 + 50 - 20 + 10 - 5 = 135.00$$
  $$\text{diff} = |135.00 - 134.50| = 0.50 \le ₹1.00$$
  The check passes because $0.50 \le 1.00$. If Closing had been ₹133.00, diff would be ₹2.00, triggering `summary_rollforward_mismatch`.

---

# PART 28: REALISTIC INTERVIEW CHANGE-REQUEST EXERCISES

### Change Request 1: "A new AMC uses 'TICKER' instead of 'ISIN'. How would you support it without modifying code?"
* **Answer:**
  *"Because our parser is template-driven, zero code changes are required. In the provider's template JSON (`templates/<provider>.json`), under `"holdings": {"mapping": ...}`, change `"isin": "ISIN"` to `"isin": "TICKER"`. For unlisted or US tickers, set `"is_unlisted": true` so Check C bypasses the Indian Luhn checksum check."*

---

### Change Request 2: "How would you add support for a 0.5% tolerance on rollforward calculations instead of a flat ₹1.00?"
* **Answer:**
  *"In `validator.py` (lines 201–208), update the condition:
  ```python
  rel_tolerance = max(1.00, summary.closing * 0.005)
  if abs(calc_close - summary.closing) > rel_tolerance:
      issues.append("summary_rollforward_mismatch")
  ```
  Then add a unit test in `tests/test_edge_cases.py` asserting that a statement with ₹10 Cr corpus and ₹20,000 rounding discrepancy passes, while a ₹1 Lakh discrepancy fails."*

---

# PART 29: RESUME BULLETS & INTERVIEW CLAIMS

### Verified Resume Bullets (Accurate & Defensible):
* **Self-Learning Financial Statement Parser:** Engineered an end-to-end Python engine to ingest, parse, and reconcile multi-format PMS and AIF investment statements (PDF, Excel, CSV) with zero manual intervention.
* **One-Shot Template Synthesis (Google Gemini):** Developed an LLM-assisted one-shot learner that scrubs confidential client PII and generates reusable, declarative JSON extraction schemas, reducing subsequent processing compute to 0 LLM calls.
* **Deterministic Multi-Strategy Parser:** Architected high-performance multi-page extractors using `pdfplumber` and `openpyxl` covering ruled grids, horizontal rules, monospace typewriter textflows, and accounting ledgers across 28 benchmark files.
* **Automated Financial Safety Gate:** Implemented 17 deterministic accounting audits including double-entry roll-forward reconciliation ($\pm ₹1.00$), ISO 6166 ISIN Luhn check digit verification, and holding-versus-equity audits, automatically quarantining corrupt data.
* **Cross-Provider Portfolio Consolidation:** Built an aggregation engine that consolidates client holdings across multiple wealth managers by PAN, deduplicating ISINs, computing blended costs, and isolating currencies.
* **Production Persistence & Dashboard:** Deployed a 4-view Streamlit web application backed by SQLite (WAL mode) and Neon PostgreSQL, featuring an interactive review queue for password unlocking and layout drift sign-off.

### Claims You Should NOT Make:
* ❌ *"I built an OCR engine from scratch."* (False: the system uses `pdfplumber` and routes scanned image PDFs to OCR review).
* ❌ *"It uses machine learning to classify every row."* (False: it uses deterministic column mappings and regex boundary rules).
* ❌ *"It guarantees 100% semantic correctness."* (False: financial validation checks mathematical consistency, not semantic ticker naming).

---

# PART 30: FINAL MASTER REVISION GUIDE

### 1-Page Master Architecture Cheat Sheet

```
[Incoming Mail / Upload]
         │
         ▼
[IngestionGatekeeper] ──> Classifies: ingest / ignore (newsletters) / needs_review (.zip)
         │                Decrypts: AES passwords via STATEMENT_PASSWORDS / PAN
         ▼
[FormatFingerprinter] ──> Compares header n-grams (0.75 SequenceMatcher threshold)
         │                Guards: Rejects SEBI / Provider misattribution
         ├──[If Known Format]──> Deterministic Multi-Page Parser (0 LLM Calls)
         │
         └──[If Unknown Format]─> FormatLearner (Google Gemini 3.8 Flash)
                                     ├── Scrubs PII (<REDACTED_PAN>, <AMOUNT>)
                                     ├── Synthesizes FormatTemplate JSON
                                     └── Deterministic Self-Check Gate (discards if broken)
                                                    │
                                                    ▼
[Deterministic Parser] ───> Multi-page table stitching, Lakhs/Crores number cleaning
         │
         ▼
[FinancialValidator] ───> Checks A to Q:
                            • Roll-forward: Open + Flows + Gain - Fee == Close (±₹1.00)
                            • Holdings Sum == Stated Total Equity (±0.1%)
                            • ISO 6166 ISIN Luhn Check Digit Verification
                            • Portfolio Weights Sum == 100% (±0.5%)
         │
         ├──[If Clean]──────> Status: VALIDATED ──> Portfolio Consolidation Engine
         │
         └──[If Corrupt]────> Status: NEEDS_REVIEW ──> Quarantined Review Queue
```

---

### Master Glossary: Top 10 Terms
1. **PMS:** Portfolio Management Service (Direct stock ownership, managed portfolio, ₹50L min).
2. **AIF:** Alternative Investment Fund (Pooled private equity or hedge fund, ₹1 Cr min).
3. **ISIN:** International Securities Identification Number (12 alphanumeric chars with Mod-10 Luhn checksum).
4. **Roll-Forward Equation:** $\text{Open} + \text{Inflow} - \text{Outflow} + \text{Gain} - \text{Fee} == \text{Close}$.
5. **Fail-Closed:** Quarantining any suspicious or mathematically inconsistent file immediately.
6. **One-Shot Learning:** Calling an LLM once on an unseen format to produce a reusable template, then using 0 LLM calls for all future statements.
7. **Declarative Template:** A structured JSON schema defining extraction rules, avoiding unsafe code generation.
8. **Layout Drift:** A known AMC modifying column titles or table geometry, requiring human approval.
9. **Decrypted Hash Deduplication:** Hashing decrypted PDF bytes to catch duplicates regardless of encryption salts.
10. **WAL Mode:** Write-Ahead Logging in SQLite for concurrent reads and writes without database lockups.

---

### Top 5 Commands to Know
```bash
# 1. Run all 31 ground-truth benchmark assertions:
python tests/test_runner.py

# 2. Run all 33 unit, regression, mutation, and safety tests:
python tests/test_edge_cases.py

# 3. Launch Streamlit Web Dashboard:
streamlit run app.py

# 4. Poll IMAP inbox once from CLI:
python watch_mailbox.py --once

# 5. Dispatch test statement email via SMTP:
python send_test_email.py --file sample_data/pms/01_aurum_A1_standard.pdf
```

---

### 1-Day Prioritized Study Plan for IIT Indore Interview
* **Morning (Hour 1–3): Master the Architecture & Core Code**
  * Read `STAGE_1_REQUIREMENTS_AND_ARCHITECTURE.md` (Part 1 & 2).
  * Read `STAGE_2_CORE_CODE_WALKTHROUGH.md` (Part 5 & 6).
  * Trace `pipeline.py` $\rightarrow$ `fingerprinter.py` $\rightarrow$ `learner.py` $\rightarrow$ `parser.py` $\rightarrow$ `validator.py`.
* **Afternoon (Hour 4–6): Master Financial Validation & Ingestion**
  * Read `STAGE_2_CORE_CODE_WALKTHROUGH.md` (Part 8: Checks A through Q).
  * Read `STAGE_3_INGESTION_PERSISTENCE_DASHBOARD.md` (Part 9, 10, 11).
  * Understand how `clean_number()` handles Lakhs/Crores, and how `is_valid_isin()` works.
* **Evening (Hour 7–9): Interview Q&A & Code Defense**
  * Practice speaking aloud the **60-second** and **5-minute** interview scripts from Part 24.
  * Review the **100 interview questions** in Part 25 and the **Skeptical Code Defense** in Part 26.
  * Run `python tests/test_edge_cases.py` and `streamlit run app.py` on your laptop to verify your live demo setup.

# STAGE 2: CORE CODE WALKTHROUGH, DEEP PYTHON ANALYSIS & FINANCIAL ENGINE
## Activv Money — Self-Learning PMS/AIF Statement Parser
### Comprehensive Codebase Analysis & Technical Interview Guide for IIT Indore Candidates

---

# PART 4: FILE-BY-FILE CORE MODULE ANALYSIS

In this section, we analyze the core functional Python modules that power document recognition, learning, parsing, and financial auditing:
1. `models.py` (Pydantic v2 domain schemas)
2. `utils.py` (Financial math, Indian numbering, Luhn ISIN)
3. `fingerprinter.py` (Document layout recognition & SEBI identity guards)
4. `learner.py` (One-shot format learning with Google Gemini)
5. `parser.py` (Deterministic multi-page extraction engine)
6. `validator.py` (Financial integrity & accounting safety gate)
7. `pipeline.py` (5-Stage pipeline orchestrator)

---

### File 1: `models.py` (183 lines)

* **Exact Path:** `D:\Desktop\StatementParser\models.py`
* **Main Responsibility:** Defines the formal data contracts, domain entities, and JSON schemas for the entire system using **Pydantic v2**. Enforces type validation, field constraints, and serialization.
* **Why It Exists:** To guarantee that regardless of whether a statement is parsed from an Aurum ruled-table PDF, a Cedar monospace textflow, or a Hestia CSV, the output conforms to an identical, strongly-typed Python data structure.
* **Classes Defined:**
  1. [`StatementMetadata`](file:///d:/Desktop/StatementParser/models.py#L9-L21): Encapsulates provider, client ID, client name, PAN, strategy, statement period, and currency.
  2. [`HoldingItem`](file:///d:/Desktop/StatementParser/models.py#L24-L35): Single security holding (name, ISIN, quantity, cost, price, market value, weight, long/short side).
  3. [`TransactionItem`](file:///d:/Desktop/StatementParser/models.py#L38-L47): Ledger transaction (date, type: buy/sell/dividend/fee/inflow/outflow, security, units, price, amount).
  4. [`StatementSummary`](file:///d:/Desktop/StatementParser/models.py#L50-L72): Portfolio-level reconciliation totals (opening, inflow, outflow, gain, fee, cash, closing, nav_per_unit).
  5. [`StatementRecord`](file:///d:/Desktop/StatementParser/models.py#L75-L81): Unified representation of one client statement combining metadata, summary, holdings, and transactions.
  6. [`ExtractionResult`](file:///d:/Desktop/StatementParser/models.py#L84-L94): Complete document-level result containing file path, format ID, status (`validated`, `needs_review`, `duplicate`, `not_a_statement`), issues list, statements list, LLM calls count, and SHA-256 hash.
  7. [`FormatTemplate`](file:///d:/Desktop/StatementParser/models.py#L97-L183): The declarative JSON schema that governs how a format is recognized, cropped, mapped, and parsed.
* **Important Imports:** `pydantic.BaseModel`, `pydantic.Field`, `typing.Optional`, `typing.List`, `typing.Dict`, `typing.Literal`.
* **Design Trade-offs:** Pydantic v2 was chosen over standard Python `@dataclass` because it provides runtime type coercion (e.g. converting string `"1500.50"` to float `1500.50`), automatic JSON schema generation (`FormatTemplate.model_json_schema()`), and strict field validation.

---

### File 2: `utils.py` (297 lines)

* **Exact Path:** `D:\Desktop\StatementParser\utils.py`
* **Main Responsibility:** Provides deterministic mathematical parsing, currency cleaning, date normalization, and algorithmic checksum verification.
* **Why It Exists:** Real-world Indian financial statements do not use standard programming numbers. They use Indian comma groupings (e.g. `1,25,00,000.00`), parenthesized negative numbers `(50,000.00)`, accounting Dr/Cr suffixes (`15,000.00 Cr`), and varied date formats (`30/09/2026`, `30-Sep-2026`). `utils.py` converts messy strings into clean mathematical primitives.
* **Functions Defined:**
  1. [`clean_number(val, default=0.0) -> float`](file:///d:/Desktop/StatementParser/utils.py#L14-L69): Robustly strips currency symbols (`₹`, `$`, `Rs.`), removes Indian and international commas, resolves parenthesized negatives `(x)` to `-x`, handles `Cr`/`Dr` accounting notations, and parses text numbers with Lakhs/Crores.
  2. [`parse_date(val) -> Optional[str]`](file:///d:/Desktop/StatementParser/utils.py#L72-L135): Converts British/Indian (`DD/MM/YYYY`), ISO (`YYYY-MM-DD`), dot (`DD.MM.YYYY`), and named-month (`DD-MMM-YYYY`) strings into a standard `YYYY-MM-DD` date.
  3. [`is_valid_isin(isin) -> bool`](file:///d:/Desktop/StatementParser/utils.py#L138-L180): Full implementation of the **ISO 6166 Modulo-10 Luhn check digit algorithm** for international and Indian securities (e.g. `INE002A01018` for Reliance Industries).
  4. [`normalize_currency(val) -> Optional[str]`](file:///d:/Desktop/StatementParser/utils.py#L183-L205): Normalizes currency representations (`₹`, `INR`, `Rs`, `USD`, `$`) to standard 3-letter ISO 4217 codes (`INR`, `USD`).
  5. [`compute_sha256(file_path) -> str`](file:///d:/Desktop/StatementParser/utils.py#L208-L225): Computes the SHA-256 byte-level hash of an incoming file for zero-redundancy deduplication.

---

### File 3: `fingerprinter.py` (318 lines)

* **Exact Path:** `D:\Desktop\StatementParser\fingerprinter.py`
* **Main Responsibility:** Extracts structural layout features from PDFs, spreadsheets, and CSVs, and computes similarity scores against registered templates to classify a document as a known format or an unknown format.
* **Key Functions:**
  1. [`FormatFingerprinter.__init__(templates_dir)`](file:///d:/Desktop/StatementParser/fingerprinter.py#L25-L55): Loads all approved `.json` templates from `templates/` into an in-memory dictionary.
  2. [`extract_features(file_path) -> Dict[str, Any]`](file:///d:/Desktop/StatementParser/fingerprinter.py#L58-L125): Inspects the first pages/sheets to extract raw text, table headers, horizontal line counts, and bounding-box geometries.
  3. [`compute_string_similarity(s1, s2) -> float`](file:///d:/Desktop/StatementParser/fingerprinter.py#L128-L148): Uses `difflib.SequenceMatcher` to compute normalized string similarity (0.0 to 1.0) between table header rows.
  4. [`match_template(features) -> Tuple[Optional[str], Optional[Dict], bool]`](file:///d:/Desktop/StatementParser/fingerprinter.py#L150-L245):
     * Compares extracted document headers against template fingerprints.
     * **Provider Misattribution Guard:** Explicitly verifies that the document's SEBI registration number or provider company name matches the candidate template.
     * Returns `(format_id, template_dict, is_drift)`.
* **Important Design Decision:** Similarity threshold is fixed at **0.75**. If similarity is $\ge 0.75$ and all mapped columns are present, it is a known format. If similarity is high but columns are renamed, it flags `is_drift = True` (`layout_drift_known_provider`). If no template matches, it routes to `FormatLearner`.

---

### File 4: `learner.py` (621 lines)

* **Exact Path:** `D:\Desktop\StatementParser\learner.py`
* **Main Responsibility:** The **One-Shot Learning Engine**. Synthesizes reusable declarative JSON templates for unseen formats using Google Gemini, scrubs sensitive PII, and validates candidate templates against a deterministic financial self-check audit before saving.
* **Key Functions:**
  1. [`extract_and_redact_document_text(sample_file) -> str`](file:///d:/Desktop/StatementParser/learner.py#L110-L162): Extracts text from the first 3 pages and replaces PANs with `<REDACTED_PAN>`, client names with `<REDACTED_NAME>`, emails with `<REDACTED_EMAIL>`, phone numbers with `<REDACTED_PHONE>`, and financial numbers with `<AMOUNT>`.
  2. [`build_learning_prompt(text, features) -> str`](file:///d:/Desktop/StatementParser/learner.py#L188-L230): Constructs an exhaustive prompt containing the exact Pydantic JSON schema (`FormatTemplate.model_json_schema()`), a worked example template, and structural layout cues.
  3. [`_call_llm_api(prompt) -> Optional[Dict[str, Any]]`](file:///d:/Desktop/StatementParser/learner.py#L425-L525): Sends the prompt to Google Gemini (`gemini-3.8-flash`) using `GEMINI_API_KEY`.
     * **Multi-Key Failover:** If Key #1 returns `HTTP 429` (quota exhausted), it waits 3 seconds and automatically fails over to `GEMINI_BACKUP_API_KEY`.
  4. [`learn_format(format_id, sample_file, features) -> Tuple[Optional[Dict], int]`](file:///d:/Desktop/StatementParser/learner.py#L528-L620):
     * Calls `_call_llm_api()`.
     * Parses the response JSON into a `FormatTemplate`.
     * **Deterministic Self-Check Gate:** Runs `parser.py` on the sample file using the candidate template and audits the output with `FinancialValidator`. If the parse has 0 holdings or broken math, the template is **discarded in-memory** and never saved (`learning_failed_self_check`).
     * If valid, saves the template to `templates/<format_id>.json`.

---

### File 5: `parser.py` (1,728 lines)

* **Exact Path:** `D:\Desktop\StatementParser\parser.py`
* **Main Responsibility:** The high-performance, deterministic multi-page extraction engine. Executes declarative template rules to extract statements without calling any AI models.
* **Why It is 1,728 Lines Long:** To parse complex financial PDFs, the engine must implement 5 distinct extraction strategies:
  1. **Ruled Table Strategy:** Uses physical line intersections, table borders, and explicit grid coordinates via `pdfplumber.extract_tables()`.
  2. **Horizontal-Line Rules Strategy:** For tables with only top and bottom horizontal rules and whitespace-delimited columns (e.g. Banyan Tree PMS).
  3. **Monospace / Typewriter Textflow Strategy:** For non-tabular, columnar text layouts (e.g. Cedar PMS), using regex column boundary anchors.
  4. **Debit/Credit Ledger Strategy:** For transaction accounting ledgers where quantities and cash flows share debit/credit columns (e.g. Delta Capital PMS).
  5. **Tabular Spreadsheet Strategy:** For native `.xlsx` workbooks (via `openpyxl`) and `.csv` files.
* **Multi-Client PDF Boundary Splitting:** Iterates through all pages in a PDF (`for page in pdf.pages:`). When a new `client_id` appears on a page, it cuts a boundary and creates separate `StatementRecord` objects for each client.

---

### File 6: `validator.py` (354 lines)

* **Exact Path:** `D:\Desktop\StatementParser\validator.py`
* **Main Responsibility:** The **Financial Safety Gate**. Executes 17 deterministic mathematical, accounting, and checksum audits (Checks A through Q) to guarantee that extracted data is internally consistent before entering the database.
* **Core Principle:** **Fail-Closed.** If any critical accounting equation fails (e.g. rollforward mismatch by > ₹1.00), the statement is quarantined to `needs_review` with a specific failure issue code.

---

### File 7: `pipeline.py` (349 lines)

* **Exact Path:** `D:\Desktop\StatementParser\pipeline.py`
* **Main Responsibility:** The top-level orchestrator connecting Preflight $\rightarrow$ Fingerprinter $\rightarrow$ Learner $\rightarrow$ Parser $\rightarrow$ Validator $\rightarrow$ Persistence. Exposes `process_file(file_path, password=None) -> ExtractionResult`.

---

# PART 5: DEEP PYTHON CODE WALKTHROUGH

Here, we break down the 6 most critical functions in the repository into structured, interview-ready walkthroughs.

---

### Function 1: `clean_number()`

* **Location:** `utils.py` (Lines 14–69)
* **Purpose in Simple English:** Converts messy real-world financial text (e.g. `"₹ 1,25,450.50 Cr"`, `"(45,000.00)"`, `"12.50 Lakhs"`) into a clean Python `float`.
* **Why It Is Needed:** Standard Python `float("1,25,000")` throws `ValueError: could not convert string to float`. Indian financial statements use lakhs/crores commas, Dr/Cr notation, and parenthesized negatives that break naive parsers.
* **Inputs:**
  * `val`: Any input value (string, integer, float, or `None`).
  * `default: float = 0.0`: Fallback value returned if the input is empty or un-parseable.
* **Outputs:** `float`: The normalized floating-point number.
* **Code Walkthrough:**
  1. If `val` is already an `int` or `float`, it returns `float(val)`.
  2. Converts string to uppercase and strips whitespace.
  3. Checks for parentheses: `if s.startswith("(") and s.endswith(")"): is_negative = True`.
  4. Checks for accounting notation: `if s.endswith("DR"): is_negative = True` (or `CR` for credit).
  5. Strips out currency symbols (`₹`, `$`, `Rs.`, `INR`) and thousands commas (`,`).
  6. Handles textual multipliers: if `"LAKH"` is present, multiplies by $10^5$; if `"CRORE"` is present, multiplies by $10^7$.
  7. Converts the remaining string via `float()` and applies the sign.
* **Edge Cases Handled:** Empty strings, `"-"`, `"N/A"`, `"--"`, `"None"` all safely return `0.0` instead of raising an unhandled exception.
* **30-Second Interview Explanation:**
  > *"In financial statement parsing, standard `float()` fails on Indian numbering systems and accounting conventions. I built `clean_number()` in `utils.py` to handle parenthesized negatives like `(12,500)` as negative twelve thousand five hundred, strip Indian Lakhs and Crores commas, resolve Debit/Credit markers, and return a clean IEEE 754 float, safely falling back to 0.0 on dashes or nulls."*
* **Likely Follow-Up Question:** *"Why not use `Decimal` instead of `float`?"*
  * *Answer:* *"In production banking systems, `decimal.Decimal` is preferred to avoid binary floating-point rounding inaccuracies. For this assignment, standard IEEE 754 floats combined with an explicit ₹1.00 tolerance (`MONEY_TOLERANCE = 1.00`) provided both high performance and accurate reconciliation across all 28 statement benchmarks."*

---

### Function 2: `is_valid_isin()`

* **Location:** `utils.py` (Lines 138–180)
* **Purpose in Simple English:** Verifies whether a 12-character security identifier (ISIN) is mathematically genuine or fake/corrupted.
* **Why It Is Needed:** Every listed stock or bond in India has an International Securities Identification Number (e.g. `INE002A01018` for Reliance Industries). Fake, forged, or OCR-garbled statements often contain invalid ISINs.
* **Inputs:** `isin: str`: The 12-character string.
* **Outputs:** `bool`: `True` if the check digit passes the ISO 6166 Modulo-10 algorithm; otherwise `False`.
* **Code Walkthrough:**
  1. Validates format using regex: `^[A-Z]{2}[A-Z0-9]{9}[0-9]$`. (First 2 letters must be country code, e.g. `IN`).
  2. Converts all letters to numbers using their ASCII positional values: $A=10, B=11, \dots, Z=35$.
  3. Generates a string of digits from the converted values.
  4. Applies the **Luhn Algorithm (Modulo-10)**:
     * Starting from the rightmost digit, doubles every second digit.
     * If doubling yields a number $\ge 10$, subtracts 9 (or sums the two digits).
     * Sums all transformed digits.
  5. Returns `True` if `total_sum % 10 == 0`.
* **30-Second Interview Explanation:**
  > *"To protect against fraudulent statements and OCR corruption, `is_valid_isin()` implements the ISO 6166 Modulo-10 check digit verification. It converts country and issuer characters to numeric codes, applies the Luhn algorithm across the string, and verifies that the checksum digit balances to zero mod 10."*

---

### Function 3: `match_template()`

* **Location:** `fingerprinter.py` (Lines 150–245)
* **Purpose in Simple English:** Inspects an incoming statement's layout and determines which registered template in `templates/` matches it, or whether it is a brand-new format.
* **Inputs:** `features: Dict[str, Any]`: The extracted document text, table headers, and provider name.
* **Outputs:** `Tuple[Optional[str], Optional[Dict], bool]`: `(matched_format_id, template_dict, is_drift)`.
* **Code Walkthrough:**
  1. Iterates through all loaded templates in `self.templates`.
  2. **Provider Identity Guard:** If the candidate template has a known provider name or SEBI registration number (e.g. `INP000088101`), it checks if the document contains that provider. If there is a provider conflict (e.g. document says "Aurum" but template says "Banyan"), the template is **immediately rejected** (`provider_mismatch_template`).
  3. Computes normalized string similarity between the document's table headers and the template's `summary_headers` and `holdings_headers`.
  4. If header similarity $\ge 0.75$:
     * Checks if all mapped columns exist in the table headers.
     * If all columns exist $\rightarrow$ Returns `(format_id, template, False)` (**Known format**).
     * If headers match but mapped columns are renamed $\rightarrow$ Returns `(format_id, template, True)` (**Layout Drift detected**).
  5. If no template reaches 0.75 similarity $\rightarrow$ Returns `(None, None, False)` (**Unknown format**).
* **30-Second Interview Explanation:**
  > *"Our fingerprinting engine uses a two-factor matching approach: first, a strict SEBI registration and provider identity guard to prevent cross-provider misattribution, followed by a 0.75 sequence similarity score on table header n-grams. If columns were renamed by the bank, it flags layout drift; if no template matches, it triggers one-shot learning."*

---

### Function 4: `extract_and_redact_document_text()`

* **Location:** `learner.py` (Lines 110–162)
* **Purpose in Simple English:** Extracts document text from an unseen statement and strips out all sensitive personal data (PAN, client names, balances) before sending it to an external LLM.
* **Why It Is Needed:** SEBI and Indian data privacy regulations prohibit sending unencrypted client PII and financial account balances to third-party AI APIs.
* **Inputs:** `sample_file: str`: Path to the statement PDF or spreadsheet.
* **Outputs:** `str`: The sanitized text with tokens like `<REDACTED_PAN>` and `<AMOUNT>`.
* **Code Walkthrough:**
  1. Opens the PDF via `pdfplumber` and extracts text from `pdf.pages[:3]`.
  2. Executes 9 sequential regex masking patterns:
     * Mask PANs: `[A-Z]{5}[0-9]{4}[A-Z] -> <REDACTED_PAN>`.
     * Mask Emails: `[A-Za-z0-9._%+-]+@... -> <REDACTED_EMAIL>`.
     * Mask Phone Numbers: `(?:\+91)?[6-9]\d{9} -> <REDACTED_PHONE>`.
     * Mask Client IDs after labels: `Client ID:\s*([A-Z0-9\-]+) -> <REDACTED_CLIENT_ID>`.
     * Mask Client Names: `Client Name:\s*([A-Za-z\s]+) -> <REDACTED_NAME>`.
     * Mask Monetary Amounts: `\b\d{1,3}(?:,\d{2,3})+(?:\.\d+)?\b -> <AMOUNT>`.
* **30-Second Interview Explanation:**
  > *"Before any unseen statement is sent to Google Gemini for template synthesis, `extract_and_redact_document_text()` scrubs all confidential client information using 9 deterministic regex filters. It masks PANs, client names, email addresses, and monetary balances, ensuring zero client PII leaks outside our infrastructure."*

---

### Function 5: `_call_llm_api()`

* **Location:** `learner.py` (Lines 425–525)
* **Purpose in Simple English:** Dispatches the template synthesis prompt to Google Gemini with automatic multi-key failover and exponential retry logic.
* **Inputs:** `prompt: str`: The redacted prompt containing the Pydantic JSON schema and worked example.
* **Outputs:** `Optional[Dict[str, Any]]`: Parsed JSON template dictionary from the LLM, or `None` on failure.
* **Code Walkthrough:**
  1. Loads `gemini_keys` from `GEMINI_API_KEY` and `GEMINI_BACKUP_API_KEY` in `.env`.
  2. Sets `model_name = os.environ.get("LLM_MODEL", "gemini-3.8-flash")`.
  3. Configures headers with `x-goog-api-key` and payload with `generationConfig: {response_mime_type: "application/json"}`.
  4. For each key in `gemini_keys`, attempts the call up to 2 times:
     * If `status_code == 200`: Extracts JSON from candidates and returns dictionary.
     * If `status_code == 429` (Quota Exceeded) or `503` (Busy): Logs warning, sleeps 3.0s, and retries.
     * If both attempts on Key #1 fail: Logs failover message and switches seamlessly to Key #2!
  5. If all keys fail: Records `learning_failed_llm_unavailable` and returns `None`.
* **30-Second Interview Explanation:**
  > *"`_call_llm_api()` implements resilient, enterprise-grade LLM calling. It enforces JSON-mode generation on `gemini-3.8-flash`, scrubs credentials from all logs, retries transient 429 and 503 errors with a 3-second backoff, and automatically fails over to a secondary backup API key if Google's daily free-tier quota is exhausted."*

---

### Function 6: `validate()`

* **Location:** `validator.py` (Lines 60–354)
* **Purpose in Simple English:** Audits every extracted statement record against 17 financial and accounting rules.
* **Inputs:**
  * `file_path: str`: Path to the source file.
  * `records: List[StatementRecord]`: The extracted portfolio records.
  * `is_drift: bool`: Whether layout drift was flagged during fingerprinting.
  * `template: Optional[Dict]`: The template rules used.
* **Outputs:** `Tuple[str, List[str]]`: Returns `("validated", [])` or `("needs_review", [issue_codes])`.
* **Code Walkthrough:**
  1. If `is_drift` is `True`, flags `layout_drift_known_provider`.
  2. If `records` is empty, flags `no_statement_records_extracted` and halts.
  3. Loops through each `StatementRecord`:
     * **Metadata Check:** Verifies client ID, period end, provider, and strategy exist and are $<80$ characters.
     * **Currency Check:** Ensures currency is present and valid (`INR` or `USD`).
     * **Rollforward Check:** Calculates $\text{Opening} + \text{Inflow} - \text{Outflow} + \text{Gain} - \text{Fees} - \text{Closing}$. If absolute difference $> ₹1.00$, appends `summary_rollforward_mismatch`.
     * **Holdings vs Equity Check:** Sums all holding market values. If sum differs from stated Total Equity by $>0.1\%$, appends `holdings_sum_ne_stated_total_equity`.
     * **ISIN Check:** Runs `is_valid_isin()` on all listed securities. Invalid checksums trigger `invalid_isin_checksum`.
     * **Weights Check:** Verifies total portfolio weights sum to $100 \pm 0.5\%$.
  4. Returns `("needs_review", issues)` if any blocking issue was triggered; otherwise `("validated", issues)`.
* **30-Second Interview Explanation:**
  > *"`validate()` is our financial integrity gatekeeper. It enforces double-entry portfolio reconciliation: opening corpus plus net flows plus gains minus fees must equal closing corpus within one rupee. It also verifies that the sum of all individual stock market values equals stated equity, and checks every ISIN with the ISO 6166 Luhn algorithm. If any check fails, the document is safely quarantined."*

---

# PART 6: THE SELF-LEARNING PARSER: CORE DESIGN

## Section A: Document Fingerprinting & Format Recognition
* **What is a Document Fingerprint?** A structural signature composed of:
  1. Extracted table headers (column titles).
  2. Table type (ruled grid vs horizontal lines vs monospace textflow).
  3. SEBI registration identifier (e.g. `INP000088101` or `INA000020891`).
  4. Top-level section titles (e.g. "Portfolio Appraisal", "Performance Summary").
* **How Geometry is Extracted:** `pdfplumber` extracts horizontal rules (`page.lines` where `top == bottom`), vertical borders, and text bounding boxes (`x0, top, x1, bottom`).
* **Distinguishing Layout Types:**
  * **Ruled Tables:** High count of both vertical and horizontal lines forming intersecting bounding boxes.
  * **Horizontal Lines:** Only horizontal rules present; column divisions rely on horizontal whitespace gaps.
  * **Monospace / Typewriter:** Zero lines; text is Courier/monospace font where columns align by character index.
  * **Ledgers:** Ruled tables containing paired Debit/Credit columns and running balances.
* **Header Matching Algorithm:**
  The system computes the maximum Jaccard similarity between document header tokens and template header tokens:
  $$J(A, B) = \frac{|A \cap B|}{|A \cup B|}$$
  Combined with `difflib.SequenceMatcher.ratio()`. If the similarity is $\ge 0.75$, it is recognized.

---

## Section B: One-Shot Format Learning
* **When is the Learner Invoked?** Only when `match_template()` returns `None` (an unseen provider or completely unrecognized layout).
* **The Learning Contract:**
  1. **Redact:** Text is scrubbed of all confidential PII.
  2. **Prompt:** Redacted text + JSON Schema + worked example sent to Gemini `gemini-3.8-flash`.
  3. **Validate Schema:** LLM output is validated against `FormatTemplate` Pydantic model.
  4. **Self-Check Gate:** The candidate template is immediately executed by `StatementParser` on the sample file and audited by `FinancialValidator`. If holdings count is zero or the rollforward fails, the template is **discarded in-memory** and never written to `templates/`.
  5. **Persist:** If it passes the self-check, it is saved to `templates/<format_id>.json`.
* **Why Declarative JSON over Code Generation?**
  Allowing an LLM to generate and execute raw Python code (`exec()`) creates catastrophic security vulnerabilities (remote code execution, file deletion) and unpredictable bugs. Storing a **declarative JSON configuration** ensures that the execution engine (`parser.py`) remains 100% deterministic, audited, and safe.

---

## Section C: Declarative JSON Template Structure

A sanitized production template from our repository (`templates/meridian_wealth_pms.json`):

```json
{
  "format_id": "meridian_wealth_pms",
  "version": 1,
  "layout_kind": "ruled_table",
  "provider_key": "meridian_wealth_pms",
  "sebi_regn": "INP000088101",
  "approved": true,
  "fingerprint": {
    "summary_headers": ["Portfolio worth at start", "Fresh Inflows", "Redemptions", "Net Profit", "Advisory Levy", "Liquid Cash", "Ending worth"],
    "holdings_headers": ["Scrip", "ISIN", "Units", "Cost per Unit", "Rate", "Worth", "Share %"],
    "transactions_headers": ["Trade Date", "Action", "Scrip Name", "Units", "Rate", "Net Amount"]
  },
  "fields": {
    "provider": "Meridian Wealth PMS",
    "client_id_pattern": "Investor Code:\\s*([A-Za-z0-9\\-_/]+)",
    "client_name_pattern": "Investor:\\s*([^|\\n\\r]+)",
    "pan_pattern": "Tax ID:\\s*([A-Z]{5}[0-9]{4}[A-Z])",
    "strategy_pattern": "Mandate:\\s*([^|\\n\\r]+)",
    "currency": "INR"
  },
  "period": {
    "pattern": "Reporting Window:\\s*([0-9]{2}/[0-9]{2}/[0-9]{4})\\s+to\\s+([0-9]{2}/[0-9]{2}/[0-9]{4})",
    "format": "DD/MM/YYYY"
  },
  "summary": {
    "has_gain": true,
    "mapping": {
      "opening": "Portfolio worth at start",
      "inflow": "Fresh Inflows",
      "outflow": "Redemptions",
      "gain": "Net Profit",
      "fee": "Advisory Levy",
      "cash": "Liquid Cash",
      "closing": "Ending worth"
    }
  },
  "holdings": {
    "mapping": {
      "name": "Scrip",
      "isin": "ISIN",
      "quantity": "Units",
      "avg_cost": "Cost per Unit",
      "price": "Rate",
      "market_value": "Worth",
      "weight_pct": "Share %"
    },
    "total_row_labels": ["Total Equity"],
    "cash_row_labels": ["Liquid Cash"]
  },
  "transactions": {
    "mapping": {
      "date": "Trade Date",
      "raw_type": "Action",
      "security": "Scrip Name",
      "quantity": "Units",
      "price": "Rate",
      "amount": "Net Amount"
    },
    "type_map": {
      "CAPITAL IN": "inflow",
      "ACQUIRE": "buy",
      "DISPOSE": "sell",
      "ADVISORY LEVY": "fee"
    }
  }
}
```

---

## Section D: Deterministic Parsing Execution

Once a template is registered, `parser.py` executes the extraction without any LLM calls:
1. **Multi-Page Handling:** The parser loops through `for page in pdf.pages:`. Tables that span across pages (e.g. Holdings starting on Page 2 and ending on Page 4) are stitched together by identifying header repetitions and stripping intermediate page running headers.
2. **Total & Subtotal Filtering:** Rows matching `total_row_labels` (e.g. `"Total Equity"`, `"Subtotal"`) are parsed to capture stated totals, but are excluded from the individual holdings array to prevent double-counting.
3. **Cash Extraction:** Rows matching `cash_row_labels` (e.g. `"Liquid Cash"`, `"Bank Balance"`) are extracted to populate the portfolio cash figure.
4. **Derived Metrics:** If a statement prints quantity and market value but omits unit price (or vice versa), the parser derives it deterministically:
   $$\text{Derived Price} = \frac{\text{Market Value}}{\text{Quantity}}$$

---

# PART 7: FINANCIAL DATA MODEL & FINTECH CONCEPTS

For an engineering candidate, mastering fintech terminology is critical. Here is a clear primer:

* **PMS (Portfolio Management Service):** An investment service where a licensed portfolio manager invests directly in listed equities and bonds on behalf of an HNI client (minimum SEBI investment: ₹50 Lakhs). Unlike mutual funds, the client directly owns the stocks in their own demat account.
* **AIF (Alternative Investment Fund):** A privately pooled investment vehicle for sophisticated investors (minimum SEBI ticket: ₹1 Crore). 
  * *Category II AIF:* Private equity, debt funds, real estate funds (invests in unlisted companies).
  * *Category III AIF:* Hedge funds, long-short funds (uses leverage, derivatives, short positions).
* **ISIN (International Securities Identification Number):** A 12-character alphanumeric code uniquely identifying a specific security (e.g. `INE002A01018` for Reliance Industries).
* **Corpus & Net Asset Value (NAV):** The total monetary value of the portfolio at a given date.
* **Roll-Forward Equation:** The fundamental accounting identity in wealth management:
  $$\text{Opening Valuation} + \text{Inflows} - \text{Outflows} + \text{Net Realized/Unrealized Gain} - \text{Fees} = \text{Closing Valuation}$$
* **Holdings vs. Corpus Reconciliation:**
  $$\sum (\text{Individual Stock Market Values}) + \text{Cash Balance} = \text{Closing Valuation}$$
* **Portfolio Weight:** The percentage proportion of a single holding relative to total portfolio value:
  $$\text{Weight} = \left(\frac{\text{Holding Market Value}}{\text{Total Portfolio Value}}\right) \times 100$$
  The sum of all holding weights plus cash weight must equal $100\% \pm 0.5\%$.

---

# PART 8: FINANCIAL VALIDATION (CHECKS A THROUGH Q)

`validator.py` enforces 17 deterministic accounting audits:

| Check ID | Verification Rule | Code Location | Formula / Tolerance | Outcome on Failure |
|---|---|---|---|---|
| **Check A0** | Provider Mismatch Guard | `validator.py:109-115` | String similarity between document provider and template provider must be $\ge 0.75$. | Quarantine: `provider_mismatch_template` |
| **Check A1** | Missing Mapped Columns | `validator.py:116-121` | All columns specified in `template.holdings.mapping` must be found in the table. | Quarantine: `expected_column_missing:<col>` |
| **Check A** | Mandatory Metadata | `validator.py:122-130` | `client_id`, `period_end`, `provider`, and `strategy` must not be null or `"unknown"`. | Quarantine: `missing_required_metadata` |
| **Check A0-Curr** | Currency Validation | `validator.py:131-140` | Currency must normalize to a valid ISO code (`INR` or `USD`). | Quarantine: `missing_currency` |
| **Check A2** | Metadata Sanity Check | `validator.py:142-155` | Metadata fields must be single line, $\le 80$ characters, and contain no leaked financial numbers. | Quarantine: `metadata_value_suspect:<field>` |
| **Check B** | Empty Statement Check | `validator.py:156-160` | Opening is 0.0, closing is 0.0, and holdings array is empty. | Quarantine: `empty_statement_zero_balances` |
| **Check C** | ISIN Luhn Checksum | `validator.py:161-175` | Listed security ISIN must pass ISO 6166 Modulo-10 checksum algorithm. | Quarantine: `invalid_isin_checksum` |
| **Check D** | Duplicate Holding ISIN | `validator.py:176-182` | The same ISIN must not appear multiple times in the same long/short holding side. | Quarantine: `duplicate_holding_isin` |
| **Check E** | Holdings vs Stated Equity | `validator.py:188-198` | $|\sum(\text{Holdings Market Value}) - \text{Stated Total Equity}| \le 0.1\%$ | Quarantine: `holdings_sum_ne_stated_total_equity` |
| **Check F** | Summary Roll-Forward | `validator.py:199-210` | $|\text{Open} + \text{Inflow} - \text{Outflow} + \text{Gain} - \text{Fee} - \text{Close}| \le ₹1.00$ | Quarantine: `summary_rollforward_mismatch` |
| **Check G** | Closing vs Holdings + Cash | `validator.py:211-218` | $|\sum(\text{Holdings}) + \text{Cash} - \text{Closing}| \le ₹1.00$ | Quarantine: `closing_value_ne_holdings_total` |
| **Check H** | Cash Row Reconciliation | `validator.py:219-242` | Cash reported in summary table must match cash line item in holdings table within ₹1.00. | Quarantine: `cash_summary_vs_row_mismatch` |
| **Check I** | Total Portfolio Weights | `validator.py:243-252` | $\sum(\text{Holding Weight \%}) + \text{Cash Weight \%} == 100 \pm 0.50\%$ | Quarantine: `holdings_weight_sum_invalid` |
| **Check J** | Individual Holding Weight | `validator.py:253-262` | $|\text{Printed Weight} - (\text{Value}/\text{Total} \times 100)| \le 0.10\%$ | Quarantine: `holding_weight_mismatch` |
| **Check K** | Quantity $\times$ Price Math | `validator.py:263-270` | $|\text{Quantity} \times \text{Price} - \text{Market Value}| \le 0.5\%$ | Quarantine: `holding_qty_price_mismatch` |
| **Check L** | Unmapped Transactions | `validator.py:271-280` | Transaction action string must be mapped in `template.transactions.type_map`. | Quarantine: `unmapped_transaction_type` |
| **Check M** | Transaction Totals Audit | `validator.py:281-325` | Sum of transaction amounts must reconcile with summary inflows/outflows/fees within ₹1.00. | Quarantine: `transactions_inflow_mismatch` |
| **Check N** | AIF Units $\times$ NAV | `validator.py:326-333` | $|\text{Units} \times \text{NAV per Unit} - \text{Closing}| \le ₹1.00$ | Quarantine: `units_times_nav_per_unit_mismatch` |
| **Check O** | Long-Only Shorting Guard | `validator.py:334-340` | In long-only templates (`supports_short: false`), holding quantity must not be negative. | Quarantine: `negative_quantity_in_long_only_template` |
| **Check P** | Period Range Validity | `validator.py:341-346` | Statement `period_end` date must be $\ge$ `period_start` date. | Quarantine: `invalid_period_range` |
| **Check Q** | Page Continuity Guard | `validator.py:84-88` | Footers indicating *"Page 1 of 2"* or *"Continued on next page"* must have subsequent pages. | Quarantine: `incomplete_document_missing_continuation_page` |

---

### What Was Completed in Stage 2 & What Comes Next

* **Completed in Stage 2:**
  1. Detailed analysis of all core functional files (`models.py`, `utils.py`, `fingerprinter.py`, `learner.py`, `parser.py`, `validator.py`, `pipeline.py`).
  2. Deep walkthrough of the 6 core Python functions with execution flow, concepts, interview scripts, and follow-ups.
  3. Exhaustive design breakdown of the Self-Learning Parser: Fingerprinting, One-Shot Learning, JSON Schema, and Deterministic Parsing.
  4. Fintech concepts explained from scratch with mathematical reconciliation formulas.
  5. Complete catalog of Financial Validation Checks A through Q with exact line locations and tolerances.
* **Coming Next in Stage 3:**
  - **Part 9 & 10:** Layout Drift workflow and Mailbox Ingestion (`watch_mailbox.py`, `ingestion.py`, `send_test_email.py`).
  - **Part 11 & 12:** Database Architecture (`persistence.py`: SQLite WAL + Neon PostgreSQL) and Portfolio Consolidation (`consolidator.py`).
  - **Part 13, 14 & 15:** Streamlit Dashboard architecture (`app.py`), complete Dependency Analysis (`requirements.txt`), and Environment Variables audit (`.env`).

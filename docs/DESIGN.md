# Activv Money — Technical Architecture & Design Document

---

## 1. The Big Picture (What This System Does)

High-Net-Worth investors in India keep their wealth spread across multiple **PMS providers** (like Aurum, Delta, Cedar) and **AIF fund houses** (like Fulcrum, Evergreen as exampless). Every month, dozens of PDF and Excel statements arrive in the company's mailbox with completely different, unstandardized layouts.

Instead of having human operators manually copy numbers into spreadsheets, or burning thousands of dollars sending every single page to an AI model, this project is an **Automated Operations Command Center** that:
1. Listens to incoming statement emails automatically via an IMAP listener.
2. Learns a new statement format **once** using an LLM, and then parses it forever using normal, free Python code.
3. Audits all financial numbers with strict double-entry mathematical checks.
4. Quarantines bad math, shifted layouts, or encrypted files in an operations Review Queue.
5. Unifies a client's multi-bank investments into a single consolidated balance sheet.

---

## 2. How We Solved the Problem Statement Requirements

### A. "Learn Once, Parse Infinitely" 
* **First time seeing a bank format (Cold Start):** The system calls Google Gemini **exactly once**. It redacts all client PANs, names, and contact details first, attaches the layout structure and JSON schema, and asks the AI to synthesize a declarative JSON template (table headers, column coordinates, and regex patterns).
* **Mandatory Self-Check Gate:** The AI never generates Python code, only JSON. We immediately run a test parse of the sample document using this candidate template. If the math fails, balances are zero, or required metadata is missing, we immediately discard the template.
* **Every future statement :** Normal Python code uses the saved JSON template to extract holdings in **0.1 seconds with 0 LLM calls ($0.00 cost)**.

### B. The Financial Safety Gate (16 Strict Math Checks)
We never trust extracted numbers blindly. Every single parsed statement must pass 16 mathematical invariant checks before entering the database:
* **Roll-forward check:** `Opening Balance + Inflows - Outflows + Profit - Fees == Closing Balance` (within ₹1.00).
* **Holdings reconciliation:** Sum of individual stock market values + Cash balance must equal the Total Closing Corpus.
* **Per-holding math:** `Quantity * Price == Market Value` for every row.
* **Holding weights:** Portfolio weights must add up to 100% (within +/-0.5%).
* **ISIN integrity:** Every stock and bond ISIN is verified against the official **ISO 6166 Mod-10 Luhn check-digit algorithm** to catch corrupted digits.
* **Fail-Closed Policy:** If even a single check fails, the statement is quarantined in `needs_review`. Corrupted numbers never enter the client's balance sheet.

### C. Layout Drift Detection
* When a known bank quietly changes its layout (e.g., Aurum renaming *"Avg Cost"* to *"Cost Basis"*):
  1. We match the bank's SEBI registration number to verify who they are.
  2. We detect that a mapped column is missing or table similarity dropped below 0.75.
  3. We **refuse to guess**. The system quarantines the file with `layout_drift_known_provider`, creates a candidate `v2` template with `"approved": false`, and alerts an operator in the dashboard to review and click **"Sign Off & Approve"**.

### D. Pre-Classification (Filtering Out Non-Statements)
* Banks also email marketing factsheets, fee invoices, and single-trade contract notes.
* Our Stage 1 Gatekeeper pre-classifies incoming documents: if it detects factsheet headers and no client portfolio tables, it marks it `not_a_statement` and skips it without calling an LLM.

### E. Duplicate Detection & Revisions Across Restarts
* **Hard Duplicates:** We compute the SHA-256 hash of every file. If someone resends the exact same statement under a different name, the system detects the hash in 2ms and flags it as `duplicate` (0 LLM calls).
* **Statement Revisions:** If a bank sends an amended statement for the same client and month with corrected numbers, the system detects matching `(provider, client_id, period_end)` with a different hash and flags it as `revision_of_existing_statement` so an operator can confirm before overwriting old records.

---

## 3. Some Features I Added:

### 1. Live Automated Email Pipeline (IMAP & SMTP Dispatcher)
* **Automated IMAP Listener (`watch_mailbox.py`):** Automatically connects to a Gmail inbox, finds unread emails, safely unbundles attachments, and processes them.
* **SMTP Test Dispatcher (`mailer.py`):** We added an in-app tool where you can upload a PDF and have the system send real emails with random subjects and attachments to test the live mailbox poller end-to-end.


### 2. Interactive Operations Command Center (`app.py`)
A 4-tab Streamlit web application built for wealth management teams:
* **Tab 1: Mailbox Ingestion & Operations:** Real-time email event log, live upload box, and visual cards for client accounts, balances, and holdings tables.
* **Tab 2: Client Portfolio Consolidation:** Takes a client (like `SAMPL1001A`) and merges their holdings across Aurum PMS, Delta PMS, and Evergreen AIF into one consolidated balance sheet with asset allocation charts. Normalizes stocks by 12-character ISIN and isolates USD from INR.
* **Tab 3: Quarantined Review Queue:** Displays all flagged files with clear issue tags, an in-place password unlocker, and template approval buttons.
* **Tab 4: Live Email Dispatch & Ingest:** Live controls to send sample statements or custom attachments to test the email poller.

### 3. Cloud PostgreSQL Database (Neon.tech)
* Upgraded persistence from local SQLite to **Cloud PostgreSQL** with 5 relational tables (`statements`, `format_templates`, `statement_artifacts`, `email_events`, `failed_learning_attempts`).
* Data persists durably across cloud deployments and server restarts.

### 4. Password-Protected PDF Unlocking with Clean Lifecycle
* Encrypted PDFs fail closed with `password_required`.
* Operators can enter the password directly in the Review Queue.


### 5. In-Place Review Queue Reprocessing
* When an operator unlocks a password or approves a drifted layout, clicking "Reprocess" updates the existing database record **in-place** (`UPDATE statements SET status = ... WHERE id = ...`).
* This avoids creating duplicate database IDs or triggering false duplicate self-collisions.

---

## 4. Answers to the  Hard Questions (Assignment Requirements)

### Question 1: What exactly does the LLM produce when it "learns" a format, and how do you make sure a bad template never silently corrupts downstream data?
1. **What the LLM produces:** The LLM outputs a strictly typed `FormatTemplate` JSON object (validated by Pydantic) — **never executable Python code** and **never client numbers**. It contains:
   * Layout type (`ruled_table`, `monospace_text`, `ruled_lines_horizontal_only`, `xlsx`, `csv_sectioned`).
   * Table headers and fingerprint tokens.
   * Regex patterns for client metadata (PAN, Client ID, dates).
   * Column mappings and row labels (`cash_row_labels`, `total_row_labels`).
2. **How bad templates are blocked:**
   * **Gate 1 (PII & Schema Check):** If any client name, PAN, or currency number leaks into the template, it is immediately rejected (`learning_failed_invalid_template`).
   * **Gate 2 (Deterministic Self-Check):** The template is immediately run on the sample PDF. If it produces zero holdings, missing metadata, or fails any mathematical invariant, the template is discarded in memory and never saved to disk.

---

### Question 2: How do you distinguish a genuinely new format from layout drift, and from a non-statement document?
1. **Non-Statement vs. Statement:** The Stage 1 pre-classifier looks for explicit negative headers (`MONTHLY FUND FACTSHEET`, `TAX INVOICE`, `CONTRACT NOTE`) and checks if portfolio holdings tables are absent. Non-statements return `not_a_statement` with zero LLM calls.
2. **Layout Drift vs. New Format:**
   * We first extract the SEBI registration number and provider title.
   * If the SEBI number matches a known provider (like Aurum), but table headers changed or a mapped column is missing, it is identified as **Layout Drift** (`layout_drift_known_provider`).
   * If the SEBI number or provider name is completely unrecognized, it is routed to the One-Shot Learner as a **genuinely new format**.

---

### Question 3: How do you handle duplicate files vs. revised statements for the same client and period, including across process restarts?
1. **Hard Duplicates:** The system computes the SHA-256 hash of every incoming file and checks it against the database. If the hash matches an existing record, it immediately returns `duplicate` with 0 LLM calls, even across system restarts.

---


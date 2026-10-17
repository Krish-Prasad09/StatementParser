#Limitations:

## 1. Portfolio Consolidation (Work in Progress)
The portfolio consolidation feature in Tab 2 is currently quite basic and needs more work. Consider this module an early-stage prototype rather than a finished work. It was made in less time without any code review so things may or may not work. I wanted to add an export to xlsx feature so that this consolidated data could be exported there but for now this is not implemented.

---

## 2. Scanned & Image-Only PDFs (No Bundled OCR)
The system will not attempt to parse image only pdf as data extraction is required from the table by python coding and from images the task was difficult. 

Instead of guessing or crashing, it immediately routes the file to the Review Queue with the issue code `no_text_layer_requires_ocr`. I didn't have much idea how to handle them, so I thought to send it for human review.

---

## 3. Unsupported Attachments: ZIP Archives and Legacy Excel
Emails sent to the system can contain various attachment types. The engine currently accepts and parses three formats: **`.pdf`**, **`.xlsx`**, and **`.csv`**.

* **ZIP Files (`zip_attachment_requires_manual_review`):** We deliberately do not auto-extract `.zip` archives. Unpacking unverified zip files in an automated email pipeline introduces risks hidden malware. Any email with a zip attachment is safely quarantined for manual review.
* **Legacy `.xls` & Macro `.xlsm` (`unsupported_statement_attachment_type`):** system deals with clean, structured `.xlsx` or `.csv` data.

---

## 4. One-Shot Format Learning Window (First 3 Pages)
When an unseen statement arrives and the system calls the LLM to learn the layout, it only extracts text from the **first three pages** (`pdf.pages[:3]`).

* **Why we did this:** It keeps LLM token usage low (<1,500 tokens), reduces API cost, and keeps synthesis speed less.
* **The boundary:** If a wealth manager sends a document where Pages 1 to 3 are purely cover letters, marketing text, or market commentary, and the actual portfolio holdings table only starts on **Page 4 or later**, the LLM won't see the table headers and template generation will fail.
* **Safety:** When this happens, the system fails closed with `learning_failed_self_check` and quarantines the file—it never generates a bad or incomplete template. 
*(Note: This 3-page limit is strictly for the initial template learning step. Once a template exists, the deterministic parser processes **every single page** without any page limit).*

---

## 5. Date Formats & Ambiguity
By default, the date parser operates on the standard Indian financial convention of **Day-First** (`DD/MM/YYYY`, `DD-MM-YYYY`, `DD.MM.YYYY`).

* If a date is ambiguous (e.g., `04/05/2026`), the system interprets it as **May 4th**, not April 5th.
* If a foreign or US-based fund manager issues a statement strictly following Month-First (`MM/DD/YYYY`), there may be issue.

---

## 6. Layout Complexity & Exotic Formats
The deterministic parser supports 5 specific layout families:
1. `ruled_table` (standard grid tables)
2. `monospace_text` (plain text columns)
3. `ruled_lines_horizontal_only` (ledger-style tables)
4. `xlsx` (Excel sheets)
5. `csv_sectioned` (multi-block CSV files)

If an asset manager issues an exotic or non-tabular statement—such as a magazine-style infographic, multi-column newsletter layout, or complex nested pivot table—the engine will not be able to model it as a declarative template. These edge-case documents will fail the automated self-check and must be reviewed manually.


## 7. LLM API Availability & Rate Limiting (Gemini Quota)
For cold-start format learning on brand new statements, the engine relies on the Google Gemini API (`gemini-3.8-flash`). Even though the pipeline supports automatic fallback to a backup key (`GEMINI_BACKUP_API_KEY`), there are times when both keys may fail:
* Rate limits / Quota exceeded.
* Network timeouts or transient downtime from Google's API endpoints.

**How the system handles this safely:**
The engine never crashes or hallucinates if the API is down. Instead, it fails closed, records the  error, and quarantines the statement in the Review Queue with:
`learning_failed_llm_unavailable`

No partial or corrupted templates are saved. Furthermore, **all previously learned formats continue to parse 100% offline with zero LLM calls**.

## 8. Review Queue Quarantine feature option in sidebar
The Quarantined Review Queue allows operators to unlock password-protected PDFs and sign off on drifted templates. However, few bugs were found at the last moment and were tried to fix but it is possible that some errors may still remain unfortunately and I will try my best to figure them out soon if exist.

It is possible that some more bugs would be there, and if I got any I would try to fix it asap....
# Activv Money — AI Usage
Claude link: https://claude.ai/share/d114c9f1-5446-400a-bca3-d8ac7ac8dfbe
Chatgpt link: https://chatgpt.com/share/6ad399ba-5a98-83ec-9a95-cc4d0d30c07e
Chatgpt link2: https://chatgpt.com/share/6ad39b4f-e4f8-83ee-b030-4845bd4703f9
---

SHORT NOTE:
1.AI helped me a lot in understanding finance stuff with which I was new to, make a base of the files on which I have to work on, giving some ideas to handle a particular edge case, telling how to optimise, telling some additional features that would be suitable to the dashboard to add even if it wasn't asked in PS, making the dataset PMS/AIF statement files for checking(thanks to claude for creativity), etc. 
2.It taught me finance from scratch. AI explained it like a tutor: how PMS is direct stock management for rich clients, how AIFs act like hedge funds or private equity, and how basic accounting formulas work (like Opening + Inflows - Outflows + Profit - Fees = Closing). It also explained what an ISIN code is.
3.It gave me a solid starting base. Starting from a completely blank project is the hardest part. AI helped me scaffold the initial folder structure and base files.
4.It generated realistic sample statements (Huge thanks to Claude).Claude was super creative here: it generated authentic-looking sample PDFs, Excel sheets, and CSVs with real Indian stocks (like Reliance, TCS, HDFC Bank). Having different layouts (some with tables, some plain text, some horizontal ledgers) gave me actual data to test against right away.
5. AI gave me great ideas for real-world problems that happen in production:
"What if someone tampered with the printed total amount?"
"What if someone re-saves the same PDF under a different filename?"

However, it misguided me a lot like some that I remember:
1. The "Keyword Hunting" Trap
What AI suggested: "Instead of sending just the first 3 pages to the AI, let's search through all pages for words like 'Portfolio' or 'Holdings' to find where the table is."
Why it was a bad idea: Bank statements usually start with 2 or 3 pages of random text. Those paragraphs mention words like "our equity portfolio" and "client holdings" all over the place without having any actual table! If we followed the AI's advice, the system would pick up a random letter page, get confused, and fail.

2. Mixing Up Different Banks
What AI suggested: "If two statements have tables that look 75% similar, just reuse the same template."
Why it was a bad idea: Many wealth managers use standard column names like Quantity, Price, and Value. If a brand-new bank sent a statement, the AI would blindly classify it under an existing bank (like calling a Zenith statement "Aurum"). I had to fix so different banks never get mixed up.
3. Hardcoding Words Just to Pass the Samples
What AI did: Initially, the AI was lazy and hardcoded section titles like "PORTFOLIO SUMMARY" and "HOLDINGS AS ON" in the parser.

4. Adding Dollars and Rupees Together
What AI did: In the portfolio consolidation tab, the AI simply wrote sum of all holdings across all accounts.
Why it was a bad idea: If a client had ₹40 Lakhs in Aurum (INR) and $50,000 in Fulcrum (USD), the AI happily calculated 40,00,000 + 50,000 = 40,50,000! You can't just add dollars to rupees without an exchange rate. I had to force the system to keep USD and INR strictly separated.
5. The Review Button Fighting Against Itself
What AI did: When building the "Reprocess" button in the Review Queue, the AI wrote code that re-ran the file from scratch.
Why it was a bad idea: The file's fingerprint was already in the database from when it was first quarantined. So when you clicked "Reprocess" after unlocking a password, the system saw its own record and yelled: "Duplicate file detected!" It was literally colliding with itself. I steered it to do an in-place database update instead.
6. Burning API Quota with Useless Retries
What AI did: When calling Google Gemini, the AI wrote code that automatically retried the same model twice, and then retried the backup model twice.
Why it was a bad idea: If Google's server is down (503) or your API key is rate-limited (429), spamming 4 rapid retries in a row just freezes the dashboard for 30 seconds and burns your API quota for nothing! You pointed out: "Do it only once per model, and if it's unavailable, let it fail closed to the review queue so I can retry it later when the server is back up."

Even I thought to keep a backup gemini api using different model instead of using a single gemini api.


## 1. Overview & AI Collaboration Methodology

In developing this solution, AI coding assistants (Claude and Antigravity) were used as pair-programming assistants, adhering to the assignment's guidance.

---
some of the ANTIGRAVITY chats with my original statements:

#### Q1
> *"when a bank changes layout(drift), what you do tell in simple words do you then store new format by calling llm for it, or send it for review"*
* **Brief Answer:** The system flags it as `needs_review` under `layout_drift_known_provider`. It synthesizes a candidate `v2` template with `"approved": false` and holds it until a human operator audits the changes in the dashboard and clicks "Sign Off & Approve", preventing unverified layouts from running in production.

#### Q2
> *"suppose human hasn't approved it yet and again v2 came, so no llm would be called na"*
* **Brief Answer:** Exactly—zero LLM calls are made. The candidate `v2` template is already registered as an unapproved candidate; subsequent statements matching that layout are immediately quarantined to `needs_review` awaiting the same human sign-off without re-triggering the LLM.

#### Q3
> *"learning_failed_self_check when does it come ?"*
* **Brief Answer:** It occurs during cold-start format learning when the LLM produces a syntactically valid JSON template, but when `StatementParser` tests it on the sample document, the extracted data fails financial auditing (e.g., zero holdings extracted, broken math, or missing client metadata). The candidate template is immediately discarded.

#### Q5
> *"I am not liking that llm is given only starting 3 pages for parsing document table, don't you think it should be fixed?"*  
> *"what if page 6 too needs to be taken"*  
> *"our llm wants only one page na that has column headers?"*
* **Brief Answer:** The first 3 pages slice (`pdf.pages[:3]`) is strictly an optimization for the **one-shot learning step** to keep prompt tokens low (<1,500) and latency under 3 seconds. Once the template is learned, the deterministic parser always iterates through **every single page** (`for page in pdf.pages:`) to extract 100% of holdings and transactions.

#### Q6
> *"Opening Valuation+Inflows−Outflows+Gains−Fees==Closing Valuation is it always present in pms aif and what if it is not present"*
* **Brief Answer:** Not all providers print an explicit net gain column (for example, Banyan quarterly statements). Our validator checks the template's `summary.has_gain` flag; if false, it does not enforce arithmetic roll-forward reconciliation, ensuring legitimate non-reconciling layouts pass safely without false alarms.

#### Q7
> *"in .xlsx what if values are given in sheet1,sheet2 then sheet3. is it taken care or not while format extracting"*
* **Brief Answer:** Yes. The openpyxl parser natively inspects all sheets in the workbook (Summary, Holdings, Transactions), identifying tables by their header row signatures regardless of which sheet tab they reside on.

#### Q8
> *"i hope multi client data given in a single pdf statement is also handled separately so that later when in dashboard a particular client data is asked it is shown??"*
* **Brief Answer:** Yes. When a multi-account statement arrives (like file `11_aurum_multi_client_2in1.pdf`), the engine unbundles it into separate `ExtractionResult` records keyed by `(client_id, strategy)`. In the dashboard, each client account appears as an independent portfolio card.

#### Q9
> *"so on restart, parsing for all stored files is repeated ? or only stored processed final data is showed"*
* **Brief Answer:** On restart, parsing is never repeated. Processed statements, metadata, and JSON artifacts are durably stored in PostgreSQL. On startup, the pipeline simply hydrates its in-memory hash cache from the database in milliseconds.

#### Q11
> *"and tell about sql injection prevention"*  
> *"what if attacker sends drop table directly"*
* **Brief Answer:** All database queries in `persistence.py` use parameterized SQL queries with `%s` placeholders via `psycopg2`. Malicious inputs like `DROP TABLE` inside a client name or filename are treated strictly as raw string literals and can never execute as SQL commands.

#### Q12
> *"what if multiple pdf attachments come in same email?"*
* **Brief Answer:** The IMAP listener unbundles each attachment separately, sanitizing filenames with `os.path.basename` and prefixing them with a message hash `{msg_prefix}_{part_idx}_{filename}`. Each attachment is parsed independently and tied back to the email's event log.

#### Q13
> *"is password code working fine, that if entered correct pass on dashboard for statement/non statement files thigns go fine"*  
> *"what about llm calls for password protected files if one with same layout is stored already"*
* **Brief Answer:** Yes. When unlocked in the dashboard, the engine decrypts the file in a secure temp directory, hashes the decrypted content, and matches it against existing templates. If the layout is already known, it parses with 0 LLM calls; if unseen, it triggers the one-shot learner.

#### Q14
> *"when I click on reprocess review queue, it reprocesses, make new db id but even if validation goes correct it sends it to duplicate... why to create new db id for reprocess, either you erase and make new when reprocess is clicked i think"*
* **Brief Answer:** We updated the reprocessor to perform an **in-place update** on the existing database row (`UPDATE statements SET status = ... WHERE id = ...`). This prevents generating new database IDs or triggering false duplicate self-collisions.

#### Q15
> *"why to show reapprove and force sync for a validated schema, you should do this for layout drift na?"*
* **Brief Answer:** Force-syncing was redundant for already validated templates. We scoped the sign-off workflow exclusively to unapproved drifted templates (`_v2.json`) requiring human review.

#### Q16
> *"actually earlier for layout drift i clicked on sign off and approve and it got approved. but i want to say that after clicking on approve the option remains same as i am still getting option of sign off and approve and NEEDS SIGN OFF instead of ACTIVE PRODUCTION"*
* **Brief Answer:** The UI session state was holding onto stale memory references. We added a persistent `promoted` flag to the template JSON, re-synced the fingerprinter registry, and triggered `st.rerun()`, so the badge immediately flips to `ACTIVE PRODUCTION`.

#### Q18
> *"and dashboard is for managing the pms aif statements from these providers by companies like activv money"*
* **Brief Answer:** Exactly. Activv Money is a Multi-Family Office / WealthTech platform. Their clients have wealth distributed across multiple wealth managers. The dashboard serves as an automated **Operations Command Center** that ingests statements, catches bad math, and unifies them into a single consolidated balance sheet.

#### Q19
> *"is sample data...outputs... manifest.csv required here?? or they can be shifted to Mr. Tester too?"*  
> *"and also tell me is there any sense of keeping manifest.csv here"*
* **Brief Answer:** `sample_data/` and `outputs/` are mandatory under **Deliverable #2** of the assignment spec, so they must remain in the repository. However, duplicate copies (like the root `manifest.csv`) were redundant clutter, so we safely removed the root copy while preserving `sample_data/manifest.csv`.

#### Q20
> *"I want you to create a Mr. Tester folder containing all files that are just involved in testing and folders having files for testing, and put it in gitignore.... as our main assignment is not to show testing but to show actual demo of mailing filtering etc.."*
* **Brief Answer:** We moved all test runners, mock scripts, test fixtures, and edge-case suites into `Mr. Tester/` and added it to `.gitignore`. This maintains repository hygiene for production while preserving a complete 65-test verification harness locally.

#### Q21
> *"like we can tell right now consolidation feature is not that proper, and scanned pdfs arent analysed zip files arent seen onoly pdf csv xlsx files analysed and what limitation?"*
* **Brief Answer:** Documented as intentional operational trade-offs: consolidation is a point-in-time snapshot prototype (no historical XIRR); scanned PDFs fail closed (`no_text_layer_requires_ocr`) to prevent OCR digit hallucinations; and `.zip` attachments are quarantined to prevent zip-bomb / malware risks.

#### Q22
> *"i want to tell that even if i amusing a backup gemini key, llm call may not happen sometimes as gemini api keys both may not work"*  
> *"i also want to tell that quaratine review feature may not work somettiems so how to tell"*
* **Brief Answer:** Documented as fail-closed reliability: if both Gemini keys hit HTTP 429 rate limits, the system safely quarantines the statement with `learning_failed_llm_unavailable` without crashing; and Review Queue reprocessing is fail-closed, meaning unlocking a password or approving a layout won't force a document through if it has internal math errors or missing pages.


* **User Inquiry:**
  > *"when a bank changes layout(drift), what you do tell in simple words do you then store new format by calling llm for it, or send it for review"*
  * **Resolution:** The system flags it as `needs_review` under `layout_drift_known_provider`. It synthesizes a candidate `v2` template with `"approved": false` and holds it until a human operator audits the changes in the dashboard and clicks "Sign Off & Approve", preventing unverified layouts from running in production.
   **User Inquiry:**
  > *"suppose human hasn't approved it yet and again v2 came, so no llm would be called na"*
  * **Resolution:** Exactly—zero LLM calls are made. The candidate `v2` template is already registered as an unapproved candidate; subsequent statements matching that layout are immediately quarantined to `needs_review` awaiting the same human sign-off without re-triggering the LLM.

  * **User Inquiry:**
  > *"learning_failed_self_check when does it come ?"*
  * **Resolution:** It occurs during cold-start format learning when the LLM produces a syntactically valid JSON template, but when `StatementParser` tests it on the sample document, the extracted data fails financial auditing (e.g., zero holdings extracted, broken math, or missing client metadata). The candidate template is immediately discarded.

  * **User Inquiry:**
  > *"I am not liking that llm is given only starting 3 pages for parsing document table, don't you think it should be fixed?"*  
  > *"what if page 6 too needs to be taken"*  
  > *"our llm wants only one page na that has column headers?"*
  * **Resolution:** The first 3 pages slice (`pdf.pages[:3]`) is strictly an optimization for the **one-shot learning step** to keep prompt tokens low (<1,500) and latency under 3 seconds. Once the template is learned, the deterministic parser always iterates through **every single page** (`for page in pdf.pages:`) to extract 100% of holdings and transactions.

  * **User Inquiry:**
  > *"Opening Valuation+Inflows−Outflows+Gains−Fees==Closing Valuation is it always present in pms aif and what if it is not present"*
  * **Resolution:** Not all providers print an explicit net gain column (for example, Banyan quarterly statements). Our validator checks the template's `summary.has_gain` flag; if false, it does not enforce arithmetic roll-forward reconciliation, ensuring legitimate non-reconciling layouts pass safely without false alarms.

  * **User Inquiry:**
  > *"in .xlsx what if values are given in sheet1,sheet2 then sheet3. is it taken care or not while format extracting"*
  * **Resolution:** Yes. The openpyxl parser natively inspects all sheets in the workbook (Summary, Holdings, Transactions), identifying tables by their header row signatures regardless of which sheet tab they reside on.

  * **User Inquiry:**
  > *"i hope multi client data given in a single pdf statement is also handled separately so that later when in dashboard a particular client data is asked it is shown??"*
  * **Resolution:** Yes. When a multi-account statement arrives (like file `11_aurum_multi_client_2in1.pdf`), the engine unbundles it into separate `ExtractionResult` records keyed by `(client_id, strategy)`. In the dashboard, each client account appears as an independent portfolio card.

  * **User Inquiry:**
  > *"so on restart, parsing for all stored files is repeated ? or only stored processed final data is showed"*
  * **Resolution:** On restart, parsing is never repeated. Processed statements, metadata, and JSON artifacts are durably stored in PostgreSQL. On startup, the pipeline simply hydrates its in-memory hash cache from the database in milliseconds.

  * **User Inquiry:**
  > *"and tell about sql injection prevention"*  
  > *"what if attacker sends drop table directly"*
  * **Resolution:** All database queries in `persistence.py` use parameterized SQL queries with `%s` placeholders via `psycopg2`. Malicious inputs like `DROP TABLE` inside a client name or filename are treated strictly as raw string literals and can never execute as SQL commands.

  * **User Inquiry:**
  > *"what if multiple pdf attachments come in same email?"*
  * **Resolution:** The IMAP listener unbundles each attachment separately, sanitizing filenames with `os.path.basename` and prefixing them with a message hash `{msg_prefix}_{part_idx}_{filename}`. Each attachment is parsed independently and tied back to the email's event log.

  * **User Inquiry:**
  > *"is password code working fine, that if entered correct pass on dashboard for statement/non statement files thigns go fine"*  
  > *"what about llm calls for password protected files if one with same layout is stored already"*
  * **Resolution:** Yes. When unlocked in the dashboard, the engine decrypts the file in a secure temp directory, hashes the decrypted content, and matches it against existing templates. If the layout is already known, it parses with 0 LLM calls; if unseen, it triggers the one-shot learner.

  * **User Inquiry:**
  > *"when I click on reprocess review queue, it reprocesses, make new db id but even if validation goes correct it sends it to duplicate... why to create new db id for reprocess, either you erase and make new when reprocess is clicked i think"*
  * **Resolution:** We updated the reprocessor to perform an **in-place update** on the existing database row (`UPDATE statements SET status = ... WHERE id = ...`). This prevents generating new database IDs or triggering false duplicate self-collisions.

  *  **User Inquiry:**
  > *"why to show reapprove and force sync for a validated schema, you should do this for layout drift na?"*
  * **Resolution:** Force-syncing was redundant for already validated templates. We scoped the sign-off workflow exclusively to unapproved drifted templates (`_v2.json`) requiring human review.

   **User Inquiry:**
  > *"actually earlier for layout drift i clicked on sign off and approve and it got approved. but i want to say that after clicking on approve the option remains same as i am still getting option of sign off and approve and NEEDS SIGN OFF instead of ACTIVE PRODUCTION"*
  * **Resolution:** The UI session state was holding onto stale memory references. We added a persistent `promoted` flag to the template JSON, re-synced the fingerprinter registry, and triggered `st.rerun()`, so the badge immediately flips to `ACTIVE PRODUCTION`.

  * **User Inquiry:**
  > *"and dashboard is for managing the pms aif statements from these providers by companies like activv money"*
  * **Resolution:** Exactly. Activv Money is a Multi-Family Office / WealthTech platform. Their clients have wealth distributed across multiple wealth managers. The dashboard serves as an automated **Operations Command Center** that ingests statements, catches bad math, and unifies them into a single consolidated balance sheet.

  * **User Inquiry:**
  > *"is sample data...outputs... manifest.csv required here?? or they can be shifted to Mr. Tester too?"*  
  > *"and also tell me is there any sense of keeping manifest.csv here"*
  * **Resolution:** `sample_data/` and `outputs/` are mandatory under **Deliverable #2** of the assignment spec, so they must remain in the repository. However, duplicate copies (like the root `manifest.csv`) were redundant clutter, so we safely removed the root copy while preserving `sample_data/manifest.csv`.

  * **User Inquiry:**
  > *"I want you to create a Mr. Tester folder containing all files that are just involved in testing and folders having files for testing, and put it in gitignore.... as our main assignment is not to show testing but to show actual demo of mailing filtering etc.."*
  * **Resolution:** We moved all test runners, mock scripts, test fixtures, and edge-case suites into `Mr. Tester/` and added it to `.gitignore`. This maintains repository hygiene for production while preserving a complete 65-test verification harness locally.

  * **User Inquiry:**
  > *"like we can tell right now consolidation feature is not that proper, and scanned pdfs arent analysed zip files arent seen onoly pdf csv xlsx files analysed and what limitation?"*
  * **Resolution:** Documented as intentional operational trade-offs: consolidation is a point-in-time snapshot prototype (no historical XIRR); scanned PDFs fail closed (`no_text_layer_requires_ocr`) to prevent OCR digit hallucinations; and `.zip` attachments are quarantined to prevent zip-bomb / malware risks.
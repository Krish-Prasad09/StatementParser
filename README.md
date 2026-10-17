# 📈 StatementParser — AI-Powered PMS/AIF Statement Dashboard

> **🚀 Live Demo → [stparser.streamlit.app](https://stparser.streamlit.app/)**

> ⚠️ **Heads up:** I made this link public so interviewers can see the full project working — zero setup needed. That said, I know this isn't safe for a real app because the dashboard has a live "Send Email" tab hooked up to a dummy Gmail account. Don't do this for production. 😅

---

## 🤔 What does this project actually do?

Indian wealth managers (PMS/AIF funds) send monthly/quarterly PDF statements to investors. Each wealth manager uses their **own totally different PDF layout** — different columns, different fonts, different everything.

This project:
1. **Reads your Gmail inbox** for incoming statement emails 📬
2. **Figures out the PDF format** without you telling it anything
3. **If it's seen this format before** → extracts data instantly, zero AI calls 💨
4. **If it's a brand new format** → calls Gemini AI once, learns the layout, saves it, never calls AI again ✨
5. **Checks the math** — rollforward reconciliation, ISIN checksums, holdings vs NAV 🔢
6. **Consolidates everything** across all your wealth managers into one clean portfolio view 📊
7. **Saves history permanently** in a cloud PostgreSQL database so data never disappears ☁️

---

## ✨ Features

| Feature | What it means in plain English |
|---|---|
| 📬 **Email Ingestion** | Connects to Gmail via IMAP, auto-reads statement emails, ignores spam/marketing |
| 🧠 **One-Shot Learning** | Sees a new bank's PDF format → calls Gemini AI once → learns it forever |
| ⚡ **Zero AI Cost for Known Formats** | Already seen this bank before? Extracts data with 0 LLM calls, just rule-based parsing |
| 🔢 **Financial Safety Gate** | Checks: Opening + Inflows − Outflows + Gain − Fees = Closing (fails loudly if wrong) |
| 🔍 **ISIN Checksum Verification** | Validates stock codes using ISO 6166 Mod-10 Luhn algorithm — catches tampered data |
| 📐 **Layout Drift Detection** | If a bank renames a column, it quarantines the file instead of guessing wrong |
| 🔐 **Password-Protected PDFs** | Locks encrypted PDFs in quarantine; enter the password in the dashboard to unlock |
| 🏦 **Multi-Custodian Consolidation** | Merges holdings from all your wealth managers by ISIN into one portfolio view |
| ☁️ **Cloud Database Persistence** | PostgreSQL (Neon.tech / Supabase / RDS) for robust, permanent cloud storage |
| 📊 **Interactive Dashboard** | Streamlit UI with 4 views: Mailbox Ops, Portfolio View, Quarantine Queue, Send Test Email |
| 🧪 **31 Automated Tests** | 1,046 ground-truth fields verified — zero false passes allowed |

---

## ☁️ Cloud Persistence: Neon PostgreSQL

Streamlit Community Cloud runs apps inside **ephemeral (temporary) containers**. When an app is idle for a few hours, the cloud server goes to sleep, and any container local disk gets wiped on the next wake-up.

To solve this in an enterprise-ready way:
* **Production & Persistent Deployment:** Backed by **[Neon.tech](https://neon.tech) Serverless PostgreSQL**. Every statement, parsed ledger, template, and email event is permanently saved in PostgreSQL. History and quarantine records are preserved across app restarts, deployments, and reboots.

---

## 🚀 Try it Live

👉 **[https://stparser.streamlit.app/](https://stparser.streamlit.app/)**

The live app has 26 sample PDF statements and 5 sample emails pre-loaded. You can:
- Upload any statement file and dispatch it via real SMTP to the test inbox
- Watch it get polled via IMAP, parsed, audited, and added to the portfolio
- Browse the quarantine queue and unlock password-protected statements
- See the consolidated portfolio view across multiple accounts
- Reset templates to cold-start mode or reset database records with one click

> **⚠️ Security Note:** The live deployment uses a real (dummy) Gmail account with live IMAP/SMTP. Anyone with the link can trigger emails from that account. For a real production system, this should be behind auth. I'm only doing this so you can see the full end-to-end flow without any setup on your end.

---

## 🛠️ Run Locally

### Prerequisites
- Python 3.10+
- A Gmail account with IMAP enabled (or any IMAP mailbox)
- A [Google Gemini API key](https://aistudio.google.com/) (free tier works fine)
- A [Neon.tech](https://neon.tech) PostgreSQL database (or any PostgreSQL connection string)

### 1️⃣ Clone the repo
```bash
git clone https://github.com/Krish-Prasad09/StatementParser.git
cd StatementParser
```

### 2️⃣ Install dependencies
```bash
pip install -r requirements.txt
```

### 3️⃣ Set up your `.env` file
```bash
cp .env.example .env
```
Then open `.env` and fill in:
```env
GEMINI_API_KEY=your_gemini_api_key_here

IMAP_HOST=imap.gmail.com
IMAP_PORT=993
IMAP_USER=your_email@gmail.com
IMAP_PASSWORD=your_app_password_here
IMAP_FOLDER=INBOX

# Neon PostgreSQL connection string
DATABASE_URL=postgresql://user:pass@ep-cool-xyz.us-east-2.aws.neon.tech/neondb?sslmode=require
```
> 💡 For Gmail, you need an [App Password](https://myaccount.google.com/apppasswords), not your regular password.

### 4️⃣ Run the dashboard
```bash
streamlit run app.py
```
Open [http://localhost:8501](http://localhost:8501) in your browser. That's it!

### 5️⃣ Run the test suite (Local Harness)
```bash
python "Mr. Tester/run_tests.py"
```
Should print `ALL TESTS (BENCHMARK 31/31 + EDGE CASES 34/34) PASSED WITH 100% SUCCESS!` ✅

---

## 📁 Project Structure

```
StatementParser/
├── app.py               # 🖥️  Streamlit dashboard (the UI you interact with)
├── pipeline.py          # 🔄  End-to-end orchestration — glues everything together
├── ingestion.py         # 📥  Reads emails, extracts PDF attachments, deduplicates
├── fingerprinter.py     # 🔍  Identifies which bank/format a PDF belongs to
├── learner.py           # 🧠  Calls Gemini AI to learn new formats (once per format)
├── parser.py            # ⚙️  Actually extracts data from PDFs using learned templates
├── validator.py         # 🔢  Audits the math (14 different financial checks)
├── consolidator.py      # 🏦  Merges multi-custodian holdings by ISIN
├── persistence.py       # 💾  Enterprise PostgreSQL store (Neon Cloud / Supabase / RDS)
├── watch_mailbox.py     # 📬  IMAP poller — watches inbox for new statement emails
├── mailer.py            # 📤  SMTP Email Dispatcher for live statements & demo tests
├── models.py            # 📐  Data schemas (what a "statement" looks like in code)
├── requirements.txt     # 📦  All Python dependencies (including psycopg2)
├── templates/           # 🗂️  Where learned PDF templates are saved (starts empty!)
├── sample_data/         # 📄  26 sample PDFs + 5 sample emails + manifest.csv
├── outputs/             # 📊  Extracted JSON portfolio outputs for sample statements
└── docs/                # 📖  Design notes and AI-usage documentation
```

> 💡 `templates/` starts **empty** on purpose. The first time you run a new format, the AI learns it and saves a template here. After that, it never calls the AI again for that format.

---

## 🧪 Benchmark Results

```
✅ 31/31 tests passed (100.0%)
📊 1,046 ground-truth fields reconciled
🏦 9 different statement formats covered
🔒 Encrypted PDFs, tampered numbers, broken rollforwards — all correctly quarantined
```

Formats tested:
- Aurum Capital PMS (2 layout versions)
- Banyan Tree Advisors PMS
- Cedar Capital PMS
- Delta Ridge Securities PMS
- Evergreen Alternatives AIF Cat II
- Fulcrum Asset Management AIF Cat III
- Hestia Capital Partners (CSV + Excel formats)

---

## 📬 Email Integration

The project connects to your Gmail inbox using IMAP. Here's the flow:

```
Gmail Inbox
    ↓
watch_mailbox.py polls for new emails
    ↓
ingestion.py extracts PDF attachments
    ↓
fingerprinter.py identifies the format
    ↓ (if new format)        ↓ (if known format)
learner.py calls Gemini    parser.py reads directly
saves template to disk     0 AI calls, instant parse
    ↓                           ↓
validator.py checks the math (14 checks)
    ↓
persistence.py saves to Cloud PostgreSQL
    ↓
Dashboard shows results
```

---

## 🔧 Environment Variables

| Variable | What it's for |
|---|---|
| `GEMINI_API_KEY` | Gemini AI key — used once per new format to learn the layout |
| `IMAP_HOST` / `IMAP_PORT` | Your email server (e.g. `imap.gmail.com` / `993`) |
| `IMAP_USER` / `IMAP_PASSWORD` | Your email login credentials |
| `IMAP_FOLDER` | Which inbox folder to watch (default: `INBOX`) |
| `DATABASE_URL` | PostgreSQL connection URL (e.g. Neon.tech, Supabase, AWS RDS) |

---

## 🏗️ Built With

- **[Streamlit](https://streamlit.io/)** — interactive dashboard UI
- **[Neon](https://neon.tech/)** & **[PostgreSQL](https://www.postgresql.org/)** — serverless cloud database for permanent persistence
- **[pdfplumber](https://github.com/jsvine/pdfplumber)** — PDF table extraction
- **[pypdf](https://github.com/py-pdf/pypdf)** — PDF decryption
- **[pandas](https://pandas.pydata.org/)** — data processing
- **[pydantic](https://docs.pydantic.dev/)** — data validation and schemas
- **[Google Gemini API](https://aistudio.google.com/)** — one-shot format learning

---

## 📝 License

Built for the **Activv Money Internship Assignment** (Newcamp Finserv).

---

*Made with ☕ and way too many PDF edge cases*

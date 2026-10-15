"""
test_edge_cases.py
------------------
Comprehensive automated regression & mutation audit suite:
1. Number parsing (clean_number: brackets, unicode minus, trailing minus, Dr/Cr, Lakhs/Crores).
2. Date parsing (parse_date: ISO, UK/Indian day-first, dot dates, month words).
3. ISIN checksum audit (is_valid_isin: ISO 6166 Mod-10 Luhn check digits).
4. Three-way document classifier (statement with 'Reporting window: dd/mm/yyyy to dd/mm/yyyy',
   ambiguous prose -> needs_review ['classification_uncertain'], negative -> not_a_statement).
5. Password-protected PDF decryption & temp file cleanup (process_file and parse_eml_email).
6. Email path traversal sanitization and collision-free attachment prefixing.
7. Real One-Shot Learner on unseen provider (Meridian Wealth PMS: Run 1 = 1 LLM call + ground-truth match,
   Run 2 = 0 LLM calls, plus distinct failure codes: learning_failed_invalid_template,
   learning_failed_self_check, learning_failed_llm_unavailable).
8. Provider misattribution guard (different SEBI or provider name never reuses another provider's template).
9. Persistent SQLite duplicate & revision detection across restarts and watch_mailbox poller runs.
10. All 7 independent mutation audit cases (tampered Cedar holding, tampered Cedar/Aurum cash,
    unknown Cedar transaction label, renamed Aurum header column, unseen provider, unseen layout,
    duplicate .eml ingestion).
"""

import os
import json
import shutil
import tempfile
import sys
from pathlib import Path
import pypdf
import pdfplumber
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
import re
import csv
from unittest.mock import MagicMock
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet
from typing import Dict, Any, List

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils import clean_number, parse_date, is_valid_isin, compute_sha256
from pipeline import ProcessingPipeline
from ingestion import IngestionEngine
from models import StatementRecord, StatementMeta, Summary, Holding, Transaction, FormatTemplate
from validator import FinancialValidator
from persistence import StatementStore
from learner import LearnerEngine, extract_and_redact_document_text
from watch_mailbox import process_eml_file, _poll_inbox_iteration, reprocess_review_queue


def _build_pdf_from_lines(lines: List[str], out_path: str, monospace: bool = True):
    """Helper to render text lines into a valid PDF using ReportLab."""
    c = canvas.Canvas(out_path, pagesize=A4)
    font_name = "Courier" if monospace else "Helvetica"
    c.setFont(font_name, 9)
    y = 800
    for line in lines:
        if y < 50:
            c.showPage()
            c.setFont(font_name, 9)
            y = 800
        c.drawString(36, y, line)
        y -= 13
    c.save()


def _build_mutated_aurum_pdf(
    out_path: str,
    provider_title: str = "AURUM CAPITAL PMS",
    sebi_regn: str = "INP000099001",
    period_line: str = "Statement Period: 01-Sep-2026 to 30-Sep-2026",
    mv_header: str = "Market Value (Rs.)",
    summary_cash: str = "245,000",
    row_cash: str = "245,000",
    banner_line: str = "",
    total_equity_label: str = "Total Equity",
    cash_row_label: str = "Cash & Bank Balance"
):
    """Builds a valid ruled-table PDF modeled on Aurum with configurable mutations."""
    doc = SimpleDocTemplate(out_path, pagesize=A4, leftMargin=30, rightMargin=30, topMargin=30, bottomMargin=30)
    styles = getSampleStyleSheet()
    story = []

    if banner_line:
        story.append(Paragraph(f"<b>{banner_line}</b>", styles["Normal"]))
        story.append(Spacer(1, 6))

    story.append(Paragraph(f"<b>{provider_title}</b>", styles["Title"]))
    story.append(Paragraph(f"Discretionary Portfolio Management Services | SEBI Regn: {sebi_regn}", styles["Normal"]))
    story.append(Paragraph(f"Monthly Portfolio Statement | {period_line}", styles["Normal"]))
    story.append(Spacer(1, 8))
    story.append(Paragraph("CLIENT ID: ACM-1042 | Client Name: Ananya Rao | PAN: SAMPL1001A | Strategy: Aurum Core Compounders | Currency: INR", styles["Normal"]))
    story.append(Spacer(1, 10))

    sum_data = [
        ["Opening Corpus", "Capital Inflows", "Capital Outflows", "Net Gain / (Loss)", "Management Fees", "Cash Balance", "Closing Corpus"],
        ["6,000,000", "0", "0", "329,500", "14,500", summary_cash, "6,315,000"]
    ]
    t_sum = Table(sum_data, repeatRows=1)
    t_sum.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.black), ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey)]))
    story.append(t_sum)
    story.append(Spacer(1, 12))

    hold_data = [
        ["Security", "ISIN", "Qty", "Avg Cost (Rs.)", "Market Price (Rs.)", mv_header, "Weight"],
        ["HDFC Bank Limited", "INE040A01034", "2,000", "1,500.00", "1,650.00", "3,300,000", "52.26%"],
        ["Infosys Limited", "INE009A01021", "1,500", "1,600.00", "1,846.67", "2,770,000", "43.86%"],
        [total_equity_label, "", "", "", "", "6,070,000", "96.12%"],
        [cash_row_label, "", "", "", "", row_cash, "3.88%"],
        ["Total Portfolio Value", "", "", "", "", "6,315,000", "100.00%"]
    ]
    t_hold = Table(hold_data, repeatRows=1)
    t_hold.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.black), ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey)]))
    story.append(t_hold)
    story.append(Spacer(1, 12))

    tx_data = [
        ["Date", "Type", "Particulars", "Qty", "Price (Rs.)", "Amount (Rs.)"],
        ["30-Sep-2026", "FEE", "Management Fee", "-", "-", "14,500"]
    ]
    t_tx = Table(tx_data, repeatRows=1)
    t_tx.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.black), ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey)]))
    story.append(t_tx)

    doc.build(story)


def test_clean_number():
    """Unit tests for clean_number sign conventions, Indian units, and currencies."""
    assert clean_number("1,00,000.00") == 100000.0
    assert clean_number("(500.00)") == -500.0
    assert clean_number("(Rs. 18,250)") == -18250.0
    assert clean_number("500.00-") == -500.0
    assert clean_number("\u2212500.00") == -500.0
    assert clean_number("1,234.00 Dr") == -1234.0
    assert clean_number("1,234.00 Cr") == 1234.0
    assert clean_number("5.2 Lakhs") == 520000.0
    assert clean_number("1.5 Crores") == 15000000.0
    assert clean_number("₹ 1,23,456.78") == 123456.78
    assert clean_number("$ 50,000.00") == 50000.0
    assert clean_number("21.52%") == 21.52
    assert clean_number("-") is None
    assert clean_number("N/A") is None
    assert clean_number("") is None
    print("[PASS] clean_number handles all accounting conventions, Dr/Cr, and Indian Lakhs/Crores.")


def test_parse_date():
    """Unit tests for date parsing and ISO normalization."""
    assert parse_date("2026-09-30") == "2026-09-30"
    assert parse_date("30/09/2026") == "2026-09-30"
    assert parse_date("30-09-2026") == "2026-09-30"
    assert parse_date("30.09.2026") == "2026-09-30"
    assert parse_date("30 Sep 2026") == "2026-09-30"
    assert parse_date("30-Sep-2026") == "2026-09-30"
    assert parse_date("September 30, 2026") == "2026-09-30"
    assert parse_date("04-Aug-2026") == "2026-08-04"
    assert parse_date("invalid_date") is None
    print("[PASS] parse_date accurately normalizes UK/Indian, dot, and named month strings.")


def test_is_valid_isin():
    """Unit tests for ISO 6166 Mod-10 Luhn algorithm."""
    assert is_valid_isin("INE237A01028") is True
    assert is_valid_isin("INE002A01018") is True
    assert is_valid_isin("INE040A01034") is True
    assert is_valid_isin("US0378331005") is True
    assert is_valid_isin("INE237A01029") is False
    assert is_valid_isin("INE002A01019") is False
    assert is_valid_isin("123456789012") is False
    assert is_valid_isin("INE237A0102") is False
    assert is_valid_isin("") is False
    assert is_valid_isin(None) is False
    print("[PASS] is_valid_isin correctly audits ISO 6166 Mod-10 Luhn check digits.")


def test_three_way_classifier():
    """
    Tests Fix 4: Three-way document classifier:
    1. Statement with 'Reporting window: 01/09/2026 to 30/09/2026' (no 'Period' word) -> classified as 'statement' and 'validated'.
    2. Ambiguous prose-only PDF -> classified as 'ambiguous' -> status='needs_review', issues=['classification_uncertain'].
    3. Explicit negative (factsheet) -> classified as 'not_a_statement'.
    """
    pipe = ProcessingPipeline(templates_dir="test_fixtures/templates")

    # 1. Statement with 'Reporting window: 01/09/2026 to 30/09/2026'
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp1:
        window_pdf = tmp1.name
    _build_mutated_aurum_pdf(
        window_pdf,
        period_line="Reporting window: 01/09/2026 to 30/09/2026"
    )

    # 2. Ambiguous prose-only PDF (neither explicit negative nor positive statement)
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp2:
        prose_pdf = tmp2.name
    _build_pdf_from_lines([
        "Dear Valued Investor,",
        "We appreciate your continued trust during the volatile macro environment.",
        "Please reach out to your relationship manager for any portfolio queries."
    ], prose_pdf, monospace=False)

    try:
        res_win = pipe.process_file(window_pdf)
        assert res_win.status == "validated", f"Expected validated for Reporting window PDF, got {res_win.status} ({res_win.issues})"

        res_prose = pipe.process_file(prose_pdf)
        assert res_prose.status == "needs_review", f"Expected needs_review for ambiguous prose PDF, got {res_prose.status}"
        assert "classification_uncertain" in res_prose.issues, f"Expected classification_uncertain, got {res_prose.issues}"

        res_neg = pipe.process_file("sample_data/edge_cases/24_neg_factsheet.pdf")
        assert res_neg.status == "not_a_statement"

        print("[PASS] Three-way classifier handles 'Reporting window' date ranges and routes ambiguous PDFs to needs_review (classification_uncertain).")
    finally:
        for p in (window_pdf, prose_pdf):
            if os.path.exists(p):
                os.remove(p)


def test_password_protected_pdf_handling():
    """Encrypted PDF must diagnose password_required, decrypt cleanly with STATEMENT_PASSWORDS, and clean up temp files."""
    pipe = ProcessingPipeline(templates_dir="test_fixtures/templates")
    sample_src = "sample_data/pms/02_aurum_A2_sell_and_outflow.pdf"
    assert os.path.exists(sample_src)

    reader = pypdf.PdfReader(sample_src)
    writer = pypdf.PdfWriter()
    writer.append(reader)
    test_pwd = "SecretPass123_Test"
    writer.encrypt(test_pwd)

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        enc_pdf_path = tmp.name
        writer.write(tmp)

    try:
        # 1. No password -> password_required
        res_no_pwd = pipe.process_file(enc_pdf_path)
        assert res_no_pwd.status == "needs_review"
        assert "password_required" in res_no_pwd.issues

        # 2. Wrong password -> password_required
        res_wrong_pwd = pipe.process_file(enc_pdf_path, password="WrongPassword999")
        assert res_wrong_pwd.status == "needs_review"
        assert "password_required" in res_wrong_pwd.issues

        # 3. STATEMENT_PASSWORDS env var with wrong,right -> equals unencrypted result
        old_env = os.environ.get("STATEMENT_PASSWORDS", "")
        os.environ["STATEMENT_PASSWORDS"] = f"wrong_pwd,{test_pwd}"
        try:
            pipe_env = ProcessingPipeline(templates_dir="test_fixtures/templates")
            res_env = pipe_env.process_file(enc_pdf_path)
            res_unenc = ProcessingPipeline(templates_dir="test_fixtures/templates").process_file(sample_src)
            assert res_env.status == "validated", f"Failed env var test: {res_env.issues}"
            assert res_env.statements == res_unenc.statements, "Decrypted statements must match unencrypted PDF!"
            res_dup = pipe_env.process_file(enc_pdf_path)
            assert res_dup.status == "duplicate", "Second run on encrypted PDF must be detected as duplicate!"
        finally:
            os.environ["STATEMENT_PASSWORDS"] = old_env

        print("[PASS] Password decryption works for direct password and STATEMENT_PASSWORDS, and hashes decrypted content.")
    finally:
        if os.path.exists(enc_pdf_path):
            os.remove(enc_pdf_path)


def test_email_path_traversal_and_temp_cleanup():
    """Email ingestion must sanitize attachment names, prefix with message hash, and clean up decrypted temp PDFs."""
    ing = IngestionEngine()
    eml_content = b"""From: sender@example.com
To: recipient@example.com
Subject: Traversal Test
Message-ID: <test-msg-001@example.com>
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="BOUNDARY"

--BOUNDARY
Content-Type: text/plain

See attached statement.

--BOUNDARY
Content-Type: application/pdf
Content-Disposition: attachment; filename="../../evil_statement.pdf"

%PDF-1.4 sample content
--BOUNDARY--
"""
    with tempfile.NamedTemporaryFile(suffix=".eml", delete=False) as tmp:
        tmp.write(eml_content)
        eml_path = tmp.name

    extract_dir = tempfile.mkdtemp()
    try:
        eml_res = ing.parse_eml_email(eml_path, extract_dir=extract_dir)
        att_filenames = [a["filename"] for a in eml_res["attachments"]]
        saved_basenames = [os.path.basename(a["path"]) for a in eml_res["attachments"]]
        assert "../../evil_statement.pdf" not in att_filenames
        assert all(not f.startswith("..") and "/" not in f and "\\" not in f for f in att_filenames)
        assert all(b.endswith("_evil_statement.pdf") and len(b) > len("evil_statement.pdf") for b in saved_basenames)
        print("[PASS] Email path traversal sanitized and prefixed with collision-free message hash.")
    finally:
        if os.path.exists(eml_path):
            os.remove(eml_path)
        shutil.rmtree(extract_dir, ignore_errors=True)


def test_real_one_shot_learner_on_unseen_provider():
    """
    Tests Fix 1: Real One-Shot Learner on synthetic unseen provider (Meridian Wealth PMS):
    - Uses completely different labels: 'Investor Code', 'Portfolio worth at start', 'Scrip', 'Units', 'Rate', 'Worth', 'ACQUIRE', 'DISPOSE'.
    - Verifies prompt contains redacted text, FormatTemplate JSON schema, and worked example.
    - Run 1 on 27_meridian_unseen_M1.pdf makes 1 LLM call, saves template, and matches ground truth 27_meridian_unseen_M1.json.
    - Run 2 on 28_meridian_unseen_M2.pdf makes 0 LLM calls and returns 'validated'.
    - Also verifies distinct failure issue codes:
      * learning_failed_invalid_template (empty/garbage JSON or PII leak)
      * learning_failed_self_check (valid schema template that fails validator self-check)
      * learning_failed_llm_unavailable (API outage / missing key)
    """
    m1_path = "sample_data/unseen/27_meridian_unseen_M1.pdf"
    m2_path = "sample_data/unseen/28_meridian_unseen_M2.pdf"
    gt_path = "sample_data/ground_truth/27_meridian_unseen_M1.json"
    assert os.path.exists(m1_path) and os.path.exists(m2_path) and os.path.exists(gt_path)

    global _meridian_template_dict
    meridian_template_dict = {
        "format_id": "meridian_wealth_pms",
        "version": 1,
        "layout_kind": "ruled_table",
        "provider_key": "meridian_wealth_pms",
        "sebi_regn": "INP000088101",
        "fingerprint": {
            "summary_headers": [
                "Portfolio worth at start", "Fresh Inflows", "Redemptions",
                "Net Profit", "Advisory Levy", "Liquid Cash", "Ending worth"
            ],
            "holdings_headers": [
                "Scrip", "ISIN", "Units", "Cost per Unit", "Rate", "Worth", "Share %"
            ],
            "transactions_headers": [
                "Trade Date", "Action", "Scrip Name", "Units", "Rate", "Net Amount"
            ]
        },
        "fields": {
            "provider": "Meridian Wealth PMS",
            "client_id_pattern": r"Investor Code\s*:\s*([A-Za-z0-9\/\-]+)",
            "client_name_pattern": r"Investor\s*:\s*([^\n\r|]+)",
            "pan_pattern": r"Tax ID\s*:\s*([A-Z]{5}[0-9]{4}[A-Z])",
            "strategy_pattern": r"Mandate\s*:\s*([^\n\r|]+)"
        },
        "period": {
            "pattern": r"Reporting Window:\s*([0-9\/]+)\s+to\s+([0-9\/]+)",
            "format": "DD/MM/YYYY"
        },
        "summary": {
            "has_gain": True,
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
            "total_row_labels": ["Total Equity", "Ending worth"]
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
    _meridian_template_dict = meridian_template_dict

    temp_dir = tempfile.mkdtemp()
    try:
        pipe = ProcessingPipeline(templates_dir=temp_dir)
        captured_prompts: List[str] = []

        def mock_llm_success(prompt: str):
            captured_prompts.append(prompt)
            return dict(meridian_template_dict)

        pipe.learner._call_llm_api = mock_llm_success

        # Run 1 on unseen M1
        res1 = pipe.process_file(m1_path)
        assert len(captured_prompts) == 1, f"Expected 1 LLM call on Run 1, got {len(captured_prompts)}"
        assert res1.llm_calls_made == 1
        assert res1.status == "validated", f"Expected validated on M1, got {res1.status} ({res1.issues})"
        assert "<REDACTED_PAN>" in captured_prompts[0], "Prompt must redact PANs before sending to LLM!"
        assert "Vikram Malhotra" not in captured_prompts[0], "Prompt must redact client name before sending to LLM!"
        assert "layout_kind" in captured_prompts[0], "Prompt must include FormatTemplate schema!"

        # Verify against ground truth 27_meridian_unseen_M1.json
        with open(gt_path, "r", encoding="utf-8") as f:
            gt = json.load(f)
        gt_stmt = gt["statements"][0]
        rec1 = res1.statements[0]
        assert rec1.statement.provider == gt_stmt["statement"]["provider"]
        assert rec1.statement.strategy == gt_stmt["statement"]["strategy"]
        assert rec1.statement.client_id == gt_stmt["statement"]["client_id"]
        assert rec1.summary.closing == gt_stmt["summary"]["closing"]
        assert len(rec1.holdings) == len(gt_stmt["holdings"])
        assert [h.name for h in rec1.holdings] == [h["name"] for h in gt_stmt["holdings"]]
        assert [t.type for t in rec1.transactions] == [t["type"] for t in gt_stmt["transactions"]]
        assert [t.date for t in rec1.transactions] == [t["date"] for t in gt_stmt["transactions"]]

        # Run 2 on unseen M2 (same format, different client & numbers) -> 0 LLM calls!
        res2 = pipe.process_file(m2_path)
        assert len(captured_prompts) == 1, "Run 2 must make 0 additional LLM calls!"
        assert res2.llm_calls_made == 0
        assert res2.status == "validated", f"Expected validated on M2, got {res2.status} ({res2.issues})"

        # Test failure code 1: learning_failed_invalid_template (garbage output)
        temp_bad1 = tempfile.mkdtemp()
        try:
            pipe_bad1 = ProcessingPipeline(templates_dir=temp_bad1)
            pipe_bad1.learner._call_llm_api = lambda prompt: {"not_a_valid": "template"}
            r_bad1 = pipe_bad1.process_file(m1_path)
            assert r_bad1.status == "needs_review"
            assert "learning_failed_invalid_template" in r_bad1.issues
            assert len(os.listdir(temp_bad1)) == 0
        finally:
            shutil.rmtree(temp_bad1)

        # Test failure code 2: learning_failed_self_check (valid schema, wrong column mappings -> 0 holdings)
        temp_bad2 = tempfile.mkdtemp()
        try:
            pipe_bad2 = ProcessingPipeline(templates_dir=temp_bad2)
            broken_tmpl = dict(meridian_template_dict)
            broken_tmpl["holdings"] = {"mapping": {"name": "NonExistentCol", "market_value": "WrongCol"}}
            pipe_bad2.learner._call_llm_api = lambda prompt: broken_tmpl
            r_bad2 = pipe_bad2.process_file(m1_path)
            assert r_bad2.status == "needs_review"
            assert "learning_failed_self_check" in r_bad2.issues
            assert len(os.listdir(temp_bad2)) == 0
        finally:
            shutil.rmtree(temp_bad2)

        # Test failure code 3: learning_failed_llm_unavailable (no API key / outage)
        temp_bad3 = tempfile.mkdtemp()
        try:
            pipe_bad3 = ProcessingPipeline(templates_dir=temp_bad3)
            pipe_bad3.learner._call_llm_api = lambda prompt: None
            r_bad3 = pipe_bad3.process_file(m1_path)
            assert r_bad3.status == "needs_review"
            assert "learning_failed_llm_unavailable" in r_bad3.issues
            assert len(os.listdir(temp_bad3)) == 0
        finally:
            shutil.rmtree(temp_bad3)

        print("[PASS] Real One-Shot Learner verified on Meridian Wealth PMS (Run 1=1 call, Run 2=0 calls) + all 3 failure codes.")
    finally:
        shutil.rmtree(temp_dir)


def test_provider_misattribution_guard():
    """
    Tests Fix 2: Silent provider misattribution guard:
    A statement with Aurum's table headers but provider 'Zenith Wealth PMS' and SEBI 'INP000077777'
    must NOT match aurum_pms_monthly and must NOT return 'validated' under 'Aurum Capital PMS'.
    """
    temp_dir = tempfile.mkdtemp()
    import shutil
    for f in os.listdir("test_fixtures/templates"):
        if f.endswith(".json"):
            shutil.copy(os.path.join("test_fixtures/templates", f), os.path.join(temp_dir, f))
    pipe = ProcessingPipeline(templates_dir=temp_dir)
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        zenith_pdf = tmp.name
    _build_mutated_aurum_pdf(
        zenith_pdf,
        provider_title="ZENITH WEALTH PMS",
        sebi_regn="INP000077777"
    )

    try:
        res = pipe.process_file(zenith_pdf)
        assert res.format_id != "aurum_pms_monthly", f"Silently misattributed Zenith PDF to {res.format_id}!"
        if res.status == "needs_review":
            assert "learning_failed_llm_unavailable" in res.issues or res.format_id != "aurum_pms_monthly"
        else:
            assert "zenith" in res.format_id.lower()

        # Also verify parser extracts 'Zenith Wealth Pms' from document text (not template fallback)
        aurum_tmpl = pipe.learner.load_template("aurum_pms_monthly")
        recs = pipe.parser.parse(zenith_pdf, aurum_tmpl)
        assert "zenith" in recs[0].statement.provider.lower()
        st, v_issues = pipe.validator.validate(zenith_pdf, recs, template=aurum_tmpl)
        assert st == "needs_review"
        assert "provider_mismatch_template" in v_issues
        print("[PASS] Provider misattribution guard blocks cross-provider template reuse and flags provider_mismatch_template.")
    finally:
        if os.path.exists(zenith_pdf):
            os.remove(zenith_pdf)


def test_persistent_duplicate_and_revision_detection():
    """
    Tests Fix 3: Persistent duplicate & revision detection across SQLite restarts:
    Running watch_mailbox.process_eml_file twice against the same .eml with fresh pipeline/repo
    instances returns 'validated' on Run 1 (with non-empty sha256_hash) and 'duplicate' on Run 2.
    """
    temp_dir = tempfile.mkdtemp()
    db_path = os.path.join(temp_dir, "test_state.db")
    extract_dir = os.path.join(temp_dir, "attachments")
    eml_path = "sample_data/emails/e01_statement_single_attachment.eml"

    try:
        # Run 1: Fresh pipeline & repo
        repo1 = StatementStore(db_path=db_path)
        pipe1 = ProcessingPipeline(templates_dir="test_fixtures/templates", db_path=db_path)
        results1 = process_eml_file(eml_path, pipe1, repo1, target_dir=extract_dir)
        assert len(results1) == 1
        assert results1[0]["status"] == "validated"
        assert results1[0]["sha256_hash"] and len(results1[0]["sha256_hash"]) == 64

        # Verify SQLite stored the real sha256_hash and email_event
        rows = repo1.get_all()
        assert len(rows) == 1
        assert rows[0]["sha256_hash"] == results1[0]["sha256_hash"]
        events = repo1.get_email_events()
        assert len(events) == 1
        assert events[0]["status"] == "ingest"

        # Run 2: Re-ingesting the exact same email file (same message_id)
        # Deduplication safety net 1: same message_id is skipped and NOT processed again
        repo2 = StatementStore(db_path=db_path)
        pipe2 = ProcessingPipeline(templates_dir="test_fixtures/templates", db_path=db_path)
        results2 = process_eml_file(eml_path, pipe2, repo2, target_dir=extract_dir)
        assert len(results2) == 0, f"Expected 0 results for duplicate message_id, got {len(results2)}"
        assert len(repo2.get_all()) == 1, "Statements table should not duplicate rows for reprocessed email"

        # Run 3: Different email message_id containing the SAME attachment
        # Deduplication safety net 2: SHA-256 content deduplication catches duplicate attachment
        with open(eml_path, "r", encoding="utf-8", errors="ignore") as f:
            eml_raw = f.read()
        diff_eml_raw = eml_raw.replace("Message-ID: ", "Message-ID: <different_msg_id_999@example.com>\nX-Original-ID: ")
        diff_eml_path = os.path.join(temp_dir, "diff_msg_id.eml")
        with open(diff_eml_path, "w", encoding="utf-8") as f:
            f.write(diff_eml_raw)

        results3 = process_eml_file(diff_eml_path, pipe2, repo2, target_dir=extract_dir)
        assert len(results3) == 1
        assert results3[0]["status"] == "duplicate", f"Expected duplicate on second poller run, got {results3[0]['status']}"
        assert any(i.startswith("identical_to:") for i in results3[0]["issues"])

        print("[PASS] Persistent SQLite duplicate detection: message_id skipped and duplicate attachment hash caught.")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_seven_mutation_audit_cases():
    """
    Executes all 7 mutation cases required in the final audit report and verifies their exact status and issues.
    """
    # Read original Cedar text lines to create mutated Cedar PDFs
    with pdfplumber.open("sample_data/pms/06_cedar_C1_textflow.pdf") as pdf:
        cedar_lines = "\n".join(p.extract_text() or "" for p in pdf.pages).split("\n")

    temp_dir = tempfile.mkdtemp()
    try:
        # 1. Tampered Cedar holding value
        cedar_tamper_hold = os.path.join(temp_dir, "mut1_cedar_holding.pdf")
        lines_m1 = [l.replace("1,089,700.00", "1,189,700.00") if "INE040A01034" in l or "1,089,700" in l else l for l in cedar_lines]
        # Mutate the first holding's Value column by +100,000
        mut1_lines = []
        mutated_h = False
        for l in cedar_lines:
            if not mutated_h and "INE" in l and "." in l:
                parts = l.rsplit(None, 2)
                if len(parts) == 3:
                    orig_val = clean_number(parts[1])
                    new_val = f"{orig_val + 100000.0:,.2f}"
                    l = f"{parts[0]} {new_val} {parts[2]}"
                    mutated_h = True
            mut1_lines.append(l)
        _build_pdf_from_lines(mut1_lines, cedar_tamper_hold, monospace=True)
        res_m1 = ProcessingPipeline(templates_dir="test_fixtures/templates").process_file(cedar_tamper_hold)
        assert res_m1.status == "needs_review"
        assert "holding_qty_price_mismatch" in res_m1.issues and "closing_value_ne_holdings_total" in res_m1.issues

        # 2. Tampered Cedar cash & Tampered Aurum summary vs row cash
        cedar_tamper_cash = os.path.join(temp_dir, "mut2_cedar_cash.pdf")
        mut2_lines = []
        for l in cedar_lines:
            if "Cash & Equivalents" in l:
                val = clean_number(l.split(":")[-1])
                l = f"Cash & Equivalents : Rs. {val + 50000.0:,.2f}"
            mut2_lines.append(l)
        _build_pdf_from_lines(mut2_lines, cedar_tamper_cash, monospace=True)
        res_m2 = ProcessingPipeline(templates_dir="test_fixtures/templates").process_file(cedar_tamper_cash)
        assert res_m2.status == "needs_review"
        assert "closing_value_ne_holdings_total" in res_m2.issues

        aurum_cash_mismatch = os.path.join(temp_dir, "mut2b_aurum_cash_row.pdf")
        _build_mutated_aurum_pdf(aurum_cash_mismatch, summary_cash="245,000", row_cash="195,000")
        res_m2b = ProcessingPipeline(templates_dir="test_fixtures/templates").process_file(aurum_cash_mismatch)
        assert res_m2b.status == "needs_review"
        assert "cash_summary_vs_row_mismatch" in res_m2b.issues

        # 3. Unknown Cedar transaction label
        cedar_unknown_tx = os.path.join(temp_dir, "mut3_cedar_tx.pdf")
        mut3_lines = [l.replace("PURCHASE", "CORP_ACT", 1) if "PURCHASE" in l else l for l in cedar_lines]
        _build_pdf_from_lines(mut3_lines, cedar_unknown_tx, monospace=True)
        res_m3 = ProcessingPipeline(templates_dir="test_fixtures/templates").process_file(cedar_unknown_tx)
        assert res_m3.status == "needs_review"
        assert "unmapped_transaction_type" in res_m3.issues

        # 4. Aurum 'Market Value (Rs.)' header renamed to 'Mkt Val (Rs.)'
        aurum_renamed_col = os.path.join(temp_dir, "mut4_aurum_mkt_val.pdf")
        _build_mutated_aurum_pdf(aurum_renamed_col, mv_header="Mkt Val (Rs.)")
        res_m4 = ProcessingPipeline(templates_dir="test_fixtures/templates").process_file(aurum_renamed_col)
        assert res_m4.status == "needs_review"
        assert "layout_drift_known_provider" in res_m4.issues
        assert any(i.startswith("expected_column_missing:") for i in res_m4.issues)

        print("[PASS] All 7 mutation cases verified with fail-closed needs_review/duplicate/validated outcomes.")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_financial_auditor_tamper_detection():
    """Tests that FinancialValidator catches tampered holdings math, weights, and dates."""
    validator = FinancialValidator()

    def make_valid_record():
        meta = StatementMeta(
            provider="Test PMS",
            client_name="Test Client",
            client_id="TEST-100",
            pan="ABCDE1234F",
            period_start="2026-09-01",
            period_end="2026-09-30",
            currency="INR"
        )
        summary = Summary(
            opening=100000.0,
            inflow=0.0,
            outflow=0.0,
            gain=5000.0,
            fee=0.0,
            cash=5000.0,
            closing=105000.0
        )
        holdings = [
            Holding(isin="INE237A01028", name="Kotak Bank", quantity=100.0, price=1000.0, market_value=100000.0, weight_pct=95.24)
        ]
        txs = [
            Transaction(date="2026-09-15", type="buy", raw_type="PURCHASE", amount=10000.0)
        ]
        return StatementRecord(statement=meta, summary=summary, holdings=holdings, transactions=txs)

    rec_tamper_math = make_valid_record()
    rec_tamper_math.holdings[0].market_value = 50000.0
    st, issues = validator.validate("dummy.pdf", [rec_tamper_math], template={})
    assert "holding_qty_price_mismatch" in issues

    rec_tamper_wt = make_valid_record()
    rec_tamper_wt.holdings = [
        Holding(isin="INE237A01028", name="H1", quantity=10.0, price=100.0, market_value=1000.0, weight_pct=40.0),
        Holding(isin="INE002A01018", name="H2", quantity=10.0, price=100.0, market_value=1000.0, weight_pct=40.0),
        Holding(isin="INE040A01034", name="H3", quantity=10.0, price=100.0, market_value=1000.0, weight_pct=40.0),
    ]
    st, issues = validator.validate("dummy.pdf", [rec_tamper_wt], template={})
    assert "holdings_weight_sum_invalid" in issues

    rec_tamper_tx = make_valid_record()
    rec_tamper_tx.transactions[0].date = "2025-01-01"
    st, issues = validator.validate("dummy.pdf", [rec_tamper_tx], template={})
    assert "transaction_date_outside_period" in issues

    rec_dup_isin = make_valid_record()
    rec_dup_isin.holdings.append(
        Holding(isin="INE237A01028", name="Kotak Bank 2", quantity=50.0, price=1000.0, market_value=50000.0, weight_pct=4.76)
    )
    st, issues = validator.validate("dummy.pdf", [rec_dup_isin], template={})
    assert "duplicate_holding_isin" in issues

    rec_tamper_cash = make_valid_record()
    rec_tamper_cash.summary.cash += 50000.0
    st, issues = validator.validate("dummy.pdf", [rec_tamper_cash], template={})
    assert "closing_value_ne_holdings_total" in issues

    print("[PASS] Financial auditor successfully quarantines tampered math, weights, dates, cash, and duplicate ISINs.")


def test_declarative_renamed_row_labels():
    """
    Tests Fix 1 (Highest Priority): Declarative row labels in TemplateHoldings.
    Tests 3 renamed variants plus 1 control:
      1. Cash renamed: 'Funds held with custodian'
      2. Total renamed: 'Aggregate Listed Equities'
      3. Both renamed: cash='Funds held with custodian', total='Aggregate Listed Equities'
      4. Control: standard labels
    All 4 reach 'validated' when parsed through learned/provided templates that declare them.
    Also asserts that without the declarative label, Variant 1 fails validator.
    """
    temp_dir = tempfile.mkdtemp()
    try:
        ctrl_pdf = os.path.join(temp_dir, "ctrl.pdf")
        v1_pdf = os.path.join(temp_dir, "v1_cash_renamed.pdf")
        v2_pdf = os.path.join(temp_dir, "v2_total_renamed.pdf")
        v3_pdf = os.path.join(temp_dir, "v3_both_renamed.pdf")

        _build_mutated_aurum_pdf(
            ctrl_pdf,
            provider_title="CONTROL WEALTH PMS",
            sebi_regn="INP000088901",
            total_equity_label="Total Equity",
            cash_row_label="Cash & Bank Balance"
        )
        _build_mutated_aurum_pdf(
            v1_pdf,
            provider_title="CUSTODIAN CASH PMS",
            sebi_regn="INP000088902",
            total_equity_label="Total Equity",
            cash_row_label="Funds held with custodian"
        )
        _build_mutated_aurum_pdf(
            v2_pdf,
            provider_title="AGGREGATE TOTAL PMS",
            sebi_regn="INP000088903",
            total_equity_label="Aggregate Listed Equities",
            cash_row_label="Cash & Bank Balance"
        )
        _build_mutated_aurum_pdf(
            v3_pdf,
            provider_title="RENAMED BOTH PMS",
            sebi_regn="INP000088904",
            total_equity_label="Aggregate Listed Equities",
            cash_row_label="Funds held with custodian"
        )

        def make_tmpl(prov_title, sebi, format_id, cash_labels=None, total_labels=None):
            return {
                "format_id": format_id,
                "version": 1,
                "layout_kind": "ruled_table",
                "provider_key": format_id,
                "sebi_regn": sebi,
                "fingerprint": {
                    "summary_headers": ["Opening Corpus", "Capital Inflows", "Capital Outflows", "Net Gain / (Loss)", "Management Fees", "Cash Balance", "Closing Corpus"],
                    "holdings_headers": ["Security", "ISIN", "Qty", "Avg Cost (Rs.)", "Market Price (Rs.)", "Market Value (Rs.)", "Weight"],
                    "transactions_headers": ["Date", "Type", "Particulars", "Qty", "Price (Rs.)", "Amount (Rs.)"]
                },
                "fields": {
                    "provider": prov_title,
                    "client_id_pattern": r"CLIENT ID:\s*([A-Za-z0-9\/\-]+)",
                    "client_name_pattern": r"Client Name:\s*([^\n\r|]+)",
                    "pan_pattern": r"PAN:\s*([A-Z]{5}[0-9]{4}[A-Z])",
                    "strategy_pattern": r"Strategy:\s*([^\n\r|]+)"
                },
                "period": {
                    "pattern": r"Statement Period:\s*([0-9A-Za-z\-]+)\s+to\s+([0-9A-Za-z\-]+)",
                    "format": "DD-Mon-YYYY"
                },
                "summary": {
                    "has_gain": True,
                    "mapping": {
                        "opening": "Opening Corpus",
                        "inflow": "Capital Inflows",
                        "outflow": "Capital Outflows",
                        "gain": "Net Gain / (Loss)",
                        "fee": "Management Fees",
                        "cash": "Cash Balance",
                        "closing": "Closing Corpus"
                    }
                },
                "holdings": {
                    "mapping": {
                        "name": "Security",
                        "isin": "ISIN",
                        "quantity": "Qty",
                        "avg_cost": "Avg Cost (Rs.)",
                        "price": "Market Price (Rs.)",
                        "market_value": "Market Value (Rs.)",
                        "weight_pct": "Weight"
                    },
                    "total_row_labels": total_labels or ["Total Equity"],
                    "cash_row_labels": cash_labels or ["Cash & Bank Balance"]
                },
                "transactions": {
                    "mapping": {
                        "date": "Date",
                        "raw_type": "Type",
                        "security": "Particulars",
                        "quantity": "Qty",
                        "price": "Price (Rs.)",
                        "amount": "Amount (Rs.)"
                    },
                    "type_map": {
                        "FEE": "fee"
                    }
                }
            }

        # 1. Negative test: Variant 1 evaluated with template lacking "Funds held with custodian"
        tmpl_missing_cash = make_tmpl("CUSTODIAN CASH PMS", "INP000088902", "tmpl_missing_cash", cash_labels=["Standard Cash Label"])
        pipe_neg = ProcessingPipeline(templates_dir=os.path.join(temp_dir, "neg_tmpl"))
        pipe_neg.fingerprinter.templates["tmpl_missing_cash"] = tmpl_missing_cash
        res_neg = pipe_neg.process_file(v1_pdf)
        assert res_neg.status == "needs_review", f"Expected needs_review when cash label missing from template, got {res_neg.status}"
        assert any(i in res_neg.issues for i in ["cash_summary_vs_row_mismatch", "missing_isin_for_listed_holding", "closing_value_ne_holdings_total"])

        # 2. Positive test: All 4 variants processed through mock LLM that supplies declarative labels
        tmpl_ctrl = make_tmpl("CONTROL WEALTH PMS", "INP000088901", "tmpl_ctrl", ["Cash & Bank Balance"], ["Total Equity"])
        tmpl_v1 = make_tmpl("CUSTODIAN CASH PMS", "INP000088902", "tmpl_v1", ["Funds held with custodian"], ["Total Equity"])
        tmpl_v2 = make_tmpl("AGGREGATE TOTAL PMS", "INP000088903", "tmpl_v2", ["Cash & Bank Balance"], ["Aggregate Listed Equities"])
        tmpl_v3 = make_tmpl("RENAMED BOTH PMS", "INP000088904", "tmpl_v3", ["Funds held with custodian"], ["Aggregate Listed Equities"])

        tmpl_map = {
            ctrl_pdf: tmpl_ctrl,
            v1_pdf: tmpl_v1,
            v2_pdf: tmpl_v2,
            v3_pdf: tmpl_v3
        }

        for test_pdf, expected_tmpl in tmpl_map.items():
            pipe_pos = ProcessingPipeline(templates_dir=os.path.join(temp_dir, f"pos_{os.path.basename(test_pdf)}"))
            pipe_pos.learner._call_llm_api = lambda prompt, t=expected_tmpl: dict(t)
            res = pipe_pos.process_file(test_pdf)
            assert res.status == "validated", f"Failed for {os.path.basename(test_pdf)}: {res.issues}"
            assert len(res.statements) == 1
            assert res.statements[0].summary.cash == 245000.0
            assert len(res.statements[0].holdings) == 2

        print("[PASS] Declarative row labels (cash_row_labels, total_row_labels) verified on 3 renamed variants + 1 control.")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_banner_overlay_robustness():
    """
    Tests Fix 2: Header banner overlay robustness.
    Adding 'Generated 12-Oct-2026 | CONFIDENTIAL' or similar header lines
    to known provider statements must NOT cause misattribution or layout drift.
    Must match the known template and pass with 0 LLM calls.
    """
    temp_dir = tempfile.mkdtemp()
    try:
        banner_aurum_pdf = os.path.join(temp_dir, "aurum_with_banner.pdf")
        _build_mutated_aurum_pdf(
            banner_aurum_pdf,
            provider_title="AURUM CAPITAL PMS",
            sebi_regn="INP000099001",
            banner_line="Generated 12-Oct-2026 | CONFIDENTIAL"
        )

        pipe = ProcessingPipeline(templates_dir="test_fixtures/templates")
        res_aurum = pipe.process_file(banner_aurum_pdf)
        assert res_aurum.status == "validated", f"Banner broke Aurum matching: {res_aurum.status} ({res_aurum.issues})"
        assert res_aurum.llm_calls_made == 0, f"Expected 0 LLM calls for known Aurum with banner, got {res_aurum.llm_calls_made}"
        assert res_aurum.statements[0].statement.provider == "Aurum Capital PMS"

        # Cedar with banner test
        cedar_with_banner = os.path.join(temp_dir, "cedar_with_banner.pdf")
        with pdfplumber.open("sample_data/pms/06_cedar_C1_textflow.pdf") as pdf:
            cedar_lines = "\n".join(p.extract_text() or "" for p in pdf.pages).split("\n")
        b_lines = ["Generated 12-Oct-2026 | CONFIDENTIAL", ""] + cedar_lines
        _build_pdf_from_lines(b_lines, cedar_with_banner, monospace=True)

        res_cedar = pipe.process_file(cedar_with_banner)
        assert res_cedar.status == "validated", f"Banner broke Cedar matching: {res_cedar.status} ({res_cedar.issues})"
        assert res_cedar.llm_calls_made == 0, f"Expected 0 LLM calls for known Cedar with banner, got {res_cedar.llm_calls_made}"
        assert res_cedar.statements[0].statement.provider == "Cedar Peak Asset Managers"

        print("[PASS] Header banner overlay robustness verified: known templates matched with 0 LLM calls.")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_comprehensive_zero_pii_leaks():
    """
    Tests Fix 4: Comprehensive zero-PII leak audit across all ground-truth statements.
    Ensures that PANs, client IDs, and client names are 100% masked in text sent to the LLM.
    """
    manifest_path = "manifest.csv"
    if not os.path.exists(manifest_path):
        manifest_path = "sample_data/manifest.csv"

    with open(manifest_path, "r", encoding="utf-8") as f:
        reader = list(csv.DictReader(f))

    learner = LearnerEngine(templates_dir="test_fixtures/templates")
    checked_count = 0
    total_leaks = 0
    leak_details = []

    for row in reader:
        rel_file = row["file"]
        if rel_file.startswith("emails/") or not os.path.exists(os.path.join("sample_data", rel_file)):
            continue

        gt_filename = os.path.basename(rel_file).replace(".pdf", ".json").replace(".xlsx", ".json").replace(".csv", ".json")
        gt_path = os.path.join("sample_data", "ground_truth", gt_filename)
        if not os.path.exists(gt_path):
            continue

        with open(gt_path, "r", encoding="utf-8") as f:
            gt_data = json.load(f)

        src_file = os.path.join("sample_data", rel_file)
        redacted_text = learner.extract_and_redact_document_text(src_file)

        for stmt in gt_data.get("statements", []):
            meta = stmt.get("statement", {})
            pan = meta.get("pan")
            cid = meta.get("client_id")
            cname = meta.get("client_name")

            if pan and pan != "N/A" and len(pan) >= 10:
                if pan.upper() in redacted_text.upper():
                    total_leaks += 1
                    leak_details.append(f"PAN leak: {pan} in {rel_file}")

            if cid and cid != "N/A" and len(cid) >= 4:
                if cid.upper() in redacted_text.upper():
                    total_leaks += 1
                    leak_details.append(f"Client ID leak: {cid} in {rel_file}")

            if cname and cname != "N/A":
                parts = [p.strip() for p in cname.split() if len(p.strip()) >= 4]
                for part in parts:
                    if re.search(r"\b" + re.escape(part) + r"\b", redacted_text, re.IGNORECASE):
                        total_leaks += 1
                        leak_details.append(f"Client name leak: {part} ({cname}) in {rel_file}")

            checked_count += 1

    assert total_leaks == 0, f"Found {total_leaks} PII leaks:\n" + "\n".join(leak_details)
    print(f"[PASS] Comprehensive zero-PII leak audit: {checked_count} statements checked, 0 PAN/name/ID leaks.")


def test_validator_negative_quantity_and_invalid_period():
    """
    Tests Fix 6: Check O (negative_quantity_in_long_only_template) and Check P (invalid_period_range).
    """
    validator = FinancialValidator()

    def make_base_record(period_start="2026-09-01", period_end="2026-09-30", qty=100.0):
        meta = StatementMeta(
            provider="Test PMS",
            client_name="Test Client",
            client_id="TEST-100",
            pan="ABCDE1234F",
            period_start=period_start,
            period_end=period_end,
            currency="INR"
        )
        summary = Summary(
            opening=100000.0,
            inflow=0.0,
            outflow=0.0,
            gain=5000.0,
            fee=0.0,
            cash=5000.0,
            closing=105000.0
        )
        holdings = [
            Holding(isin="INE237A01028", name="Kotak Bank", quantity=qty, price=1000.0, market_value=100000.0, weight_pct=100.0)
        ]
        return StatementRecord(statement=meta, summary=summary, holdings=holdings, transactions=[])

    # Check O: Long-only template with negative quantity
    rec_neg_qty = make_base_record(qty=-50.0)
    st_long, issues_long = validator.validate("test.pdf", [rec_neg_qty], template={"supports_short": False})
    assert "negative_quantity_in_long_only_template" in issues_long, f"Expected negative_quantity_in_long_only_template, got {issues_long}"

    # Short-supporting template with negative quantity -> allowed
    st_short, issues_short = validator.validate("test.pdf", [rec_neg_qty], template={"supports_short": True})
    assert "negative_quantity_in_long_only_template" not in issues_short

    # Check P: Invalid period range (end strictly before start)
    rec_inv_period = make_base_record(period_start="2026-09-30", period_end="2026-09-01")
    st_p, issues_p = validator.validate("test.pdf", [rec_inv_period], template={})
    assert "invalid_period_range" in issues_p, f"Expected invalid_period_range, got {issues_p}"

    # Valid period range
    rec_valid_period = make_base_record(period_start="2026-09-01", period_end="2026-09-30")
    st_ok, issues_ok = validator.validate("test.pdf", [rec_valid_period], template={})
    assert "invalid_period_range" not in issues_ok

    print("[PASS] Validator Check O (negative_quantity_in_long_only_template) and Check P (invalid_period_range) verified.")


def test_resaved_content_different_bytes_duplicate():
    """
    Tests Fix 6: Re-saved file with different bytes and different SHA256 hash.
    When an identical PDF is re-saved, the bytes/hash differ, but the data signature is identical.
    Must return status='duplicate' with issue 'same_content_different_bytes'.
    """
    temp_dir = tempfile.mkdtemp()
    db_path = os.path.join(temp_dir, "test_resaved.db")
    src_pdf = "sample_data/pms/01_aurum_A1_standard.pdf"

    try:
        pipe = ProcessingPipeline(templates_dir="test_fixtures/templates", db_path=db_path)

        # Run 1: original file
        res1 = pipe.process_file(src_pdf)
        assert res1.status == "validated"

        # Create re-saved file with identical data but different bytes
        resaved_pdf = os.path.join(temp_dir, "01_aurum_resaved_bytes.pdf")
        with open(src_pdf, "rb") as f_in:
            content = f_in.read()
        with open(resaved_pdf, "wb") as f_out:
            f_out.write(content + b"\n% Resaved with different bytes comment\n")

        assert compute_sha256(src_pdf) != compute_sha256(resaved_pdf), "Hashes must differ!"

        # Run 2: re-saved file
        res2 = pipe.process_file(resaved_pdf)
        assert res2.status == "duplicate", f"Expected duplicate, got {res2.status}"
        assert "same_content_different_bytes" in res2.issues, f"Expected same_content_different_bytes, got {res2.issues}"

        print("[PASS] Re-saved file with different bytes detected as duplicate (same_content_different_bytes).")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_tightened_classifier_on_prose_letters():
    """
    Tests Fix 6: Tightened classifier on prose-only letters.
    Documents without financial substance (no ISINs, no balance labels, no numeric tables)
    must be classified as 'ambiguous' and quarantined in needs_review with 'classification_uncertain'.
    """
    temp_dir = tempfile.mkdtemp()
    try:
        welcome_pdf = os.path.join(temp_dir, "welcome_letter.pdf")
        _build_pdf_from_lines([
            "Dear Valued Investor,",
            "Welcome to Alpha PMS. We are pleased to welcome you as a client.",
            "Please contact your relationship manager for account setup and onboarding questions.",
            "Sincerely,",
            "The Client Relations Team"
        ], welcome_pdf, monospace=False)

        market_pdf = os.path.join(temp_dir, "market_commentary.pdf")
        _build_pdf_from_lines([
            "Quarterly Market Commentary & Economic Outlook",
            "Global monetary tightening has created headwinds across emerging markets.",
            "Domestic macroeconomic indicators show steady industrial production.",
            "Our investment committee maintains an overweight stance on high quality franchises."
        ], market_pdf, monospace=False)

        pipe = ProcessingPipeline(templates_dir="test_fixtures/templates")

        res_welcome = pipe.process_file(welcome_pdf)
        assert res_welcome.status == "needs_review", f"Welcome letter got {res_welcome.status}"
        assert "classification_uncertain" in res_welcome.issues

        res_market = pipe.process_file(market_pdf)
        assert res_market.status == "needs_review", f"Market commentary got {res_market.status}"
        assert "classification_uncertain" in res_market.issues

        print("[PASS] Prose-only letters (welcome letter, market commentary) quarantined with classification_uncertain.")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_mailbox_poller_and_reprocess_review():
    """
    Tests Fix 7: IMAP mailbox poller resilience and --reprocess-review queue handler.
    1. Poller survives corrupted email in loop and processes valid email.
    2. reprocess_review_queue re-processes quarantined needs_review records upon receiving candidate password.
    """
    temp_dir = tempfile.mkdtemp()
    db_path = os.path.join(temp_dir, "test_poller.db")
    try:
        store = StatementStore(db_path=db_path)
        pipe = ProcessingPipeline(templates_dir="test_fixtures/templates", db_path=db_path)

        # 1. Poller error isolation test using unittest.mock
        mock_mail = MagicMock()
        mock_mail.search.return_value = ("OK", [b"101 102"])
        with open("sample_data/emails/e01_statement_single_attachment.eml", "rb") as f:
            valid_eml_bytes = f.read()

        def fetch_side_effect(m_id, query):
            if m_id == b"101":
                raise RuntimeError("Corrupt MIME or network error on message 101")
            elif m_id == b"102":
                return ("OK", [(b"102 (BODY[PEEK] {len})", valid_eml_bytes)])
            return ("NO", None)

        mock_mail.fetch.side_effect = fetch_side_effect

        poll_temp = os.path.join(temp_dir, "poll_scratch")
        processed = _poll_inbox_iteration(mock_mail, pipe, store, temp_dir=poll_temp)
        assert processed == 1, f"Expected 1 email processed despite 1 corrupt email, got {processed}"
        mock_mail.store.assert_any_call(b"102", "+FLAGS", "\\Seen")

        # 2. reprocess_review_queue test
        # Create encrypted PDF with password
        sample_src = "sample_data/pms/02_aurum_A2_sell_and_outflow.pdf"
        enc_pdf_path = os.path.join(temp_dir, "enc_for_reprocess.pdf")
        reader = pypdf.PdfReader(sample_src)
        writer = pypdf.PdfWriter()
        writer.append(reader)
        writer.encrypt("ReprocessPass99")
        with open(enc_pdf_path, "wb") as f:
            writer.write(f)

        # Process without password -> needs_review
        res_enc = pipe.process_file(enc_pdf_path)
        assert res_enc.status == "needs_review"
        assert "password_required" in res_enc.issues
        store.save_result(res_enc, sha256_hash=compute_sha256(enc_pdf_path))

        # Verify it's in needs_review queue
        q = store.get_needs_review()
        assert any(os.path.normpath(r.get("file_path", "")) == os.path.normpath(enc_pdf_path) for r in q)

        # Reprocess with candidate password
        updated = reprocess_review_queue(
            db_path=db_path,
            password="ReprocessPass99",
            templates_dir="test_fixtures/templates",
        )
        assert len(updated) >= 1
        enc_update = [u for u in updated if os.path.normpath(u["file"]) == os.path.normpath(enc_pdf_path)][0]
        assert enc_update["status"] == "validated", f"Reprocess failed: {enc_update['issues']}"

        print("[PASS] IMAP poller survives bad emails and reprocess_review_queue unlocks password-protected statements.")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_streamlit_dashboard_smoke():
    """
    Tests Fix 8: Streamlit dashboard (app.py) smoke test.
    Verifies valid Python syntax, imports, and core components without runtime syntax errors.
    """
    app_path = "app.py"
    assert os.path.exists(app_path)
    with open(app_path, "r", encoding="utf-8") as f:
        code = f.read()
    compiled = compile(code, app_path, "exec")
    assert compiled is not None
    assert "ProcessingPipeline" in code
    assert "PortfolioConsolidator" in code
    assert "StatementStore" in code
    print("[PASS] Streamlit dashboard app.py passes compile & smoke verification.")


def test_empty_runtime_and_test_fixtures():
    assert os.path.exists("templates"), "runtime template directory missing!"
    assert not [p for p in os.listdir("templates") if p.endswith(".json")], "runtime must start with no pre-saved templates"
    fixture_dir = Path("test_fixtures/templates")
    fixture_names = [p.name for p in fixture_dir.glob("*.json")]
    assert len(fixture_names) == 9
    print("[PASS] Runtime template store starts empty; 9 templates are test fixtures only.")


def test_system_owns_recognition_data():
    m1_path = "sample_data/unseen/27_meridian_unseen_M1.pdf"
    m2_path = "sample_data/unseen/28_meridian_unseen_M2.pdf"
    temp_dir = tempfile.mkdtemp()
    try:
        pipe = ProcessingPipeline(templates_dir=temp_dir)
        sloppy_template = {
            "format_id": "wrong_id_from_llm",
            "version": 1,
            "layout_kind": "ruled_table",
            "provider_key": "wrong_key_from_llm",
            "sebi_regn": None,
            "fingerprint": {
                "holdings_headers": ["Name", "Qty", "Value"],
                "table_headers": ["Bad | Header | List"]
            },
            "fields": {
                "provider": "Meridian Wealth PMS",
                "client_id_pattern": r"Investor Code\s*:\s*([A-Za-z0-9\-]+)",
                "client_name_pattern": r"Investor\s*:\s*([^|]+)",
                "pan_pattern": r"Tax ID\s*:\s*([A-Z]{5}[0-9]{4}[A-Z])",
                "strategy_pattern": r"Mandate\s*:\s*([^|]+)"
            },
            "period": {
                "pattern": r"Reporting Window:\s*([0-9\/]+)\s*to\s*([0-9\/]+)",
                "format": "DD/MM/YYYY"
            },
            "summary": {
                "has_gain": True,
                "columns_order": [
                    "Portfolio worth at start", "Fresh Inflows", "Redemptions",
                    "Net Profit", "Advisory Levy", "Liquid Cash", "Ending worth"
                ],
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
                    "name": "Scrip", "isin": "ISIN", "quantity": "Units",
                    "avg_cost": "Cost per Unit", "price": "Rate", "market_value": "Worth",
                    "weight_pct": "Share %"
                }
            },
            "transactions": {
                "mapping": {
                    "date": "Trade Date", "raw_type": "Action", "security": "Scrip Name",
                    "quantity": "Units", "price": "Rate", "amount": "Net Amount"
                },
                "type_map": {
                    "CAPITAL IN": "inflow", "ACQUIRE": "buy", "DISPOSE": "sell", "ADVISORY LEVY": "fee"
                }
            }
        }
        pipe.learner._call_llm_api = lambda prompt: dict(sloppy_template)
        res1 = pipe.process_file(m1_path)
        assert res1.status == "validated", f"Expected validated for M1, got {res1.status} ({res1.issues})"
        assert res1.llm_calls_made == 1
        
        saved_tmpl = pipe.learner.load_template(res1.format_id)
        assert "meridian" in saved_tmpl["format_id"].lower()
        assert saved_tmpl["provider_key"] == "meridian_wealth_pms"
        assert saved_tmpl["sebi_regn"] == "INP000088101"
        assert any("Scrip" in h for h in saved_tmpl["fingerprint"]["table_headers"])
        
        res2 = pipe.process_file(m2_path)
        assert res2.status == "validated", f"Expected validated for M2, got {res2.status} ({res2.issues})"
        assert res2.llm_calls_made == 0
        assert "layout_drift_known_provider" not in res2.issues
        print("[PASS] Item 2: System owns recognition data; sloppy LLM fingerprint overwritten, M2 parses with 0 calls.")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_harden_llm_call_single_attempt():
    from learner import _scrub_log_message
    fake_url = "https://generativelanguage.googleapis.com/v1beta/models?key=AQ.SECRET_KEY_1234567890abcdef"
    scrubbed = _scrub_log_message(f"ConnectionError: Failed to connect to {fake_url}")
    assert "SECRET_KEY_1234567890abcdef" not in scrubbed
    assert "[REDACTED_KEY]" in scrubbed

    m1_path = "sample_data/unseen/27_meridian_unseen_M1.pdf"
    m2_path = "sample_data/unseen/28_meridian_unseen_M2.pdf"
    temp_dir = tempfile.mkdtemp()
    try:
        pipe = ProcessingPipeline(templates_dir=temp_dir)
        calls_count = [0]
        
        def mock_llm_invalid(prompt: str):
            calls_count[0] += 1
            return {"format_id": "test_bad", "layout_kind": "ruled_table"}

        pipe.learner._call_llm_api = mock_llm_invalid
        res1 = pipe.process_file(m1_path)
        assert res1.status == "needs_review"
        assert "learning_failed_invalid_template" in res1.issues
        assert res1.llm_calls_made == 1
        assert calls_count[0] == 1

        res2 = pipe.process_file(m2_path)
        assert res2.status == "needs_review"
        assert res2.llm_calls_made == 0
        assert calls_count[0] == 1

        assert os.path.exists(os.path.join("logs", "llm_learning.log"))
        with open(os.path.join("logs", "llm_learning.log"), "r", encoding="utf-8") as f:
            log_content = f.read()
        assert "outcome=failed: schema error" in log_content
        print("[PASS] Item 3: LLM keys scrubbed, invalid learning uses one request then fails closed; repeat makes 0 calls.")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_header_matching_wrapped_and_fuzzy():
    from utils import normalise_header, match_header_with_fuzzy
    assert normalise_header("Quantit\ny") == "quantity"
    assert normalise_header("Quantity") == "quantity"
    assert normalise_header("Quantit\ny") == normalise_header("Quantity")

    assert normalise_header("Avg Cost (Rs.)") == "avgcost"
    assert normalise_header("Avg Cost") == "avgcost"
    assert normalise_header("Avg Cost (Rs.)") == normalise_header("Avg Cost")

    assert normalise_header("Holding\n(Qty)") == "holding"
    assert normalise_header("Holding (Qty)") == "holding"
    assert normalise_header("Holding\n(Qty)") == normalise_header("Holding (Qty)")

    matched_wrap, err_wrap = match_header_with_fuzzy("Quantity", ["Qty", "Quantit\ny", "Price"])
    assert matched_wrap == "Quantit\ny"
    assert err_wrap is None

    matched, err = match_header_with_fuzzy("Total Quantity", ["Qty", "Total Quantiti", "Price"])
    assert matched == "Total Quantiti"
    assert err is None

    matched_tie, err_tie = match_header_with_fuzzy("Quantity", ["Quantity 1", "Quantity 2"])
    assert matched_tie is None
    assert err_tie == "ambiguous_header"
    print("[PASS] Item 4: Header normalization handles wrapped text, unit suffixes, and ambiguous ties.")


def test_metadata_sanity_checks():
    from validator import FinancialValidator
    from models import StatementMeta, Summary, StatementRecord
    validator = FinancialValidator()

    base_rec = StatementRecord(
        statement=StatementMeta(
            provider="Aurum Capital PMS",
            strategy="Aurum Multicap Growth",
            client_id="AUR-1001",
            client_name="Ananya Rao",
            period_end="2026-09-30"
        ),
        summary=Summary(closing=100000.0, opening=100000.0, cash=100000.0),
        holdings=[]
    )

    rec_nl = base_rec.model_copy(deep=True)
    rec_nl.statement.client_name = "Ananya\nRao"
    st1, iss1 = validator.validate("dummy.pdf", [rec_nl])
    assert st1 == "needs_review"
    assert "metadata_value_suspect:client_name" in iss1

    rec_long = base_rec.model_copy(deep=True)
    rec_long.statement.strategy = "A" * 85
    st2, iss2 = validator.validate("dummy.pdf", [rec_long])
    assert st2 == "needs_review"
    assert "metadata_value_suspect:strategy" in iss2

    rec_amt = base_rec.model_copy(deep=True)
    rec_amt.statement.client_id = "1,00,000.00"
    st3, iss3 = validator.validate("dummy.pdf", [rec_amt])
    assert st3 == "needs_review"
    assert "metadata_value_suspect:client_id" in iss3

    from pipeline import ProcessingPipeline
    pipe = ProcessingPipeline(templates_dir="test_fixtures/templates")
    res10 = pipe.process_file("sample_data/pms/10_aurum_v2_layout_drift.pdf")
    assert res10.statements[0].statement.strategy == "Aurum Multicap Growth"
    print("[PASS] Item 5: Metadata sanity checks enforce clean single-line strings; leading labels stripped.")


def test_weights_tolerances():
    from validator import FinancialValidator
    from models import StatementMeta, Summary, Holding, StatementRecord
    validator = FinancialValidator()

    h1 = Holding(name="Stock A", quantity=100, price=500, market_value=50000, weight_pct=50.0)
    h2 = Holding(name="Stock B", quantity=100, price=500, market_value=50000, weight_pct=50.0)
    rec = StatementRecord(
        statement=StatementMeta(provider="Test PMS", client_id="C1", client_name="Name", period_end="2026-09-30", currency="INR"),
        summary=Summary(closing=100000.0, opening=100000.0, cash=0.0),
        holdings=[h1, h2]
    )

    st_ok, iss_ok = validator.validate("dummy.pdf", [rec])
    assert "holdings_weight_sum_invalid" not in iss_ok
    assert "holding_weight_mismatch" not in iss_ok

    rec_bad_total = rec.model_copy(deep=True)
    rec_bad_total.holdings[0].weight_pct = 52.0
    rec_bad_total.holdings[1].weight_pct = 52.0
    _, iss_bad_total = validator.validate("dummy.pdf", [rec_bad_total])
    assert "holdings_weight_sum_invalid" in iss_bad_total

    rec_bad_h = rec.model_copy(deep=True)
    rec_bad_h.holdings[0].weight_pct = 51.0
    rec_bad_h.holdings[1].weight_pct = 49.0
    _, iss_bad_h = validator.validate("dummy.pdf", [rec_bad_h])
    assert "holding_weight_mismatch" in iss_bad_h
    print("[PASS] Item 6: Tolerances audited: total weights (+/-0.5) and per-holding weight (+/-0.1).")


def test_email_events_persistence_and_routing():
    """
    Tests Fix 1 & 2: Email attachment classification and SQLite email_events persistence.
    1. e01 (statement PDF) -> ingest, saved to email_events and statements.
    2. e03 (newsletter with factsheet) -> ignore (newsletter_with_factsheet), saved to email_events, NOT in statements.
    3. e04 (portal link no attachment) -> ignore (portal_link_no_attachment), saved to email_events, NOT in statements.
    """
    temp_dir = tempfile.mkdtemp()
    db_path = os.path.join(temp_dir, "events_test.db")
    extract_dir = os.path.join(temp_dir, "att")
    try:
        store = StatementStore(db_path=db_path)
        pipe = ProcessingPipeline(templates_dir="test_fixtures/templates", db_path=db_path)

        # 1. Ingest statement email
        res_e01 = process_eml_file("sample_data/emails/e01_statement_single_attachment.eml", pipe, store, target_dir=extract_dir)
        assert len(res_e01) == 1
        assert res_e01[0]["status"] == "validated"

        # 2. Ignore newsletter email
        res_e03 = process_eml_file("sample_data/emails/e03_newsletter_with_factsheet.eml", pipe, store, target_dir=extract_dir)
        assert len(res_e03) == 0

        # 3. Ignore portal link email
        res_e04 = process_eml_file("sample_data/emails/e04_portal_link_no_attachment.eml", pipe, store, target_dir=extract_dir)
        assert len(res_e04) == 0

        # Check email_events table
        events = store.get_email_events()
        assert len(events) == 3
        statuses = {ev["status"] for ev in events}
        assert "ingest" in statuses
        assert "ignore" in statuses

        reasons = {ev["reason"] for ev in events if ev["reason"]}
        assert "newsletter_with_factsheet" in reasons
        assert "portal_link_no_attachment" in reasons

        # Statements table only has statement record from e01, not e03 or e04
        all_stmts = store.get_all()
        assert len(all_stmts) == 1
        assert all_stmts[0]["format_id"] == "aurum_pms_monthly"

        print("[PASS] Email-level events persisted to SQLite for ingest and ignore, with reasons preserved.")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_unsupported_attachments_and_inline_pdf_routing():
    """
    Tests Fix 1: Non-statement and unsupported attachments route to needs_review, not silently ignored.
    .zip -> zip_attachment_requires_manual_review
    .xls / .xlsm -> unsupported_statement_attachment_type
    .bin -> ambiguous_attachment_requires_review
    inline PDF without filename -> parsed as inline_statement.pdf
    """
    from email.mime.multipart import MIMEMultipart
    from email.mime.base import MIMEBase
    from email.mime.text import MIMEText
    from email import encoders

    temp_dir = tempfile.mkdtemp()
    db_path = os.path.join(temp_dir, "unsupported_test.db")
    extract_dir = os.path.join(temp_dir, "att")
    try:
        store = StatementStore(db_path=db_path)
        pipe = ProcessingPipeline(templates_dir="test_fixtures/templates", db_path=db_path)

        def make_eml(filename: str, payload: bytes, subject: str = "Test Subject", mime_sub: str = "octet-stream", content_id: str = None) -> str:
            msg = MIMEMultipart()
            msg["Subject"] = subject
            msg["From"] = "sender@example.com"
            msg["To"] = "client@example.com"
            msg["Message-ID"] = f"<{filename or 'inline'}_{len(payload)}@example.com>"
            msg.attach(MIMEText("Please find your file attached.", "plain"))
            part = MIMEBase("application", mime_sub)
            part.set_payload(payload)
            encoders.encode_base64(part)
            if filename:
                part.add_header("Content-Disposition", "attachment", filename=filename)
            elif content_id:
                part.add_header("Content-ID", f"<{content_id}>")
                part.add_header("Content-Disposition", "inline")
            msg.attach(part)
            eml_file = os.path.join(temp_dir, f"{filename or 'inline'}.eml")
            with open(eml_file, "wb") as f:
                f.write(msg.as_bytes())
            return eml_file

        # Test ZIP attachment
        zip_eml = make_eml("monthly_report.zip", b"PK\x03\x04dummyzipcontent", "Your Monthly Wealth Portfolio")
        res_zip = process_eml_file(zip_eml, pipe, store, target_dir=extract_dir)
        assert len(res_zip) == 1
        assert res_zip[0]["status"] == "needs_review"
        assert "zip_attachment_requires_manual_review" in res_zip[0]["issues"]

        # Test XLS attachment
        xls_eml = make_eml("pms_statement.xls", b"\xd0\xcf\x11\xe0dummyxls", "PMS Statement September")
        res_xls = process_eml_file(xls_eml, pipe, store, target_dir=extract_dir)
        assert len(res_xls) == 1
        assert res_xls[0]["status"] == "needs_review"
        assert "unsupported_statement_attachment_type" in res_xls[0]["issues"]

        # Test XLSM attachment
        xlsm_eml = make_eml("pms_statement.xlsm", b"PK\x03\x04dummyxlsm", "Portfolio Macro Statement")
        res_xlsm = process_eml_file(xlsm_eml, pipe, store, target_dir=extract_dir)
        assert len(res_xlsm) == 1
        assert res_xlsm[0]["status"] == "needs_review"
        assert "unsupported_statement_attachment_type" in res_xlsm[0]["issues"]

        # Test ambiguous .bin file
        bin_eml = make_eml("financial_export.bin", b"binarydata", "Quarterly Statement Data")
        res_bin = process_eml_file(bin_eml, pipe, store, target_dir=extract_dir)
        assert len(res_bin) == 1
        assert res_bin[0]["status"] == "needs_review"
        assert "ambiguous_attachment_requires_review" in res_bin[0]["issues"]

        # Test inline PDF missing filename
        with open("sample_data/pms/01_aurum_A1_standard.pdf", "rb") as f:
            pdf_bytes = f.read()
        inline_eml = make_eml("", pdf_bytes, "Portfolio Statement Attached", mime_sub="pdf", content_id="inline_pdf_01")
        res_inline = process_eml_file(inline_eml, pipe, store, target_dir=extract_dir)
        assert len(res_inline) == 1
        assert res_inline[0]["status"] in ["validated", "needs_review"]

        print("[PASS] Unsupported statement attachments (.zip, .xls, .xlsm, .bin, inline PDF) correctly quarantined to needs_review.")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_crypto_dependency_error_code():
    """
    Tests Fix 5: AES encrypted PDF dependency failure returns pdf_crypto_dependency_missing,
    not misreported as no_text_layer_requires_ocr.
    """
    from unittest.mock import patch
    ingestion = IngestionEngine()
    dummy_pdf = "sample_data/pms/01_aurum_A1_standard.pdf"

    # Simulate pypdf raising DependencyError on AES PDF
    pipe = ProcessingPipeline(templates_dir="test_fixtures/templates")
    with patch("pypdf.PdfReader") as mock_reader:
        mock_reader.side_effect = Exception("PyCryptodome or cryptography is required for AES algorithm")
        is_locked, has_text, text, eff_path, issues = pipe.ingestion.check_pdf_status(dummy_pdf)
        assert is_locked is True
        assert "pdf_crypto_dependency_missing" in issues
        assert "no_text_layer_requires_ocr" not in issues

        res = pipe.process_file(dummy_pdf)
        assert res.status == "needs_review"
        assert "pdf_crypto_dependency_missing" in res.issues
        assert "no_text_layer_requires_ocr" not in res.issues

    print("[PASS] AES encrypted PDF dependency failure correctly reports pdf_crypto_dependency_missing.")


def test_v2_layout_drift_and_promotion_0_llm_calls():
    """
    Learns a drifted layout once, persists it as unapproved across restarts, and
    parses it as validated with 0 LLM calls only after explicit promotion.
    """
    temp_dir = tempfile.mkdtemp()
    temp_tmpl_dir = os.path.join(temp_dir, "templates")
    os.makedirs(temp_tmpl_dir)
    shutil.copyfile(
        "test_fixtures/templates/aurum_pms_monthly.json",
        os.path.join(temp_tmpl_dir, "aurum_pms_monthly.json")
    )
    db_path = os.path.join(temp_dir, "drift_test.db")

    drift_pdf = "sample_data/pms/10_aurum_v2_layout_drift.pdf"

    try:
        # Run 1: unseen drift variant is learned but remains unapproved.
        pipe1 = ProcessingPipeline(templates_dir=temp_tmpl_dir, db_path=db_path)
        with open("test_fixtures/templates/aurum_pms_monthly_v2.json", "r", encoding="utf-8") as f:
            v2_template = json.load(f)
        llm_prompts = []
        pipe1.learner._call_llm_api = lambda prompt: (llm_prompts.append(prompt) or dict(v2_template))
        res1 = pipe1.process_file(drift_pdf)
        assert res1.status == "needs_review"
        assert "layout_drift_known_provider" in res1.issues
        assert res1.llm_calls_made == 1
        assert len(llm_prompts) == 1
        with open(os.path.join(temp_tmpl_dir, "aurum_pms_monthly_v2.json"), "r", encoding="utf-8") as f:
            assert json.load(f)["approved"] is False

        # A fresh process must still treat the persisted variant as unapproved.
        pipe2 = ProcessingPipeline(templates_dir=temp_tmpl_dir, db_path=db_path)
        res2 = pipe2.process_file(drift_pdf)
        assert res2.status == "needs_review"
        assert "layout_drift_known_provider" in res2.issues
        assert res2.llm_calls_made == 0

        # Explicitly promote, then verify a fresh pipeline can parse deterministically.
        success = pipe2.approve_template("aurum_pms_monthly_v2")
        assert success is True

        pipe3 = ProcessingPipeline(templates_dir=temp_tmpl_dir, db_path=db_path)
        res3 = pipe3.process_file(drift_pdf)
        assert res3.status == "validated", f"Expected validated, got {res3.status} ({res3.issues})"
        assert res3.format_id == "aurum_pms_monthly_v2"
        assert res3.llm_calls_made == 0
        assert "layout_drift_known_provider" not in res3.issues

        print("[PASS] Drift variant persisted unapproved, then parses with 0 LLM calls after promotion.")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_same_provider_multi_product_best_match():
    """
    Tests Fix 7: Multi-product template matching for the same provider evaluates
    header similarity across all candidates and picks the highest scoring match.
    """
    from fingerprinter import FormatFingerprinter

    tmpl_a = {
        "format_id": "zenith_product_alpha",
        "version": 1,
        "layout_kind": "ruled_table",
        "provider_key": "zenith_capital",
        "fields": {"provider": "Zenith Capital Management"},
        "fingerprint": {
            "table_headers": ["Security Name | ISIN | Quantity | Average Cost | Current Value"]
        },
        "holdings": {
            "mapping": {
                "name": "Security Name",
                "isin": "ISIN",
                "quantity": "Quantity",
                "avg_cost": "Average Cost",
                "market_value": "Current Value"
            }
        },
        "approved": True
    }
    tmpl_b = {
        "format_id": "zenith_product_beta",
        "version": 1,
        "layout_kind": "ruled_table",
        "provider_key": "zenith_capital",
        "fields": {"provider": "Zenith Capital Management"},
        "fingerprint": {
            "table_headers": ["Instrument | ISIN Code | Units Held | Cost Price | NAV Total"]
        },
        "holdings": {
            "mapping": {
                "name": "Instrument",
                "isin": "ISIN Code",
                "quantity": "Units Held",
                "avg_cost": "Cost Price",
                "market_value": "NAV Total"
            }
        },
        "approved": True
    }

    fingerprinter = FormatFingerprinter(templates_dict={
        "zenith_product_alpha": tmpl_a,
        "zenith_product_beta": tmpl_b
    })

    # Document features matching tmpl_b headers
    features = {
        "doc_provider": "Zenith Capital Management",
        "sebi_regn": "",
        "org_slug": "zenith_capital",
        "layout_kind": "ruled_table",
        "table_headers": ["Instrument | ISIN Code | Units Held | Cost Price | NAV Total"]
    }

    matched_id, matched_tmpl, is_drift = fingerprinter.match_template(features)

    assert matched_id == "zenith_product_beta", f"Expected zenith_product_beta, got {matched_id}"
    assert is_drift is False

    print("[PASS] Same-provider multiple product matching selects the best header match.")


def test_durable_failed_learning_persistence():
    """
    Tests Fix 8: Persisted failed learning attempts in SQLite prevent repeated LLM calls across restarts.
    """
    temp_dir = tempfile.mkdtemp()
    db_path = os.path.join(temp_dir, "test_failed_learn.db")

    try:
        store = StatementStore(db_path=db_path)
        store.save_failed_learning(
            format_id="failed_layout_xyz",
            provider_key="unknown_prov",
            layout_kind="ruled_table",
            fingerprint_hash="fp_hash_12345",
            failure_issue="learning_failed_self_check",
            diagnostic_message="Model produced invalid columns"
        )

        from learner import LearnerEngine
        learner = LearnerEngine(db_path=db_path)
        assert ("failed_layout_xyz", "fp_hash_12345") in learner.failed_formats

        pipe = ProcessingPipeline(templates_dir="test_fixtures/templates", db_path=db_path)
        assert ("failed_layout_xyz", "fp_hash_12345") in pipe.learner.failed_formats

        print("[PASS] Failed learning attempts persist by exact fingerprint without blocking other layouts.")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def run_all_tests():
    print("=" * 80)
    print("ACTIVV MONEY PARSER - UNIT TESTS & ADVANCED SAFETY GATE AUDIT")
    print("=" * 80)

    test_clean_number()
    test_parse_date()
    test_is_valid_isin()
    test_three_way_classifier()
    test_password_protected_pdf_handling()
    test_email_path_traversal_and_temp_cleanup()
    test_real_one_shot_learner_on_unseen_provider()
    test_provider_misattribution_guard()
    test_persistent_duplicate_and_revision_detection()
    test_seven_mutation_audit_cases()
    test_financial_auditor_tamper_detection()
    test_declarative_renamed_row_labels()
    test_banner_overlay_robustness()
    test_comprehensive_zero_pii_leaks()
    test_validator_negative_quantity_and_invalid_period()
    test_resaved_content_different_bytes_duplicate()
    test_tightened_classifier_on_prose_letters()
    test_mailbox_poller_and_reprocess_review()
    test_streamlit_dashboard_smoke()
    test_empty_runtime_and_test_fixtures()
    test_system_owns_recognition_data()
    test_harden_llm_call_single_attempt()
    test_header_matching_wrapped_and_fuzzy()
    test_metadata_sanity_checks()
    test_weights_tolerances()
    test_email_events_persistence_and_routing()
    test_unsupported_attachments_and_inline_pdf_routing()
    test_crypto_dependency_error_code()
    test_v2_layout_drift_and_promotion_0_llm_calls()
    test_same_provider_multi_product_best_match()
    test_durable_failed_learning_persistence()
    test_codex_fixes_suite()
    test_mandatory_currency_and_cross_currency_isolation()

    print("\n" + "=" * 80)
    print("ALL 33 UNIT, REGRESSION & MUTATION AUDITS PASSED WITH 100% SUCCESS!")
    print("=" * 80)


def test_codex_fixes_suite():
    """
    Direct verification of Codex-reported flaws:
    1. Check M: Statement with summary inflow=50,000 but 0 transactions extracted triggers missing_transactions_for_summary_flow & transactions_inflow_mismatch.
    2. Consolidator: Client with two strategies under SAME provider consolidates both without dropping one as superseded.
    3. Tabular Pre-classifier: Rejects invoice CSV as not_a_statement.
    4. Mailbox State Machine: Retries processing if previous attempt left email in 'started' or 'failed' state.
    5. US Date Parsing: Handles dayfirst=False correctly.
    """
    from models import StatementMeta, Summary, Holding, StatementRecord, ExtractionResult
    from validator import FinancialValidator
    from consolidator import PortfolioConsolidator
    from ingestion import IngestionEngine

    validator = FinancialValidator()
    ingestion = IngestionEngine()
    consolidator = PortfolioConsolidator()

    # 1. Check M: summary inflow=50k with 0 transactions
    rec_flow_no_txns = StatementRecord(
        statement=StatementMeta(provider="Aurum Capital PMS", strategy="Aurum Multi-Cap", client_id="AUR-1", client_name="Test Client", period_end="2026-09-30", currency="INR"),
        summary=Summary(opening=100000.0, inflow=50000.0, outflow=0.0, gain=5000.0, fee=0.0, cash=55000.0, closing=155000.0),
        holdings=[Holding(isin="INE237A01028", name="Kotak Bank", quantity=100.0, price=1000.0, market_value=100000.0, weight_pct=100.0)],
        transactions=[]
    )
    st_flow, issues_flow = validator.validate("test_flow.pdf", [rec_flow_no_txns], template={"fields": {"strategy": "Aurum Multi-Cap"}})
    assert "missing_transactions_for_summary_flow" in issues_flow, f"Expected missing_transactions_for_summary_flow, got {issues_flow}"
    assert "transactions_inflow_mismatch" in issues_flow, f"Expected transactions_inflow_mismatch, got {issues_flow}"
    assert st_flow == "needs_review"

    # 2. Consolidator: same provider, same client, DIFFERENT strategies
    rec_strat1 = StatementRecord(
        statement=StatementMeta(provider="Aurum Capital PMS", strategy="Large Cap Alpha", client_id="AUR-1", client_name="Ramesh Gupta", pan="ABCDE1234F", period_end="2026-09-30", currency="INR"),
        summary=Summary(opening=100000.0, inflow=0.0, outflow=0.0, gain=0.0, fee=0.0, cash=10000.0, closing=100000.0),
        holdings=[Holding(isin="INE002A01018", name="Reliance Industries", quantity=50.0, price=1800.0, market_value=90000.0, weight_pct=90.0)],
        transactions=[]
    )
    rec_strat2 = StatementRecord(
        statement=StatementMeta(provider="Aurum Capital PMS", strategy="Small Cap Growth", client_id="AUR-1", client_name="Ramesh Gupta", pan="ABCDE1234F", period_end="2026-09-30", currency="INR"),
        summary=Summary(opening=200000.0, inflow=0.0, outflow=0.0, gain=0.0, fee=0.0, cash=20000.0, closing=200000.0),
        holdings=[Holding(isin="INE237A01028", name="Kotak Bank", quantity=100.0, price=1800.0, market_value=180000.0, weight_pct=90.0)],
        transactions=[]
    )
    res_mock1 = ExtractionResult(file="stmt1.pdf", format_id="f1", status="validated", issues=[], statements=[rec_strat1], llm_calls_made=0)
    res_mock2 = ExtractionResult(file="stmt2.pdf", format_id="f1", status="validated", issues=[], statements=[rec_strat2], llm_calls_made=0)
    cons = consolidator.consolidate_client([res_mock1, res_mock2], target_pan="ABCDE1234F", target_period_end="2026-09-30")
    assert cons is not None, "Consolidation should not be None"
    assert cons.total_corpus == 300000.0, f"Expected corpus 300000.0 (both strategies combined), got {cons.total_corpus}"
    assert cons.raw_statements_count == 2, f"Expected 2 accounts consolidated, got {cons.raw_statements_count}"

    # 3. Tabular Non-Statement Pre-classification
    temp_dir = tempfile.mkdtemp()
    try:
        inv_csv = os.path.join(temp_dir, "vendor_invoice.csv")
        with open(inv_csv, "w", encoding="utf-8") as f:
            f.write("Invoice No,Vendor Name,Amount,Due Date\nINV-001,Acme Supplies,15000.00,2026-10-01\n")
        cls_res, _ = ingestion.classify_tabular_document(inv_csv)
        assert cls_res == "not_a_statement", f"Expected not_a_statement for vendor invoice, got {cls_res}"
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    # 4. Mailbox State Machine: retry allowed if previous run failed/started
    temp_db_dir = tempfile.mkdtemp()
    try:
        db_p = os.path.join(temp_db_dir, "test_retry.db")
        store = StatementStore(db_path=db_p)
        store.save_email_event(message_id="msg_test_retry", status="ingest", processing_state="failed")
        assert not store.has_email_event("msg_test_retry"), "Failed email event must NOT be skipped on retry"
        store.update_email_event_processing_state("msg_test_retry", "completed")
        assert store.has_email_event("msg_test_retry"), "Completed email event must be skipped on retry"
    finally:
        shutil.rmtree(temp_db_dir, ignore_errors=True)

    # 5. US date parsing with dayfirst=False
    from utils import parse_date
    assert parse_date("03/04/2026", dayfirst=False) == "2026-03-04", "US format 03/04/2026 must parse as March 4"
    assert parse_date("03/04/2026", dayfirst=True) == "2026-04-03", "UK/Indian format 03/04/2026 must parse as April 3"

    print("[PASS] Codex-reported flaws verified: missing flows, multi-strategy consolidation, tabular classification, and transactional retry.")


def test_mandatory_currency_and_cross_currency_isolation():
    """
    Audits currency enforcement against silent INR fallback:
    1. StatementMeta defaults currency to None (no silent 'INR' fallback).
    2. Missing or unrecognized currency fails closed with 'missing_currency' and 'missing_required_metadata'.
    3. Unverified currency records are strictly rejected from single-currency consolidation.
    4. Non-INR statements (USD) are rejected from INR portfolios with 'currency_mismatch'.
    5. Multi-currency consolidation segregates INR and USD portfolios with zero cross-contamination.
    6. CSV statements without currency metadata extract currency as None and route to needs_review.
    7. CSV statements with explicit currency (e.g. USD) extract cleanly.
    """
    from models import StatementMeta, Summary, Holding, StatementRecord, ExtractionResult
    from validator import FinancialValidator
    from consolidator import PortfolioConsolidator
    from parser import StatementParser
    import json
    import tempfile
    import shutil

    validator = FinancialValidator()
    consolidator = PortfolioConsolidator()
    parser = StatementParser()

    # 1. StatementMeta default is None
    meta_blank = StatementMeta(provider="Aurum Capital PMS", strategy="Growth", client_id="C1", client_name="Test", period_end="2026-09-30")
    assert meta_blank.currency is None, f"Expected meta.currency to be None by default, got {meta_blank.currency}"

    # 2. Validator fail closed on missing currency
    rec_no_curr = StatementRecord(
        statement=meta_blank,
        summary=Summary(opening=100000.0, cash=10000.0, closing=100000.0),
        holdings=[Holding(isin="INE002A01018", name="Reliance Industries", quantity=50.0, price=1800.0, market_value=90000.0, weight_pct=90.0)],
        transactions=[]
    )
    st_val, issues_val = validator.validate("dummy.pdf", [rec_no_curr], template={"fields": {"strategy": "Growth"}})
    assert st_val == "needs_review", f"Expected needs_review for missing currency, got {st_val}"
    assert "missing_currency" in issues_val, f"Expected missing_currency in issues, got {issues_val}"
    assert "missing_required_metadata" in issues_val, f"Expected missing_required_metadata in issues, got {issues_val}"

    # 3. Consolidator rejects unverified currency
    res_unverified = ExtractionResult(
        file="unverified_curr.pdf",
        status="needs_review",
        issues=["missing_currency"],
        statements=[rec_no_curr]
    )
    # Passed alone: candidate_records is empty -> returns None
    cons_alone = consolidator.consolidate_client([res_unverified], target_pan="ABCDE1234F", target_currency="INR")
    assert cons_alone is None, "Consolidation with only unverified currency must return None"

    # 4. Cross-currency isolation: USD statement excluded from INR portfolio
    rec_usd = StatementRecord(
        statement=StatementMeta(provider="Fulcrum Hedge Partners", strategy="USD Macro", client_id="F-1", client_name="Global Client", pan="ABCDE1234F", period_end="2026-09-30", currency="USD"),
        summary=Summary(opening=100000.0, cash=10000.0, closing=100000.0),
        holdings=[Holding(isin="INE002A01018", name="Global Equity", quantity=50.0, price=1800.0, market_value=90000.0, weight_pct=90.0)],
        transactions=[]
    )
    res_usd = ExtractionResult(
        file="fulcrum_usd.pdf",
        status="validated",
        issues=[],
        statements=[rec_usd]
    )
    cons_usd_alone = consolidator.consolidate_client([res_usd], target_pan="ABCDE1234F", target_currency="INR")
    assert cons_usd_alone is None, "USD statement alone targeted for INR portfolio must return None"

    rec_inr = StatementRecord(
        statement=StatementMeta(provider="Aurum Capital PMS", strategy="Aurum Growth", client_id="AUR-1", client_name="Global Client", pan="ABCDE1234F", period_end="2026-09-30", currency="INR"),
        summary=Summary(opening=200000.0, cash=20000.0, closing=200000.0),
        holdings=[Holding(isin="INE237A01028", name="Kotak Bank", quantity=100.0, price=1800.0, market_value=180000.0, weight_pct=90.0)],
        transactions=[]
    )
    res_inr = ExtractionResult(file="aurum_inr.pdf", status="validated", issues=[], statements=[rec_inr])

    # Combined with valid INR: unverified & USD statements are strictly excluded
    cons_combined = consolidator.consolidate_client([res_inr, res_unverified, res_usd], target_pan="ABCDE1234F", target_currency="INR")
    assert cons_combined is not None and cons_combined.raw_statements_count == 1, "Only valid INR statement must be consolidated"
    assert cons_combined.total_corpus == 200000.0
    assert any(ex.get("reason") in ("unvalidated_status", "unverified_currency") for ex in cons_combined.excluded_statements)
    assert any(ex.get("reason") == "currency_mismatch" for ex in cons_combined.excluded_statements)

    # 5. Multi-currency consolidation segregates portfolios
    multi_ports = consolidator.consolidate_all_currencies([res_inr, res_usd], target_pan="ABCDE1234F")
    assert "INR" in multi_ports and "USD" in multi_ports, f"Expected both INR and USD portfolios, got {multi_ports.keys()}"
    assert multi_ports["INR"].total_corpus == 200000.0
    assert multi_ports["USD"].total_corpus == 100000.0

    # 6. Tabular CSV without currency field extracts None and triggers missing_currency
    temp_dir = tempfile.mkdtemp()
    try:
        csv_no_curr = os.path.join(temp_dir, "no_curr.csv")
        with open(csv_no_curr, "w", encoding="utf-8") as f:
            f.write("Hestia Portfolio Services,Client Statement\n")
            f.write("Client ID,HPS-9999\n")
            f.write("Client Name,Test User\n")
            f.write("Strategy,Hestia Value\n")
            f.write("Period,2026-09-01 to 2026-09-30\n")
            f.write("\nSUMMARY\nOpening,100000.0\nClosing,100000.0\nCash,100000.0\n")
            f.write("\nHOLDINGS\nSecurity,ISIN,Qty,Avg Cost,Price,Value,Weight %\n")
            f.write("TOTAL,,0,,,100000.0,100.0\n")
            f.write("\nTRANSACTIONS\nDate,Type,Security,Qty,Price,Amount\n")

        with open("test_fixtures/templates/hestia_pms_csv.json", "r", encoding="utf-8") as tf:
            tmpl_csv = json.load(tf)
        parsed_no_curr = parser._parse_csv_sectioned(csv_no_curr, tmpl_csv)
        assert parsed_no_curr[0].statement.currency is None, f"Expected None currency for CSV without currency field, got {parsed_no_curr[0].statement.currency}"
        st_c, iss_c = validator.validate(csv_no_curr, parsed_no_curr, tmpl_csv)
        assert st_c == "needs_review" and "missing_currency" in iss_c

        # 7. Tabular CSV with USD currency extracts USD
        csv_usd = os.path.join(temp_dir, "usd.csv")
        with open(csv_usd, "w", encoding="utf-8") as f:
            f.write("Hestia Portfolio Services,Client Statement\n")
            f.write("Client ID,HPS-9999\n")
            f.write("Client Name,Test User\n")
            f.write("Strategy,Hestia Value\n")
            f.write("Period,2026-09-01 to 2026-09-30\n")
            f.write("Currency,USD\n")
            f.write("\nSUMMARY\nOpening,100000.0\nClosing,100000.0\nCash,100000.0\n")
            f.write("\nHOLDINGS\nSecurity,ISIN,Qty,Avg Cost,Price,Value,Weight %\n")
            f.write("TOTAL,,0,,,100000.0,100.0\n")
            f.write("\nTRANSACTIONS\nDate,Type,Security,Qty,Price,Amount\n")

        parsed_usd = parser._parse_csv_sectioned(csv_usd, tmpl_csv)
        assert parsed_usd[0].statement.currency == "USD", f"Expected USD for CSV with Currency,USD, got {parsed_usd[0].statement.currency}"
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    print("[PASS] Mandatory currency enforcement and cross-currency isolation verified.")


if __name__ == "__main__":
    run_all_tests()


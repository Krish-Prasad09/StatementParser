"""
ingestion.py
------------
Handles file ingestion, duplicate detection, scanned PDF detection,
password-protected PDF handling, three-way pre-classification of documents,
and secure .eml email unbundling.
"""

import os
import re
import hashlib
import email
from email import policy
import pypdf
from typing import Optional, Tuple, List, Dict, Any
from utils import compute_sha256


class IngestionEngine:
    def __init__(self):
        # Maps sha256 hash -> original relative file path
        self.seen_hashes: Dict[str, str] = {}
        # Soft duplicate map: (provider, client_id, period_end) -> sha256 hash
        self.seen_statements: Dict[Tuple[str, str, str], str] = {}
        self.seen_statement_meta: Dict[Tuple[str, str, str], Tuple[str, str]] = {}

    def register_or_check_duplicate(self, file_path: str, relative_name: str) -> Optional[str]:
        """
        Hard duplicate rule: Checks SHA-256 hash of file content.
        Returns the original file path if duplicate, or None if new.
        """
        sha = compute_sha256(file_path)
        if sha in self.seen_hashes:
            return self.seen_hashes[sha]
        self.seen_hashes[sha] = relative_name
        return None

    def register_or_check_revision(
        self,
        provider: str,
        client_id: str,
        period_end: str,
        file_hash: str,
        file_ext: str = "",
        data_signature: str = ""
    ) -> Optional[str]:
        """
        Soft revision rule: Checks if the same provider, client, and period was already processed
        with a different content hash.
        Returns 'revision_of_existing_statement' if revised statement, or None if new/unchanged.
        """
        if not provider or not client_id or not period_end:
            return None
        stmt_key = (provider.strip().lower(), client_id.strip().upper(), period_end.strip())
        if stmt_key in self.seen_statements:
            if self.seen_statements[stmt_key] != file_hash:
                prev_meta = self.seen_statement_meta.get(stmt_key)
                if prev_meta and file_ext and data_signature:
                    prev_ext, prev_sig = prev_meta
                    if prev_ext != file_ext and prev_sig == data_signature:
                        return None
                    if prev_ext == file_ext and prev_sig == data_signature:
                        return "same_content_different_bytes"
                return "revision_of_existing_statement"
        else:
            self.seen_statements[stmt_key] = file_hash
            self.seen_statement_meta[stmt_key] = (file_ext, data_signature)
        return None

    def check_pdf_status(self, file_path: str, candidate_password: Optional[str] = None) -> Tuple[bool, bool, str, str, Optional[str]]:
        """
        Checks PDF status for encryption, password unlocking, and text layer.
        Returns:
          (is_locked, has_text_layer, extracted_text, effective_path, issue_code)
        """
        effective_path = file_path

        # 1. Check for encryption via pypdf
        try:
            reader = pypdf.PdfReader(file_path)
            if reader.is_encrypted:
                passwords_to_try = []
                if candidate_password:
                    passwords_to_try.append(candidate_password.strip())
                env_pwds = os.environ.get("STATEMENT_PASSWORDS", "").split(",")
                for pwd in env_pwds:
                    if pwd.strip() and pwd.strip() not in passwords_to_try:
                        passwords_to_try.append(pwd.strip())

                if not passwords_to_try:
                    return True, False, "", file_path, "password_required"

                decrypted = False
                for pwd in passwords_to_try:
                    try:
                        res = reader.decrypt(pwd)
                        if res != 0:
                            decrypted = True
                            break
                    except Exception as dec_err:
                        err_str = str(dec_err).lower()
                        if any(k in err_str for k in ["cryptography", "crypto", "dependency", "aes"]):
                            return True, False, "", file_path, "pdf_crypto_dependency_missing"

                if not decrypted:
                    return True, False, "", file_path, "password_required"

                import tempfile
                writer = pypdf.PdfWriter()
                for page in reader.pages:
                    writer.add_page(page)

                with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
                    decrypted_temp_path = tf.name
                    writer.write(tf)

                effective_path = decrypted_temp_path

        except Exception as e:
            err_str = str(e).lower()
            if any(k in err_str for k in ["cryptography", "crypto", "dependency", "aes"]):
                return True, False, "", file_path, "pdf_crypto_dependency_missing"
            if "encrypted" in err_str or "password" in err_str:
                return True, False, "", file_path, "password_required"

        # 2. Extract text with pdfplumber from effective_path
        full_text = ""
        try:
            import pdfplumber
            with pdfplumber.open(effective_path) as pdf:
                for page in pdf.pages:
                    full_text += (page.extract_text() or "") + "\n"
        except Exception as e:
            err_str = str(e).lower()
            if any(k in err_str for k in ["cryptography", "crypto", "dependency", "aes"]):
                return True, False, "", effective_path, "pdf_crypto_dependency_missing"
            if "password" in err_str or "encrypted" in err_str:
                return True, False, "", effective_path, "password_required"

        clean_text = full_text.strip()
        if len(clean_text) >= 30:
            return False, True, clean_text, effective_path, None

        # Fallback to pypdf for text
        try:
            reader = pypdf.PdfReader(effective_path)
            pypdf_text = ""
            for page in reader.pages:
                pypdf_text += (page.extract_text() or "") + "\n"
            clean_pypdf = pypdf_text.strip()
            if len(clean_pypdf) >= 30:
                return False, True, clean_pypdf, effective_path, None
        except Exception:
            pass

        return False, False, "", effective_path, "no_text_layer_requires_ocr"

    def classify_document(self, text: str, filename: str) -> Tuple[str, Optional[str]]:
        """
        Three-way document classifier running BEFORE the learner:
        1. 'not_a_statement': Confident non-statement (Factsheets, Tax/Fee Invoices, Contract Notes)
        2. 'statement': Confident periodic portfolio statement
        3. 'ambiguous': Uncertain document -> routed to needs_review with 'classification_uncertain'
        Never silently drops a document that contains ISINs plus a holdings or valuation table.
        """
        lower = text.lower()
        first_lines = "\n".join(text.split("\n")[:12]).lower()

        has_isin = bool(re.search(r"\bIN[EF][A-Z0-9]{8}[0-9]\b", text))
        has_client = bool(re.search(
            r"\b(client\s*(?:id|code|name)?|folio|contributor|investor|crn|unitholder|account\s*(?:no|number|id|holder|name))\b",
            lower
        ))

        # 1. Confident Non-Statements (Factsheets without client-specific holdings, Invoices, Contract Notes)
        if re.search(r"\b(monthly\s+factsheet|fund\s+factsheet|factsheet)\b", first_lines) or (
            "this is not a client statement" in lower
        ):
            if not (has_isin and has_client):
                return "not_a_statement", None

        if re.search(r"\b(tax\s+invoice|fee\s+invoice|invoice\s+no)\b", first_lines) or (
            "invoice no:" in lower and "total payable" in lower and "holdings" not in lower
        ):
            return "not_a_statement", None

        if re.search(r"\b(contract\s+note|single-trade\s+confirmation)\b", first_lines) or (
            "single-trade confirmation. not a periodic portfolio statement" in lower
        ):
            return "not_a_statement", None

        # 2. Structural Evidence Checks
        has_date_range = bool(re.search(
            r"\b[0-9]{1,4}[-\/\.][0-9A-Za-z]{2,9}[-\/\.][0-9]{2,4}\s*(?:to|through|\-|–|—)\s*[0-9]{1,4}[-\/\.][0-9A-Za-z]{2,9}[-\/\.][0-9]{2,4}\b",
            lower
        ))
        has_period = has_date_range or bool(re.search(
            r"\b(period|reporting|window|quarter|month|as\s+on|as\s+of|statement\s+for|valuation\s+date|report\s+date|dated?|ended?)\b",
            lower
        ))
        has_portfolio = bool(re.search(
            r"\b(holdings|portfolio|net asset value|valuation|ledger|shares|units|corpus|equity|security|isin|scrip|instrument|drawdown)\b",
            lower
        ))
        has_holdings_table = bool(re.search(r"\b(holdings|valuation|portfolio|securities|scrip|market\s+value)\b", lower))
        has_summary_label = bool(re.search(
            r"\b(opening\s+(?:balance|value|corpus|nav)|closing\s+(?:balance|value|corpus|nav)|net\s+gain|capital\s+inflows|total\s+equity|market\s+value|ledger\s+summary|portfolio\s+summary)\b",
            lower
        ))
        has_numeric_table = bool(
            re.search(r"\b\d+[,.]\d{2}\b", text) and
            re.search(r"\b(qty|units|avg\s*cost|price|market\s*value|nav)\b", lower)
        )

        has_financial_substance = has_isin or has_summary_label or has_numeric_table

        # Confident periodic statement requires client + period + portfolio AND financial substance
        if (has_client and has_period and has_portfolio and has_financial_substance) or (has_isin and has_holdings_table):
            return "statement", None

        # 3. Ambiguous Document (e.g. prose letters, marketing updates) -> Fail closed to needs_review with classification_uncertain
        return "ambiguous", "classification_uncertain"

    def classify_tabular_document(self, file_path: str) -> Tuple[str, Optional[str]]:
        """
        Structural pre-classifier for tabular non-PDF files (.csv, .xlsx).
        Verifies presence of portfolio, client, and investment terms before ingestion.
        Returns ('statement', None), ('not_a_statement', None), or ('ambiguous', 'classification_uncertain').
        """
        ext = os.path.splitext(file_path)[1].lower()
        extracted_text = ""
        try:
            if ext == ".csv":
                with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                    lines = [f.readline() for _ in range(30)]
                    extracted_text = "\n".join(lines)
            elif ext == ".xlsx":
                import openpyxl
                wb = openpyxl.load_workbook(file_path, data_only=True, read_only=True)
                for sheet in wb.sheetnames[:2]:
                    ws = wb[sheet]
                    for r in list(ws.iter_rows(values_only=True))[:25]:
                        row_vals = [str(c) for c in r if c is not None]
                        extracted_text += " ".join(row_vals) + "\n"
        except Exception:
            return "ambiguous", "classification_uncertain"

        lower = extracted_text.lower()
        if any(term in lower for term in ["invoice no", "tax invoice", "vendor bill", "payroll summary", "employee list"]):
            return "not_a_statement", None

        has_financial_terms = bool(re.search(
            r"\b(portfolio|holdings?|isin|shares|units|market\s*value|current\s*value|closing|opening|corpus|nav|investment|equity|pms|aif)\b",
            lower
        ))
        has_client_or_account = bool(re.search(
            r"\b(client|investor|folio|account|pan|name|code|strategy|provider)\b",
            lower
        ))
        has_numeric_columns = bool(re.search(r"\b\d+[\.,]\d{2}\b", extracted_text))

        if has_financial_terms and (has_client_or_account or has_numeric_columns):
            return "statement", None
        elif not has_financial_terms:
            return "not_a_statement", None
        return "ambiguous", "classification_uncertain"

    def parse_eml_email(self, eml_path: str, extract_dir: str = "temp_attachments") -> Dict[str, Any]:
        """
        Parses an .eml email file, securely extracts attachments with collision-free prefixes,
        cleans up any decrypted temporary PDFs in a finally block, and determines ingestion status:
        - ingest: if email contains valid or ambiguous statement attachment(s)
        - ignore: if marketing newsletter, factsheet-only, or link with no attachment
        """
        os.makedirs(extract_dir, exist_ok=True)
        with open(eml_path, "rb") as f:
            raw_bytes = f.read()
        msg = email.message_from_bytes(raw_bytes, policy=policy.default)

        subject = msg.get("subject", "")
        sender = msg.get("from", "")
        msg_id_hdr = msg.get("message-id", "")
        msg_prefix = hashlib.sha256((msg_id_hdr or eml_path).encode("utf-8") + raw_bytes[:256]).hexdigest()[:8]

        attachments = []

        for part_idx, part in enumerate(msg.walk()):
            filename = part.get_filename()
            payload = part.get_payload(decode=True)
            if not payload:
                continue

            content_type = part.get_content_type()
            is_inline_pdf = False

            if not filename:
                # Check for inline PDF without filename
                if content_type == "application/pdf" or payload.startswith(b"%PDF"):
                    filename = f"inline_statement_{part_idx}.pdf"
                    is_inline_pdf = True
                else:
                    continue

            safe_name = os.path.basename(filename)
            save_path = os.path.join(extract_dir, f"{msg_prefix}_{part_idx}_{safe_name}")
            with open(save_path, "wb") as out_f:
                out_f.write(payload)
            attachments.append({
                "filename": safe_name,
                "path": save_path,
                "size": len(payload),
                "is_inline_pdf": is_inline_pdf
            })

        eff_msg_id = msg_id_hdr.strip() or f"local_{msg_prefix}"
        msg_date = str(msg.get("date", ""))

        if not attachments:
            return {
                "status": "ignore",
                "subject": subject,
                "sender": sender,
                "message_id": eff_msg_id,
                "date": msg_date,
                "reason": "portal_link_no_attachment",
                "attachments": []
            }

        review_reasons = []
        has_valid_statement = False
        has_ambiguous_statement = False
        confident_non_statement_count = 0

        for att in attachments:
            att_path = att["path"]
            ext = os.path.splitext(att["filename"])[1].lower()

            if att.get("is_inline_pdf"):
                review_reasons.append("inline_pdf_missing_filename")

            if ext == ".zip":
                review_reasons.append("zip_attachment_requires_manual_review")
            elif ext in [".xls", ".xlsm"]:
                review_reasons.append("unsupported_statement_attachment_type")
            elif ext in [".xlsx", ".csv"]:
                tab_status, _ = self.classify_tabular_document(att_path)
                if tab_status == "statement":
                    has_valid_statement = True
                elif tab_status == "not_a_statement":
                    confident_non_statement_count += 1
                else:
                    has_ambiguous_statement = True
            elif ext == ".pdf":
                eff_path = att_path
                try:
                    is_locked, has_text, text, eff_path, _ = self.check_pdf_status(att_path)
                    if is_locked or not has_text:
                        has_valid_statement = True
                    else:
                        doc_status, _ = self.classify_document(text, att["filename"])
                        if doc_status == "statement":
                            has_valid_statement = True
                        elif doc_status == "ambiguous":
                            has_ambiguous_statement = True
                        elif doc_status == "not_a_statement":
                            confident_non_statement_count += 1
                finally:
                    if eff_path and eff_path != att_path and os.path.exists(eff_path):
                        try:
                            os.remove(eff_path)
                        except Exception:
                            pass
            else:
                review_reasons.append("ambiguous_attachment_requires_review")

        if has_valid_statement:
            status = "ingest"
            reason = ""
        elif review_reasons:
            status = "needs_review"
            reason = review_reasons[0]
        elif has_ambiguous_statement:
            status = "needs_review"
            reason = "classification_uncertain"
        elif confident_non_statement_count == len(attachments):
            status = "ignore"
            reason = "newsletter_with_factsheet"
        else:
            status = "needs_review"
            reason = "ambiguous_attachment_requires_review"

        return {
            "status": status,
            "subject": subject,
            "sender": sender,
            "message_id": eff_msg_id,
            "date": msg_date,
            "reason": reason,
            "attachments": attachments
        }

    def connect_and_fetch_imap_mailbox(
        self,
        host: str = "imap.gmail.com",
        username: str = "",
        password: str = "",
        folder: str = "INBOX",
        extract_dir: str = "temp_attachments"
    ) -> List[Dict[str, Any]]:
        """
        Connects to a mailbox over IMAP SSL, scans for messages using BODY.PEEK,
        extracts attachments, and runs parse_eml_email.
        """
        import imaplib
        if not username or not password:
            return []

        os.makedirs(extract_dir, exist_ok=True)
        results = []
        try:
            mail = imaplib.IMAP4_SSL(host)
            mail.login(username, password)
            mail.select(folder)

            status, search_data = mail.search(None, "UNSEEN")
            if status == "OK" and search_data[0]:
                for msg_id in search_data[0].split():
                    res, msg_data = mail.fetch(msg_id, "(BODY.PEEK[])")
                    if res == "OK":
                        raw_email = msg_data[0][1]
                        temp_eml = os.path.join(extract_dir, f"msg_{msg_id.decode()}.eml")
                        with open(temp_eml, "wb") as f:
                            f.write(raw_email)
                        eml_result = self.parse_eml_email(temp_eml, extract_dir=extract_dir)
                        results.append(eml_result)
            mail.logout()
        except Exception as e:
            results.append({"status": "error", "error": str(e)})

        return results

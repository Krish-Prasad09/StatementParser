"""
pipeline.py
-----------
Unified end-to-end processing pipeline for PMS and AIF statement parsing.
Orchestrates:
1. Ingestion Gatekeeper (SHA256 deduplication, password protection check, scanned PDF detection, three-way classification)
2. Structural Fingerprinting (autonomous format recognition & layout drift detection)
3. One-shot Learning (learns reusable template on first encounter, 0 LLM calls subsequently)
4. Deterministic Parsing (zero LLM calls for known formats)
5. Financial Auditing & Validation (audits mathematical balances, roll-forwards, weights, and ISIN checksums)
"""

import os
import json
import hashlib
from typing import Optional, Dict, Any, List

from models import ExtractionResult, StatementRecord
from ingestion import IngestionEngine
from fingerprinter import FormatFingerprinter
from learner import FormatLearner
from parser import StatementParser
from validator import FinancialValidator
from utils import compute_sha256


class ProcessingPipeline:
    """The central pipeline for statement processing."""

    def __init__(self, templates_dir: str = "templates", db_path: Optional[str] = None):
        self.templates_dir = templates_dir
        self.db_path = db_path
        self.ingestion = IngestionEngine()
        self.learner = FormatLearner(templates_dir=templates_dir, db_path=db_path)
        self.parser = StatementParser()
        self.validator = FinancialValidator()
        self.processed_history: Dict[str, ExtractionResult] = {}
        self.store = None
        if self.db_path:
            from persistence import StatementStore
            self.store = StatementStore(self.db_path)
            self.store.sync_to_pipeline(self)
        self._load_templates()

    def _load_templates(self):
        """Loads all existing templates into fingerprinter."""
        templates_dict = {}
        if os.path.exists(self.templates_dir):
            for fname in os.listdir(self.templates_dir):
                if fname.endswith(".json"):
                    path = os.path.join(self.templates_dir, fname)
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        fmt_id = data.get("format_id", fname[:-5])
                        templates_dict[fmt_id] = data
        self.fingerprinter = FormatFingerprinter(templates_dict=templates_dict)

    def approve_template(self, format_id: str) -> bool:
        """Approves/promotes a template so subsequent statements of that format parse cleanly with 0 LLM calls."""
        tmpl_path = os.path.join(self.templates_dir, f"{format_id}.json")
        updated = False
        if os.path.exists(tmpl_path):
            try:
                with open(tmpl_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                data["approved"] = True
                with open(tmpl_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
                updated = True
            except Exception:
                pass
        self._load_templates()
        if format_id in self.fingerprinter.templates:
            self.fingerprinter.templates[format_id]["approved"] = True
            updated = True
        return updated

    promote_template = approve_template

    def process_file(self, file_path: str, password: Optional[str] = None) -> ExtractionResult:
        """
        Processes a file end-to-end and returns an ExtractionResult.
        Operates completely autonomously from document features without relying on external hints.
        """
        filename = os.path.basename(file_path)
        rel_name = file_path.replace("sample_data/", "").replace("sample_data\\", "").replace("\\", "/")
        effective_path = file_path
        temp_decrypted_created = False

        if self.store is not None:
            self.store.sync_to_pipeline(self)

        try:
            # -------------------------------------------------------------
            # STEP 1: Ingestion Gatekeeper
            # -------------------------------------------------------------
            ext = os.path.splitext(file_path)[1].lower()
            raw_text = ""

            if ext == ".pdf":
                is_locked, has_text, raw_text, effective_path, issue_code = self.ingestion.check_pdf_status(
                    file_path, candidate_password=password
                )
                if effective_path != file_path:
                    temp_decrypted_created = True

                # Password protection check
                if is_locked:
                    raw_hash = compute_sha256(file_path) if os.path.exists(file_path) else ""
                    res = ExtractionResult(
                        file=rel_name,
                        format_id="none",
                        status="needs_review",
                        issues=[issue_code or "password_required"],
                        statements=[],
                        llm_calls_made=0,
                        sha256_hash=raw_hash
                    )
                    self.processed_history[rel_name] = res
                    return res

                curr_hash = compute_sha256(effective_path) if os.path.exists(effective_path) else ""

                # Scanned image-only check
                if not has_text:
                    res = ExtractionResult(
                        file=rel_name,
                        format_id="none",
                        status="needs_review",
                        issues=[issue_code or "no_text_layer_requires_ocr"],
                        statements=[],
                        llm_calls_made=0,
                        sha256_hash=curr_hash
                    )
                    self.processed_history[rel_name] = res
                    return res

                # Three-way document pre-classifier (statement | not_a_statement | ambiguous)
                doc_status, doc_issue = self.ingestion.classify_document(raw_text, filename)
                if doc_status == "not_a_statement":
                    res = ExtractionResult(
                        file=rel_name,
                        format_id="none",
                        status="not_a_statement",
                        issues=[],
                        statements=[],
                        llm_calls_made=0,
                        sha256_hash=curr_hash
                    )
                    self.processed_history[rel_name] = res
                    return res
                elif doc_status == "ambiguous":
                    res = ExtractionResult(
                        file=rel_name,
                        format_id="none",
                        status="needs_review",
                        issues=[doc_issue or "classification_uncertain"],
                        statements=[],
                        llm_calls_made=0,
                        sha256_hash=curr_hash
                    )
                    self.processed_history[rel_name] = res
                    return res
            elif ext in [".xlsx", ".csv"]:
                curr_hash = compute_sha256(effective_path) if os.path.exists(effective_path) else ""
                tab_status, tab_issue = self.ingestion.classify_tabular_document(effective_path)
                if tab_status == "not_a_statement":
                    res = ExtractionResult(
                        file=rel_name,
                        format_id="non_statement_document",
                        status="not_a_statement",
                        issues=[],
                        statements=[],
                        llm_calls_made=0,
                        sha256_hash=curr_hash
                    )
                    self.processed_history[rel_name] = res
                    return res
                elif tab_status == "ambiguous":
                    res = ExtractionResult(
                        file=rel_name,
                        format_id="none",
                        status="needs_review",
                        issues=[tab_issue or "classification_uncertain"],
                        statements=[],
                        llm_calls_made=0,
                        sha256_hash=curr_hash
                    )
                    self.processed_history[rel_name] = res
                    return res
            else:
                curr_hash = compute_sha256(effective_path) if os.path.exists(effective_path) else ""

            # Deduplication check via SHA-256 content hash of effective content
            dup_of = self.ingestion.register_or_check_duplicate(effective_path, rel_name)
            if dup_of:
                orig_result = self.processed_history.get(dup_of)
                statements = orig_result.statements if orig_result else []
                fmt_id = orig_result.format_id if orig_result else "duplicate_statement"
                res = ExtractionResult(
                    file=rel_name,
                    format_id=fmt_id,
                    status="duplicate",
                    issues=[f"identical_to:{dup_of}"],
                    statements=statements,
                    llm_calls_made=0,
                    sha256_hash=curr_hash
                )
                return res

            # -------------------------------------------------------------
            # STEP 2: Structural Fingerprinting & Format Matching
            # -------------------------------------------------------------
            features = self.fingerprinter.extract_document_features(effective_path, raw_text=raw_text)
            matched_format_id, template_dict, is_drift = self.fingerprinter.match_template(features)

            # -------------------------------------------------------------
            # STEP 3: One-shot Learning / Drift Handling
            # -------------------------------------------------------------
            llm_calls = 0

            if is_drift:
                v2_format_id = matched_format_id if (matched_format_id and matched_format_id.endswith("_v2")) else f"{matched_format_id or 'format'}_v2"
                if self.learner.has_template(v2_format_id):
                    template_dict = self.learner.load_template(v2_format_id)
                    matched_format_id = v2_format_id
                else:
                    learned_tmpl, calls = self.learner.learn_format(
                        v2_format_id, effective_path, features, approved=False
                    )
                    llm_calls = calls
                    if learned_tmpl:
                        template_dict = learned_tmpl
                        matched_format_id = v2_format_id
                        self._load_templates()
            elif not template_dict:
                target_id = matched_format_id or features.get("provider_key")
                template_dict, calls = self.learner.learn_format(target_id, effective_path, features)
                llm_calls = calls
                if template_dict:
                    matched_format_id = template_dict.get("format_id", target_id)
                    self._load_templates()
                else:
                    failure_issue = getattr(self.learner, "last_failure_issue", "learning_failed_llm_unavailable")
                    diag_msg = getattr(self.learner, "last_diagnostic_message", "")
                    res = ExtractionResult(
                        file=rel_name,
                        format_id=matched_format_id or "unknown_format",
                        status="needs_review",
                        issues=[failure_issue],
                        statements=[],
                        notes=diag_msg,
                        diagnostic_message=diag_msg,
                        llm_calls_made=llm_calls,
                        sha256_hash=curr_hash
                    )
                    self.processed_history[rel_name] = res
                    return res

            # -------------------------------------------------------------
            # STEP 4: Deterministic Parsing (0 LLM Calls)
            # -------------------------------------------------------------
            records: List[StatementRecord] = []
            try:
                records = self.parser.parse(effective_path, template_dict)
            except Exception as e:
                res = ExtractionResult(
                    file=rel_name,
                    format_id=matched_format_id or "unknown_format",
                    status="needs_review",
                    issues=[f"parsing_exception:{str(e)}"],
                    statements=[],
                    llm_calls_made=llm_calls,
                    sha256_hash=curr_hash
                )
                self.processed_history[rel_name] = res
                return res

            # -------------------------------------------------------------
            # STEP 5: Financial Auditing & Validation
            # -------------------------------------------------------------
            status, issues = self.validator.validate(
                file_path=effective_path,
                records=records,
                is_drift=is_drift,
                template=template_dict
            )

            # Soft revision check: Same (provider, client_id, period_end) with different content hash
            file_ext = os.path.splitext(effective_path)[1].lower()
            for rec in records:
                rev_provider = rec.statement.provider or matched_format_id
                fin_payload = {
                    "summary": rec.summary.model_dump(),
                    "holdings": [h.model_dump() for h in rec.holdings],
                    "transactions": [t.model_dump() for t in rec.transactions],
                }
                data_sig = hashlib.sha256(json.dumps(fin_payload, sort_keys=True).encode("utf-8")).hexdigest()
                rev_issue = self.ingestion.register_or_check_revision(
                    rev_provider,
                    rec.statement.client_id,
                    rec.statement.period_end,
                    curr_hash,
                    file_ext=file_ext,
                    data_signature=data_sig
                )
                if rev_issue:
                    if rev_issue == "same_content_different_bytes":
                        status = "duplicate"
                        issues = ["same_content_different_bytes"]
                    elif status == "validated":
                        if rev_issue not in issues:
                            issues.append(rev_issue)
                        status = "needs_review"

            if "incomplete_document_missing_continuation_page" in issues:
                records = []

            res = ExtractionResult(
                file=rel_name,
                format_id=matched_format_id,
                status=status,
                issues=issues,
                statements=records,
                llm_calls_made=llm_calls,
                sha256_hash=curr_hash
            )

            self.processed_history[rel_name] = res
            return res

        finally:
            if temp_decrypted_created and effective_path and os.path.exists(effective_path):
                try:
                    os.remove(effective_path)
                except Exception:
                    pass

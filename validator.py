"""
validator.py
------------
Financial integrity validator and auditor.
Performs deterministic multi-point checks:
- Document completeness (missing continuation pages)
- Fail closed (reject empty statements or missing client metadata)
- ISIN Luhn checksum (ISO 6166 Mod-10)
- Stated equity vs sum of holdings
- Summary roll-forward (Opening + Inflows - Outflows + Gain - Fees == Closing)
- Closing corpus vs (Holdings + Cash) / Longs + Shorts + Cash
- Holding weights sum reconciliation (~100% tolerance)
- Per-row Quantity x Price == Market Value verification
- Transaction amount cross-checks (Trade amount == Qty x Price)
- AIF Units x NAV per unit == Total NAV verification
"""

import os
import re
from datetime import datetime
from typing import List, Tuple, Optional, Dict, Any
import pdfplumber

from models import StatementRecord
from utils import is_valid_isin, normalize_currency


class FinancialValidator:
    """Audits extracted statements and document integrity."""

    def check_document_completeness(self, file_path: str) -> Optional[str]:
        """Checks if a PDF statement is missing continuation pages."""
        if not file_path.lower().endswith(".pdf"):
            return None

        try:
            with pdfplumber.open(file_path) as pdf:
                actual_pages = len(pdf.pages)
                for page_idx, page in enumerate(pdf.pages):
                    text = page.extract_text() or ""

                    # Check footer page pattern: e.g. "Page 1 of 2"
                    m = re.search(r"Page\s+(\d+)\s+of\s+(\d+)", text, re.IGNORECASE)
                    if m:
                        curr_p, total_p = int(m.group(1)), int(m.group(2))
                        if total_p > actual_pages:
                            return "incomplete_document_missing_continuation_page"

                    # Check for explicit continuation text on last page
                    if page_idx == actual_pages - 1:
                        if "...continued on next page" in text.lower() or "continued on page" in text.lower():
                            return "incomplete_document_missing_continuation_page"
        except Exception:
            pass

        return None

    # Financial Audit Tolerances
    MONEY_TOLERANCE = 1.00          # 1 INR tolerance for monetary sums and roundings
    WEIGHT_SUM_TOLERANCE = 0.50     # 0.5% tolerance for weights sum
    TOTAL_WEIGHT_TOLERANCE = 0.50   # 0.5% tolerance for total weights sum
    HOLDING_WEIGHT_TOLERANCE = 0.10 # 0.1% tolerance for per-holding weight vs market_value / total
    QTY_PRICE_TOLERANCE_PCT = 0.005 # 0.5% tolerance for per-row price verification
    NAV_PER_UNIT_TOLERANCE = 1.00   # 1 INR tolerance for units * nav_per_unit

    SOFT_ISSUES = {"template_missing_row_labels"}

    def validate(
        self,
        file_path: str,
        records: List[StatementRecord],
        is_drift: bool = False,
        template: Optional[Dict[str, Any]] = None
    ) -> Tuple[str, List[str]]:
        """
        Audits extracted statement records.
        Returns (status, list_of_issues).
        status: 'validated' | 'needs_review'
        """
        issues: List[str] = []

        # 1. Document Completeness Check
        comp_issue = self.check_document_completeness(file_path)
        if comp_issue:
            issues.append(comp_issue)
            return "needs_review", issues

        # 2. Layout Drift Check
        if is_drift:
            issues.append("layout_drift_known_provider")

        # 3. Fail closed if no records extracted
        if not records:
            issues.append("no_statement_records_extracted")
            return "needs_review", issues

        tmpl_holdings_cfg = template.get("holdings", {}) if template else {}
        is_unlisted = tmpl_holdings_cfg.get("is_unlisted", template.get("is_unlisted", False) if template else False)
        supports_short = tmpl_holdings_cfg.get("supports_short", template.get("supports_short", False) if template else False)
        tmpl_provider = (template.get("fields", {}).get("provider") or "").strip() if template else ""

        # 4. Financial Integrity Checks per StatementRecord
        for record in records:
            meta = record.statement
            summary = record.summary
            holdings = record.holdings
            txns = record.transactions

            # Check A0: Provider mismatch against template
            if tmpl_provider and meta.provider:
                from fingerprinter import compute_string_similarity
                if compute_string_similarity(meta.provider, tmpl_provider) < 0.75:
                    if "provider_mismatch_template" not in issues:
                        issues.append("provider_mismatch_template")

            # Check A1: Missing mapped columns detected during table extraction
            tmpl_expected = []
            if template:
                tmpl_expected = [str(c).lower().strip() for c in template.get("holdings", {}).get("mapping", {}).values()]
                tmpl_expected += [str(c).lower().strip() for c in template.get("summary", {}).get("mapping", {}).values()]

            missing_cols = getattr(summary, "missing_columns", None) or []
            for mc in missing_cols:
                col_name = mc.split(":", 1)[1].strip().lower() if ":" in mc else mc.lower().strip()
                if not tmpl_expected or col_name in tmpl_expected:
                    if mc not in issues:
                        issues.append(mc)

            # Check A: Required metadata (Fail closed)
            tmpl_fields = template.get("fields", {}) if template else {}
            if not meta.client_id or not meta.period_end or not meta.provider or meta.provider.strip().lower() in ("unknown", "unknown provider"):
                if "missing_required_metadata" not in issues:
                    issues.append("missing_required_metadata")
            elif not meta.strategy:
                if "missing_required_metadata" not in issues:
                    issues.append("missing_required_metadata")

            # Check A0: Mandatory currency extraction & validation (Fail closed on missing/unverified currency)
            normalized_curr = normalize_currency(meta.currency)
            if not meta.currency or not normalized_curr:
                if "missing_currency" not in issues:
                    issues.append("missing_currency")
                if "missing_required_metadata" not in issues:
                    issues.append("missing_required_metadata")
            else:
                meta.currency = normalized_curr


            # Check A2: Metadata sanity check (single line <= 80 chars, no newline, no amount pattern)
            for m_field in ("client_id", "client_name", "strategy", "provider"):
                m_val = getattr(meta, m_field, None)
                if m_val:
                    m_val_str = str(m_val)
                    if "\n" in m_val_str or "\r" in m_val_str or len(m_val_str) > 80:
                        err_code = f"metadata_value_suspect:{m_field}"
                        if err_code not in issues:
                            issues.append(err_code)
                    elif re.search(r"\b\d{1,3}(?:,\d{2,3})+(?:\.\d+)?\b", m_val_str) or re.search(r"^\s*[\d,]+\.\d+\s*$", m_val_str):
                        err_code = f"metadata_value_suspect:{m_field}"
                        if err_code not in issues:
                            issues.append(err_code)

            # Check B: Empty balances & zero holdings (Fail closed)
            if summary.closing == 0.0 and summary.opening == 0.0 and not holdings:
                if "empty_statement_zero_balances" not in issues:
                    issues.append("empty_statement_zero_balances")

            # Check C: ISIN Luhn Checksum validation
            for h in holdings:
                if h.isin and not is_valid_isin(h.isin):
                    if "invalid_isin_checksum" not in issues:
                        issues.append("invalid_isin_checksum")

            for t in txns:
                if t.isin and not is_valid_isin(t.isin):
                    if "invalid_isin_checksum" not in issues:
                        issues.append("invalid_isin_checksum")

            # Check D: Duplicate ISIN detection in holdings
            seen_isins = set()
            for h in holdings:
                if h.isin:
                    if h.isin in seen_isins:
                        if "duplicate_holding_isin" not in issues:
                            issues.append("duplicate_holding_isin")
                    seen_isins.add(h.isin)

            # Check E: Missing ISIN for listed holdings
            if not is_unlisted:
                for h in holdings:
                    if not h.isin and h.name and not any(k in h.name.lower() for k in ["cash", "margin", "liquidity", "treasury"]):
                        if "missing_isin_for_listed_holding" not in issues:
                            issues.append("missing_isin_for_listed_holding")

            # Check F: Holdings Sum vs Stated Total Equity
            sum_holdings = sum(h.market_value for h in holdings)
            stated_equity = getattr(summary, "stated_equity", None)
            if stated_equity is not None:
                if abs(sum_holdings - stated_equity) > self.MONEY_TOLERANCE:
                    if "holdings_sum_ne_stated_total_equity" not in issues:
                        issues.append("holdings_sum_ne_stated_total_equity")

            # Check G: Summary Roll-forward Check
            # Opening + Inflow - Outflow + Gain - Fee == Closing
            if summary.gain is not None:
                expected_closing = (
                    summary.opening
                    + summary.inflow
                    - summary.outflow
                    + summary.gain
                    - summary.fee
                )
                if abs(expected_closing - summary.closing) > self.MONEY_TOLERANCE:
                    if "summary_rollforward_mismatch" not in issues:
                        issues.append("summary_rollforward_mismatch")

            # Check H: Closing Value vs (Holdings Total + Cash)
            expected_total = sum_holdings + summary.cash
            if abs(expected_total - summary.closing) > 5.0:
                if "closing_value_ne_holdings_total" not in issues:
                    issues.append("closing_value_ne_holdings_total")

            # Check H2: Summary-table cash vs holdings cash-row cash when both are printed
            cash_from_summary = getattr(summary, "cash_from_summary", None)
            cash_from_row = getattr(summary, "cash_from_row", None)
            if cash_from_summary is not None and cash_from_row is not None:
                if abs(cash_from_summary - cash_from_row) > self.MONEY_TOLERANCE:
                    if "cash_summary_vs_row_mismatch" not in issues:
                        issues.append("cash_summary_vs_row_mismatch")

            # Check I: Cat III Long/Short Specific Audits
            if supports_short:
                stated_gl = getattr(summary, "stated_gross_long", None)
                stated_gs = getattr(summary, "stated_gross_short", None)
                longs_sum = sum(h.market_value for h in holdings if h.market_value > 0)
                shorts_sum = sum(h.market_value for h in holdings if h.market_value < 0)
                if stated_gl is not None and abs(longs_sum - stated_gl) > self.MONEY_TOLERANCE:
                    if "gross_long_mismatch" not in issues:
                        issues.append("gross_long_mismatch")
                if stated_gs is not None and abs(shorts_sum - stated_gs) > self.MONEY_TOLERANCE:
                    if "gross_short_mismatch" not in issues:
                        issues.append("gross_short_mismatch")
                if summary.cash == 0.0:
                    if "cash_row_not_found" not in issues:
                        issues.append("cash_row_not_found")

            # Check J: Weights Sum & Holding Weights Reconciliation
            weights = [h.weight_pct for h in holdings if h.weight_pct is not None]
            if weights:
                sum_w = sum(weights)
                cash_pct = (summary.cash / summary.closing * 100.0) if summary.closing > 0 else 0.0
                diff_without_cash = abs(sum_w - 100.0)
                diff_with_cash = abs((sum_w + cash_pct) - 100.0)
                if diff_without_cash > self.TOTAL_WEIGHT_TOLERANCE and diff_with_cash > self.TOTAL_WEIGHT_TOLERANCE:
                    if "holdings_weight_sum_invalid" not in issues:
                        issues.append("holdings_weight_sum_invalid")

                total_holdings = sum(h.market_value for h in holdings)
                for h in holdings:
                    if h.weight_pct is not None and h.market_value is not None:
                        err_equity = abs(h.weight_pct - ((h.market_value / total_holdings) * 100.0)) if total_holdings > 0 else 999.0
                        err_closing = abs(h.weight_pct - ((h.market_value / summary.closing) * 100.0)) if summary.closing > 0 else 999.0
                        if min(err_equity, err_closing) > self.HOLDING_WEIGHT_TOLERANCE:
                            if "holding_weight_mismatch" not in issues:
                                issues.append("holding_weight_mismatch")

            # Check K: Per-row Holding Qty x Price == Market Value
            for h in holdings:
                if h.quantity and h.price and h.market_value:
                    expected_mv = abs(h.quantity) * h.price
                    if abs(abs(h.market_value) - expected_mv) > max(1.0, expected_mv * self.QTY_PRICE_TOLERANCE_PCT):
                        if "holding_qty_price_mismatch" not in issues:
                            issues.append("holding_qty_price_mismatch")

            # Check L: Transaction Checks
            inflow_tx_sum = 0.0
            outflow_tx_sum = 0.0
            fee_tx_sum = 0.0

            for t in txns:
                # Unmapped type check
                if t.type not in ("buy", "sell", "dividend", "fee", "inflow", "outflow"):
                    if "unmapped_transaction_type" not in issues:
                        issues.append("unmapped_transaction_type")

                # Accumulate for summary cross-check
                if t.type == "inflow":
                    inflow_tx_sum += t.amount
                elif t.type == "outflow":
                    outflow_tx_sum += t.amount
                elif t.type == "fee":
                    fee_tx_sum += t.amount

                # Trade amount check: Qty x Price == Amount
                if t.type in ("buy", "sell") and t.quantity and t.price and t.amount:
                    expected_amt = t.quantity * t.price
                    if abs(t.amount - expected_amt) > max(1.0, expected_amt * self.QTY_PRICE_TOLERANCE_PCT):
                        if "transaction_amount_mismatch" not in issues:
                            issues.append("transaction_amount_mismatch")

                # Transaction date within statement period
                if meta.period_start and meta.period_end and t.date:
                    if t.date < meta.period_start or t.date > meta.period_end:
                        if "transaction_date_outside_period" not in issues:
                            issues.append("transaction_date_outside_period")

            # Check M: Cross-check transactions vs summary figures
            if summary.inflow > 0 or inflow_tx_sum > 0:
                if summary.inflow > 0 and len([t for t in txns if t.type == "inflow"]) == 0:
                    if "missing_transactions_for_summary_flow" not in issues:
                        issues.append("missing_transactions_for_summary_flow")
                    if "transactions_inflow_mismatch" not in issues:
                        issues.append("transactions_inflow_mismatch")
                elif abs(inflow_tx_sum - summary.inflow) > self.MONEY_TOLERANCE:
                    if "transactions_inflow_mismatch" not in issues:
                        issues.append("transactions_inflow_mismatch")

            if summary.outflow > 0 or outflow_tx_sum > 0:
                if summary.outflow > 0 and len([t for t in txns if t.type == "outflow"]) == 0:
                    if "missing_transactions_for_summary_flow" not in issues:
                        issues.append("missing_transactions_for_summary_flow")
                    if "transactions_outflow_mismatch" not in issues:
                        issues.append("transactions_outflow_mismatch")
                elif abs(outflow_tx_sum - summary.outflow) > self.MONEY_TOLERANCE:
                    if "transactions_outflow_mismatch" not in issues:
                        issues.append("transactions_outflow_mismatch")

            if summary.fee > 0 or fee_tx_sum > 0:
                if abs(fee_tx_sum - summary.fee) > self.MONEY_TOLERANCE:
                    if "transactions_fee_mismatch" not in issues:
                        issues.append("transactions_fee_mismatch")

            # Check N: AIF Units x NAV per unit == Total Closing NAV
            if summary.units and summary.nav_per_unit and summary.closing > 0:
                expected_nav = round(summary.units * summary.nav_per_unit, 2)
                if abs(expected_nav - summary.closing) > self.NAV_PER_UNIT_TOLERANCE:
                    if "units_times_nav_per_unit_mismatch" not in issues:
                        issues.append("units_times_nav_per_unit_mismatch")

            # Check O: Long-only template cannot have negative holding quantities
            if not supports_short:
                for h in holdings:
                    if h.quantity < 0:
                        if "negative_quantity_in_long_only_template" not in issues:
                            issues.append("negative_quantity_in_long_only_template")

            # Check P: Period range validity (period_start <= period_end)
            if meta.period_start and meta.period_end:
                if meta.period_start > meta.period_end:
                    if "invalid_period_range" not in issues:
                        issues.append("invalid_period_range")

            # Check Q: Collect any non-blocking soft issues from summary
            soft_list = getattr(summary, "soft_issues", []) or []
            for si in soft_list:
                if si not in issues:
                    issues.append(si)

        blocking_issues = [i for i in issues if i not in self.SOFT_ISSUES]
        status = "needs_review" if blocking_issues else "validated"
        return status, issues

"""
parser.py
---------
Deterministic extraction engine for PMS and AIF statements.
Parses documents with 0 LLM calls using declarative format templates.
Handles:
- ruled_table
- monospace_text
- ruled_lines_horizontal_only
- xlsx
- csv_sectioned
"""

import os
import re
import csv
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple
import pdfplumber
import openpyxl

from models import StatementMeta, Summary, Holding, Transaction, StatementRecord
from utils import clean_number, parse_date, normalise_header, match_header_with_fuzzy, normalize_currency


def strip_leading_label(val: Optional[str]) -> Optional[str]:
    if not val or not isinstance(val, str):
        return val
    s = val.strip()
    s = re.sub(
        r"^(?:(?:Scheme\s+Name|Scheme|PMS\s+Approach|Approach|Strategy\s+Name|Strategy|Mandate|Plan|Portfolio|Client\s+ID|Client\s+Name|Investor\s+Name|Investor|Name|Account\s+Holder|Account\s+No|Folio|PAN|Tax\s+ID|Provider)\s*[:\s|-]*)+",
        "",
        s,
        flags=re.IGNORECASE
    ).strip()
    return s


def resolve_isin_from_holdings(security_name: Optional[str], holdings: List[Holding]) -> Optional[str]:
    """Resolves ISIN by matching security name against holdings in the same document."""
    if not security_name:
        return None
    sec_norm = re.sub(r"[^\w\s]", "", security_name.lower()).strip()
    for h in holdings:
        if not h.isin:
            continue
        h_norm = re.sub(r"[^\w\s]", "", h.name.lower()).strip()
        if sec_norm == h_norm or sec_norm in h_norm or h_norm in sec_norm:
            return h.isin
    return None


def safe_num(val: Any, default: float = 0.0) -> float:
    """Safely converts val to float using clean_number, returning default if None."""
    num = clean_number(val)
    return float(num) if num is not None else default


def normalize_provider_title(raw_line: str) -> str:
    """Normalizes top document header line into a clean provider name."""
    line = raw_line.strip()
    if not line:
        return ""
    if line.isupper():
        acronyms = {"PMS", "AIF", "LLP", "AMC", "NRI", "HNI", "II", "III", "IV"}
        words = line.split()
        return " ".join(w if w in acronyms else w.capitalize() for w in words)
    return line


def _col_matches_label(col_name: str, label: str) -> bool:
    """Checks if a normalized table column header matches a template mapping label."""
    if not col_name or not label:
        return False
    from difflib import SequenceMatcher
    nc = normalise_header(col_name)
    nl = normalise_header(label)
    if nc == nl:
        return True
    if SequenceMatcher(None, nc, nl).ratio() >= 0.9:
        return True
    return False


COMPANY_KEYWORD_PAT = re.compile(
    r"\b(Capital|Wealth|Securities|Alternatives|Partners|Advisors|Managers|Asset Management|Investments|Portfolio Services|Hedge|Equities|Funds|PMS|AIF|Limited|Ltd)\b",
    re.IGNORECASE
)
NOISE_LINE_PAT = re.compile(
    r"\b(Statement|Report|Generated|Confidential|Strictly Private|Page\s+\d+|Reporting Window|Quarter:|As on|As of|Client Name|PAN|Folio|Account No|Investor Code|Unitholder ID)\b",
    re.IGNORECASE
)


def find_provider_in_text(text: str, default_fallback: str = "Unknown Provider") -> str:
    """Extracts provider name from document text, skipping top banners and noise lines."""
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    if not lines:
        return default_fallback

    # 1. First line matching company keywords and not noise
    for l in lines[:10]:
        if NOISE_LINE_PAT.search(l) or "confidential" in l.lower() or "generated" in l.lower():
            continue
        if COMPANY_KEYWORD_PAT.search(l):
            return normalize_provider_title(l)

    # 2. Look near SEBI Regn line
    for l in lines[:15]:
        if "sebi" in l.lower():
            before_sebi = re.split(r"[,|\-]?\s*SEBI", l, flags=re.IGNORECASE)[0].strip()
            if before_sebi and COMPANY_KEYWORD_PAT.search(before_sebi) and not NOISE_LINE_PAT.search(before_sebi):
                return normalize_provider_title(before_sebi)
# ABC Capital Partners | SEBI Registration No. INM000012345            

    # 3. First non-noise line
    for l in lines[:10]:
        if not NOISE_LINE_PAT.search(l) and "confidential" not in l.lower() and "generated" not in l.lower():
            return normalize_provider_title(l)

    return default_fallback


def extract_metadata_from_text(text: str, template: Dict[str, Any], raw_crop_left: str = "", raw_crop_right: str = "") -> StatementMeta:
    """Extracts provider, client identity, strategy, and statement period from document text using template rules."""
    fields = template.get("fields", {})

    # Take provider from the document text first; template provider field is only a fallback
    provider_name = ""
    prov_pat = fields.get("provider_pattern")
    if prov_pat:
        m_prov = re.search(prov_pat, text, re.IGNORECASE)
        if m_prov:
            provider_name = normalize_provider_title(m_prov.group(1) if m_prov.lastindex else m_prov.group(0))
    if not provider_name:
        fallback_prov = fields.get("provider", "Unknown Provider")
        provider_name = find_provider_in_text(text, default_fallback=fallback_prov)

    # Combined text for regex extraction
    search_text = f"{text}\n{raw_crop_left}\n{raw_crop_right}"

    # Client ID
    client_id = ""
    default_cid_pat = r"(?:CLIENT ID|Client ID|FOLIO\s*/\s*CLIENT CODE|ACCOUNT NO|INVESTOR CODE|UNITHOLDER ID|CLIENT CODE)[ \t]*[:\s][ \t]*([A-Za-z0-9\/\-]+)"
    client_id_pat = fields.get("client_id_pattern") or default_cid_pat
    m = re.search(client_id_pat, search_text, re.IGNORECASE)
    if not m and client_id_pat != default_cid_pat:
        m = re.search(default_cid_pat, search_text, re.IGNORECASE)
    if m:
        client_id = m.group(1).strip()
# For example, from CLIENT ID: ABC123, it stores ABC123 in client_id.

    # Client Name
    client_name = ""
    default_cname_pat = r"(?:Client Name|Investor Name|Account Name|Account Holder|Investor(?!\s+Code)|Name)[ \t]*[:\s]?[ \t]+([^\r\n|]+)"
    client_name_pat = fields.get("client_name_pattern") or default_cname_pat
    m = re.search(client_name_pat, search_text, re.IGNORECASE)
    if not m and client_name_pat != default_cname_pat:
        m = re.search(default_cname_pat, search_text, re.IGNORECASE)
    if m:
        cname = m.group(1).strip()
        cname = re.split(r"(?:Strategy|PMS Approach|Portfolio|PAN|Scheme|Series|Status|Address|Mandate|Tax ID|Plan|Class|Commitment)", cname, flags=re.IGNORECASE)[0].strip()
# Rahul Sharma PAN ABCDE1234F stripped to Rahul Sharma
       
        cname = re.sub(r"^(?:Account Holder|Investor Name|Client Name|Investor|Name)[ \t]*[:\s]?", "", cname, flags=re.IGNORECASE).strip()
        client_name = cname

    # PAN
    pan = None
    default_pan_pat = r"(?:PAN|Tax ID)[ \t]*[:\s]?[ \t]*([A-Z]{5}[0-9]{4}[A-Z])"
    pan_pat = fields.get("pan_pattern") or default_pan_pat
    m = re.search(pan_pat, search_text, re.IGNORECASE)
    if not m and pan_pat != default_pan_pat:
        m = re.search(default_pan_pat, search_text, re.IGNORECASE)
    if m:
        pan = m.group(1).strip()

    # Strategy (try template strategy_pattern first, then fallback)
    strategy = None
    strat_pat = fields.get("strategy_pattern")
# strat_pat came from the LLM.

    if strat_pat:
        h_strat_pat = re.sub(r"\\s\+", r"[ \t]+", strat_pat)
# takes the LLM's regex and swaps \s+ with [ \t]+ (only spaces and tabs). It puts a safety bumper on the LLM's pattern so it is locked strictly to a single horizontal line!

        for m in re.finditer(h_strat_pat, search_text, re.IGNORECASE):
            val = m.group(1).strip()
            val = re.split(r"(?:PAN|Tax ID|Currency|Base Currency|Portfolio Currency|Class|Series|Status|Address|Mobile|Custodian|Commitment)", val, flags=re.IGNORECASE)[0].strip()
            if any(val.upper().startswith(k) for k in ["DETAILS", "PROFILE", "SERVICES", "MANAGEMENT", "SUMMARY", "SEBI", "INR", "USD", "&", "METADATA", "VALUATION", "REPORT", "STATEMENT"]) or "METADATA" in val.upper():
                continue
            if len(val) >= 3:
                strategy = val
                break

    if not strategy:
        strat_candidates = re.finditer(
            r"(?:Scheme Name|Scheme|PMS Approach|Approach|Strategy Name|Strategy|Mandate|Plan|(?:(?<!discretionary\s)(?<!discretionary\sportfolio\s)\bPortfolio\b(?!\s*management)(?!\s*details)(?!\s*currency)(?!\s*profile)(?!\s*worth)))[ \t]*[:\s]?[ \t]+([^\r\n]+)",
            search_text,
            re.IGNORECASE
        )
        for m in strat_candidates:
            val = m.group(1).strip()
            val = re.split(r"(?:PAN|Tax ID|Currency|Base Currency|Portfolio Currency|Class|Series|Status|Address|Mobile|Custodian|Commitment)", val, flags=re.IGNORECASE)[0].strip()
            if any(val.upper().startswith(k) for k in ["DETAILS", "PROFILE", "SERVICES", "MANAGEMENT", "SUMMARY", "SEBI", "INR", "USD", "&", "METADATA", "VALUATION", "REPORT", "STATEMENT"]) or "METADATA" in val.upper():
                continue
            if len(val) >= 3:
                strategy = val
                break

    # Currency: extract and normalize explicitly without blind default
    currency = None
    curr_pat = fields.get("currency_pattern")
    if curr_pat:
        cm = re.search(curr_pat, search_text, re.IGNORECASE)
        if cm:
            currency = normalize_currency(cm.group(1) if cm.groups() else cm.group(0))

    if not currency and fields.get("currency"):
        currency = normalize_currency(fields.get("currency"))

    if not currency:
        m = re.search(
            r"(?:Base\s*Currency|Portfolio\s*Currency|Fund\s*Currency|Reporting\s*Currency|Valuation\s*Currency|Currency)[ \t]*[:\-\s][ \t]*([A-Za-z]{3}|Rs\.?|₹|\$|€|£)",
            search_text,
            re.IGNORECASE
        )
        if m:
            currency = normalize_currency(m.group(1))

    if not currency:
        m = re.search(
            r"(?:Base\s*Currency|Portfolio\s*Currency|Fund\s*Currency|Reporting\s*Currency|Valuation\s*Currency|Currency)[\s\r\n]+([A-Za-z]{3}|Rs\.?|₹|\$|€|£)\b",
            search_text,
            re.IGNORECASE
        )
        if m:
            currency = normalize_currency(m.group(1))

    if not currency:
        m = re.search(
            r"(?:figures|amounts?|values?)\s+in\s+([A-Za-z]{3}|Rs\.?|₹|\$|€|£|Rupees|US\s*Dollars?)",
            search_text,
            re.IGNORECASE
        )
        if m:
            currency = normalize_currency(m.group(1))

    if not currency:
        if re.search(r"\(Rs\.?\)|Gross Amount \(Rs\.\)|Cost Basis\s*\(Rs\.\)|\bRs\.\s*\d", search_text, re.IGNORECASE) or "₹" in search_text:
            currency = "INR"
        elif "(USD)" in search_text or "USD " in search_text or "in USD" in search_text or "Series USD" in search_text:
            currency = "USD"


    # Period: try template period.pattern first, then standard fallbacks
    period_start = None
    period_end = ""
    period_cfg = template.get("period", {})
    tmpl_period_pat = period_cfg.get("pattern")

    if tmpl_period_pat:
        pm = re.search(tmpl_period_pat, search_text, re.IGNORECASE)
        if pm:
            groups = pm.groups()
            if len(groups) == 2:
                period_start = parse_date(groups[0].replace(" ", "-"))
                period_end = parse_date(groups[1].replace(" ", "-"))
            elif len(groups) == 4:
                start_m, start_y, end_m, end_y = groups
                period_start = parse_date(f"01-{start_m[:3]}-{start_y}")
                if end_m.lower().startswith("sep"):
                    period_end = f"{end_y}-09-30"
                elif end_m.lower().startswith("dec"):
                    period_end = f"{end_y}-12-31"
                elif end_m.lower().startswith("mar"):
                    period_end = f"{end_y}-03-31"
                elif end_m.lower().startswith("jun"):
                    period_end = f"{end_y}-06-30"
            elif len(groups) == 1:
                dt_str = groups[0].strip().replace(" ", "-")
                period_end = parse_date(dt_str)
                if period_end and period_cfg.get("infer_start") == "first_of_month":
                    dt_obj = datetime.strptime(period_end, "%Y-%m-%d")
                    period_start = f"{dt_obj.year:04d}-{dt_obj.month:02d}-01"

    if not period_end:
        range_match = re.search(r"([0-9]{1,4}[-\/\.][0-9A-Za-z]+[-\/\.][0-9]{2,4})\s*(?:to|through|\-|–|—)\s*([0-9]{1,4}[-\/\.][0-9A-Za-z]+[-\/\.][0-9]{2,4})", search_text, re.IGNORECASE)
        if range_match:
            period_start = parse_date(range_match.group(1))
            period_end = parse_date(range_match.group(2))
        else:
            q_match = re.search(r"Quarter:\s*([A-Za-z]+)\s+([0-9]{4})\s*-\s*([A-Za-z]+)\s+([0-9]{4})", search_text, re.IGNORECASE)
            if q_match:
                start_m, start_y, end_m, end_y = q_match.groups()
                period_start = parse_date(f"01-{start_m[:3]}-{start_y}")
                if end_m.lower().startswith("sep"):
                    period_end = f"{end_y}-09-30"
                elif end_m.lower().startswith("dec"):
                    period_end = f"{end_y}-12-31"
                elif end_m.lower().startswith("mar"):
                    period_end = f"{end_y}-03-31"
                elif end_m.lower().startswith("jun"):
                    period_end = f"{end_y}-06-30"
            else:
                single_m = re.search(r"(?:month ended|Report Date|Valuation Date|As on|As of)\s*[:\s]\s*([0-9]{1,2}[-\/\s][A-Za-z0-9]+[-\/\s][0-9]{4})", search_text, re.IGNORECASE)
                if single_m:
                    dt_str = single_m.group(1).strip().replace(" ", "-")
                    period_end = parse_date(dt_str)
                    if period_end:
                        dt_obj = datetime.strptime(period_end, "%Y-%m-%d")
                        period_start = f"{dt_obj.year:04d}-{dt_obj.month:02d}-01"

    return StatementMeta(
        provider=strip_leading_label(provider_name) or provider_name,
        strategy=strip_leading_label(strategy),
        client_id=strip_leading_label(client_id) or client_id,
        client_name=strip_leading_label(client_name) or client_name,
        pan=pan,
        period_start=period_start,
        period_end=period_end,
        currency=currency
    )


class StatementParser:
    """Deterministic parser executing layout-specific extraction rules from FormatTemplate."""

    def parse(self, file_path: str, template: Dict[str, Any]) -> List[StatementRecord]:
        layout_kind = template.get("layout_kind", "ruled_table")

        if layout_kind == "xlsx":
            return self._parse_xlsx(file_path, template)
        elif layout_kind == "csv_sectioned":
            return self._parse_csv_sectioned(file_path, template)
        elif layout_kind == "monospace_text":
            return self._parse_monospace_text(file_path, template)
        elif layout_kind == "ruled_lines_horizontal_only":
            return self._parse_ruled_lines(file_path, template)
        else:
            return self._parse_ruled_table(file_path, template)

    def _parse_ruled_table(self, file_path: str, template: Dict[str, Any]) -> List[StatementRecord]:
        records = []
        fields_cfg = template.get("fields", {})
        tmpl_cid_pat = fields_cfg.get("client_id_pattern")
        default_cid_pat = r"(?:CLIENT ID|FOLIO\s*/\s*CLIENT CODE|ACCOUNT NO|INVESTOR CODE|UNITHOLDER ID|CLIENT CODE)\s*[:\s]\s*([A-Za-z0-9\/\-]+)"

        with pdfplumber.open(file_path) as pdf:
            pages_by_statement = []
            current_statement_pages = []
            last_client_id = None

            for page in pdf.pages:
                text = page.extract_text() or ""
                cid_match = None
                if tmpl_cid_pat:
                    cid_match = re.search(tmpl_cid_pat, text, re.IGNORECASE)
                if not cid_match:
                    cid_match = re.search(default_cid_pat, text, re.IGNORECASE)

                if cid_match:
                    found_cid = cid_match.group(1).strip()
# 1. Did the Client ID just change? (Boundary between two clients)
                    if last_client_id is not None and found_cid != last_client_id:
                        pages_by_statement.append(current_statement_pages)
                        current_statement_pages = [page]
                        last_client_id = found_cid
                    else:
                        current_statement_pages.append(page)
                        last_client_id = found_cid
                else:
                    current_statement_pages.append(page)

            if current_statement_pages:
                pages_by_statement.append(current_statement_pages)

            for stmt_pages in pages_by_statement:
                first_page = next((p for p in stmt_pages if re.search(r"(?:CLIENT ID|Client ID|FOLIO|PAN|INP\d+)", p.extract_text() or "", re.IGNORECASE)), stmt_pages[0])
                full_text = "\n".join(p.extract_text() or "" for p in stmt_pages)

                w = first_page.width
                left_crop = first_page.crop((20, 70, w / 2, 210)).extract_text() or ""
                right_crop = first_page.crop((w / 2, 70, w - 20, 210)).extract_text() or ""

                meta = extract_metadata_from_text(full_text, template, left_crop, right_crop)
                summary = self._extract_table_summary(stmt_pages, template)
                holdings = self._extract_table_holdings(stmt_pages, template, summary)
                transactions = self._extract_table_transactions(stmt_pages, template, holdings)

                records.append(StatementRecord(
                    statement=meta,
                    summary=summary,
                    holdings=holdings,
                    transactions=transactions
                ))

        return records

    def _extract_table_summary(self, pages: List[Any], template: Dict[str, Any]) -> Summary:
        summary_cfg = template.get("summary", {})
        has_gain = summary_cfg.get("has_gain", True)
        s_map: Dict[str, str] = summary_cfg.get("mapping", {})

        summary_dict: Dict[str, Any] = {
            "opening": 0.0,
            "inflow": 0.0,
            "outflow": 0.0,
            "gain": None if not has_gain else 0.0,
            "fee": 0.0,
            "cash": 0.0,
            "closing": 0.0,
            "missing_columns": []
        }

        mapped_labels = [v.lower() for v in s_map.values() if v]

        for page in pages:
            tables = page.extract_tables()
            for t in tables:
                if not t or len(t) < 2:
                    continue
                header = [re.sub(r"\s+", " ", str(c).strip().lower()) for c in t[0] if c]
                hdr_str = " ".join(header)

                is_summary_table = False
                if mapped_labels and sum(1 for lbl in mapped_labels if any(_col_matches_label(h, lbl) for h in header)) >= 2:
                    is_summary_table = True
                elif any(k in hdr_str for k in ["opening", "closing", "corpus", "nav per unit", "drawdowns", "subscriptions", "worth at start"]):
                    is_summary_table = True

                if is_summary_table and len(t) == 2:
                    cols = [re.sub(r"\s+", " ", str(c).strip().lower()) for c in t[0]]
                    vals = t[1]

                    for c_name, val in zip(cols, vals):
                        if not val:
                            continue
                        num = safe_num(str(val))

                        # 1. Check explicit template mapping first
                        matched_field = None
                        for field_key, label_str in s_map.items():
                            if _col_matches_label(c_name, label_str):
                                matched_field = field_key
                                break

                        if matched_field:
                            if matched_field == "gain":
                                summary_dict["gain"] = clean_number(str(val))
                            elif matched_field == "mgmt_fee":
                                summary_dict["mgmt_fee"] = abs(num)
                                summary_dict["fee"] += abs(num)
                            elif matched_field == "perf_fee":
                                summary_dict["perf_fee"] = abs(num)
                                summary_dict["fee"] += abs(num)
                            elif matched_field == "cash":
                                summary_dict["cash"] = abs(num)
                                summary_dict["cash_from_summary"] = abs(num)
                            elif matched_field in summary_dict or matched_field in ["units", "nav_per_unit"]:
                                summary_dict[matched_field] = abs(num)
                            continue

                        # 2. Fallback keyword matching for unmapped columns (e.g. auxiliary NAV/cash tables)
                        if "nav per unit" in c_name:
                            summary_dict["nav_per_unit"] = abs(num)
                        elif "units held" in c_name:
                            summary_dict["units"] = abs(num)
                        elif any(k in c_name for k in ["cash & money market", "liquid cash", "cash balance"]) and "cash" not in s_map:
                            summary_dict["cash"] = abs(num)
                            summary_dict["cash_from_summary"] = abs(num)
                        elif not s_map:
                            if any(k in c_name for k in ["opening", "beginning", "worth at start"]):
                                summary_dict["opening"] = abs(num)
                            elif any(k in c_name for k in ["inflow", "addition", "contribution", "drawdown", "subscription"]):
                                summary_dict["inflow"] = abs(num)
                            elif any(k in c_name for k in ["outflow", "withdrawal", "redemption", "distribution"]):
                                summary_dict["outflow"] = abs(num)
                            elif any(k in c_name for k in ["gain", "valuation change", "trading p&l", "profit"]):
                                summary_dict["gain"] = clean_number(str(val))
                            elif any(k in c_name for k in ["fee", "charges", "levy"]):
                                if "management" in c_name:
                                    summary_dict["mgmt_fee"] = abs(num)
                                elif "performance" in c_name:
                                    summary_dict["perf_fee"] = abs(num)
                                summary_dict["fee"] += abs(num)
                            elif any(k in c_name for k in ["cash", "money market"]):
                                summary_dict["cash"] = abs(num)
                                summary_dict["cash_from_summary"] = abs(num)
                            elif any(k in c_name for k in ["closing", "ending", "ending worth"]):
                                summary_dict["closing"] = abs(num)

        if summary_dict.get("mgmt_fee") is not None and summary_dict.get("perf_fee") is not None:
            summary_dict["fee"] = summary_dict["mgmt_fee"] + summary_dict["perf_fee"]

        return Summary(**summary_dict)

    def _extract_table_holdings(self, pages: List[Any], template: Dict[str, Any], summary: Optional[Summary] = None) -> List[Holding]:
        holdings = []
        holdings_cfg = template.get("holdings", {})
        supports_short = holdings_cfg.get("supports_short", False)
        is_unlisted = holdings_cfg.get("is_unlisted", False)
        h_map: Dict[str, str] = holdings_cfg.get("mapping", {})
        current_side = "long"

        mapped_labels = [v for v in h_map.values() if v]
        checked_missing_cols = False

        for page in pages:
            tables = page.extract_tables()
            for t in tables:
                if not t or len(t) < 2:
                    continue
                header = [re.sub(r"\s+", " ", str(c).strip().lower()) for c in t[0] if c]
                hdr_str = " ".join(header)

                is_holdings_table = False
                if mapped_labels and sum(1 for lbl in mapped_labels if any(_col_matches_label(h, lbl) for h in header)) >= 2:
                    is_holdings_table = True
                elif re.search(r"\b(security|scrip|company|instrument|portfolio company)\b", hdr_str) or any(k in hdr_str for k in ["% alloc", "% nav"]):
                    if not any(k in hdr_str for k in ["particulars", "execution date", "notice date", "trade date", "txn", "description"]):
                        is_holdings_table = True

                if is_holdings_table:
                    cols = [re.sub(r"\s+", " ", str(c).strip().lower()) for c in t[0] if c is not None]

                    # Verify that all template-mapped holding columns exist in this table
                    if h_map and not checked_missing_cols:
                        checked_missing_cols = True
                        for field_key, expected_hdr in h_map.items():
                            if not any(_col_matches_label(c, expected_hdr) for c in cols):
                                if summary is not None:
                                    missing_list = getattr(summary, "missing_columns", [])
                                    issue_tag = f"expected_column_missing:{expected_hdr}"
                                    if issue_tag not in missing_list:
                                        missing_list.append(issue_tag)
                                    summary.missing_columns = missing_list

                    holdings_cfg = template.get("holdings", {})
                    tmpl_total_labels = [lbl.lower().strip() for lbl in holdings_cfg.get("total_row_labels", [])]
                    tmpl_gl_labels = [lbl.lower().strip() for lbl in holdings_cfg.get("gross_long_labels", [])]
                    tmpl_gs_labels = [lbl.lower().strip() for lbl in holdings_cfg.get("gross_short_labels", [])]
                    tmpl_cash_labels = [lbl.lower().strip() for lbl in holdings_cfg.get("cash_row_labels", [])]
                    tmpl_nav_labels = [lbl.lower().strip() for lbl in holdings_cfg.get("nav_row_labels", [])]

                    fallback_used = False
                    if not tmpl_total_labels:
                        tmpl_total_labels = ["total equity", "total securities", "subtotal listed equities"]
                        fallback_used = True
                    if not tmpl_gl_labels:
                        tmpl_gl_labels = ["gross long exposure", "gross long"]
                    if not tmpl_gs_labels:
                        tmpl_gs_labels = ["gross short exposure", "gross short"]
                    if not tmpl_cash_labels:
                        tmpl_cash_labels = ["cash & margin", "cash and margin", "liquid cash reserve", "liquid cash", "liquid balance", "cash & bank", "cash balance", "cash & liquid", "cash equivalents", "cash"]
                        fallback_used = True
                    if not tmpl_nav_labels:
                        tmpl_nav_labels = ["net asset value"]

                    if fallback_used and summary is not None:
                        soft_list = getattr(summary, "soft_issues", [])
                        if "template_missing_row_labels" not in soft_list:
                            soft_list.append("template_missing_row_labels")
                        summary.soft_issues = soft_list

                    def _matches_any_lbl(cell_val: str, lbl_list: List[str]) -> bool:
                        cv = cell_val.strip().lower()
                        return any(cv == l or cv.startswith(l) or (len(l) > 4 and l in cv) for l in lbl_list)

                    for row in t[1:]:
                        if not row or not any(row):
                            continue
                        row_str = " ".join(str(c) for c in row if c).strip()

                        # Detect section headers inside tables (e.g. Cat III Long vs Short positions)
                        if "long positions" in row_str.lower():
                            current_side = "long"
                            continue
                        elif "short positions" in row_str.lower():
                            current_side = "short"
                            continue

                        first_cell = str(row[0]).strip().lower()
                        mv_label = h_map.get("market_value", "market value")

                        # 1. Capture stated total equity row for validator cross-check
                        if _matches_any_lbl(first_cell, tmpl_total_labels):
                            for c_name, val in zip(cols, row):
                                if _col_matches_label(c_name, mv_label) or any(k in c_name for k in ["market value", "valuation", "current value", "worth"]):
                                    num = clean_number(val)
                                    if num is not None and summary is not None:
                                        summary.stated_equity = abs(num)
                            continue

                        # 2. Capture gross long exposure row (AIF Cat III long positions)
                        if _matches_any_lbl(first_cell, tmpl_gl_labels):
                            for c_name, val in zip(cols, row):
                                if _col_matches_label(c_name, mv_label) or any(k in c_name for k in ["market value", "valuation", "amount"]):
                                    num = clean_number(val)
                                    if num is not None and summary is not None:
                                        summary.stated_gross_long = abs(num)
                            continue

                        # 3. Capture gross short exposure row (AIF Cat III short/derivative obligations)
                        if _matches_any_lbl(first_cell, tmpl_gs_labels):
                            for c_name, val in zip(cols, row):
                                if _col_matches_label(c_name, mv_label) or any(k in c_name for k in ["market value", "valuation", "amount"]):
                                    num = clean_number(val)
                                    if num is not None and summary is not None:
                                        summary.stated_gross_short = -abs(num)
                            continue

                        # 4. Capture cash / bank balance row if printed inside holdings table
                        if _matches_any_lbl(first_cell, tmpl_cash_labels):
                            for c_name, val in zip(cols, row):
                                if _col_matches_label(c_name, mv_label) or any(k in c_name for k in ["market value", "valuation", "total value", "current value", "amount", "worth"]):
                                    num = clean_number(val)
                                    if num is not None and summary is not None:
                                        summary.cash_from_row = abs(num)
                                        if summary.cash == 0.0:
                                            summary.cash = abs(num)
                            continue

                        # 5. Capture Net Asset Value (NAV) row if printed inside holdings table
                        if _matches_any_lbl(first_cell, tmpl_nav_labels):
                            for c_name, val in zip(cols, row):
                                if _col_matches_label(c_name, mv_label) or any(k in c_name for k in ["market value", "valuation", "amount"]):
                                    num = clean_number(val)
                                    if num is not None and summary is not None:
                                        summary.stated_nav = abs(num)
                            continue

                        # Skip any subtotal, total, or cash rows so they are not treated as stock holdings
                        if _matches_any_lbl(first_cell, tmpl_total_labels + tmpl_cash_labels + tmpl_nav_labels):
                            continue

                        if any(first_cell.startswith(k) for k in ["total", "subtotal", "sub-total", "grand total", "net portfolio", "...continued", "gross", "treasury"]):
                            continue

                        name = ""
                        isin = None
                        qty = 0.0
                        avg_cost = None
                        price = None
                        market_value = 0.0
                        weight_pct = None
                        total_cost = None
                        instrument = None

                        for c_name, val in zip(cols, row):
                            if val is None or str(val).strip() in ["", "-"]:
                                continue
                            val_str = str(val).replace("\n", " ").strip()
                            num = clean_number(val_str)

                            # 1. Explicit template column mapping
                            matched_field = None
                            for field_key, label_str in h_map.items():
                                if _col_matches_label(c_name, label_str):
                                    matched_field = field_key
                                    break

                            if matched_field:
                                if matched_field == "name":
                                    m_isin = re.search(r"\(([A-Z0-9]{12})\)", val_str)
                                    if m_isin:
                                        isin = m_isin.group(1)
                                        name = val_str[:m_isin.start()].strip()
                                    else:
                                        name = val_str
                                elif matched_field == "isin":
                                    if isin is None and re.match(r"^[A-Z0-9]{12}$", val_str):
                                        isin = val_str
                                elif matched_field == "instrument":
                                    instrument = val_str
                                elif matched_field == "quantity" and num is not None:
                                    qty = num
                                elif matched_field == "avg_cost" and num is not None:
                                    avg_cost = abs(num)
                                elif matched_field == "total_cost" and num is not None:
                                    total_cost = num
                                elif matched_field == "price" and num is not None:
                                    price = abs(num)
                                elif matched_field == "market_value" and num is not None:
                                    market_value = num
                                elif matched_field == "weight_pct" and num is not None:
                                    weight_pct = num
                                continue

                            # 2. Fallback synonyms only when template has no holdings mapping
                            if not h_map:
                                if any(k in c_name for k in ["security", "scrip", "company", "portfolio company"]):
                                    m_isin = re.search(r"\(([A-Z0-9]{12})\)", val_str)
                                    if m_isin:
                                        isin = m_isin.group(1)
                                        name = val_str[:m_isin.start()].strip()
                                    else:
                                        name = val_str
                                elif any(k in c_name for k in ["isin", "symbol"]):
                                    if isin is None and re.match(r"^[A-Z0-9]{12}$", val_str):
                                        isin = val_str
                                elif any(k in c_name for k in ["instrument type", "instrument"]):
                                    instrument = val_str
                                elif any(k in c_name for k in ["qty", "units", "holding"]):
                                    qty = num
                                elif any(k in c_name for k in ["avg cost", "acquisition cost", "cost per unit"]):
                                    avg_cost = abs(num)
                                elif any(k in c_name for k in ["total cost", "cost basis"]):
                                    total_cost = num
                                elif any(k in c_name for k in ["market price", "price", "ltp", "rate"]):
                                    price = abs(num)
                                elif any(k in c_name for k in ["market value", "current value", "fair valuation", "valuation", "worth"]):
                                    market_value = num
                                elif any(k in c_name for k in ["weight", "% alloc", "% nav", "allocation", "share %"]):
                                    weight_pct = num

                        if not name or name.startswith(("Rs.", "INR", "USD")) or name.lower().startswith("cash"):
                            continue
                        if supports_short and qty == 0.0:
                            continue

                        if supports_short and current_side == "short":
                            qty = -abs(qty)
                            market_value = -abs(market_value)
                            if weight_pct is not None:
                                weight_pct = -abs(weight_pct)

                        # Derive market price if missing: Price = Market Value / Quantity
                        if price is None and qty != 0 and market_value != 0:
                            price = round(abs(market_value) / abs(qty), 4)

                        # Derive total cost or average cost if only one is printed in the statement
                        if total_cost is None and avg_cost is not None and qty != 0:
                            total_cost = round(avg_cost * abs(qty), 2)
                        elif avg_cost is None and total_cost is not None and qty != 0:
                            avg_cost = round(total_cost / abs(qty), 4)

                        holding_dict = {
                            "name": name,
                            "isin": isin,
                            "quantity": qty,
                            "market_value": market_value,
                            "weight_pct": weight_pct,
                            "price": price,
                            "avg_cost": avg_cost,
                            "total_cost": total_cost,
                        }
                        if supports_short:
                            holding_dict["side"] = current_side
                        if is_unlisted:
                            holding_dict["instrument"] = instrument

                        holdings.append(Holding(**holding_dict))

        return holdings

    def _extract_table_transactions(self, pages: List[Any], template: Dict[str, Any], holdings: List[Holding]) -> List[Transaction]:
        transactions = []
        tx_cfg = template.get("transactions", {})
        t_map: Dict[str, str] = tx_cfg.get("mapping", {})
        raw_type_map: Dict[str, str] = tx_cfg.get("type_map", {
            "INFLOW": "inflow", "OUTFLOW": "outflow", "BUY": "buy", "SELL": "sell",
            "PURCHASE": "buy", "SALE": "sell", "DIVIDEND": "dividend", "FEE": "fee",
            "CHARGES": "fee", "DRAWDOWN": "inflow", "DISTRIBUTION": "outflow",
            "SUBSCRIPTION": "inflow", "REDEMPTION": "outflow", "MANAGEMENT FEE": "fee",
            "PERFORMANCE FEE": "fee", "MGMT FEE": "fee", "PERF FEE": "fee"
        })
        type_map_upper = {re.sub(r"\s+", " ", k.upper().strip()): v for k, v in raw_type_map.items()}
        mapped_labels = [v for v in t_map.values() if v]

        for page in pages:
            tables = page.extract_tables()
            for t in tables:
                if not t or len(t) < 2:
                    continue
                header = [re.sub(r"\s+", " ", str(c).strip().lower()) for c in t[0] if c]
                hdr_str = " ".join(header)

                is_tx_table = False
                if mapped_labels and sum(1 for lbl in mapped_labels if any(_col_matches_label(h, lbl) for h in header)) >= 2:
                    is_tx_table = True
                elif any(k in hdr_str for k in ["particulars", "action", "narration", "transaction type", "txn", "ref no.", "description"]):
                    is_tx_table = True

                if is_tx_table:
                    cols = [re.sub(r"\s+", " ", str(c).strip().lower()) for c in t[0] if c is not None]

                    for row in t[1:]:
                        if not row or not any(row):
                            continue
                        tx_date = ""
                        raw_type = ""
                        sec_name = None
                        isin = None
                        qty = None
                        price = None
                        amount = 0.0

                        for c_name, val in zip(cols, row):
                            if val is None or str(val).strip() in ["", "-"]:
                                continue
                            val_str = str(val).replace("\n", " ").strip()
                            num = clean_number(val_str)

                            # 1. Explicit template mapping
                            matched_field = None
                            for field_key, label_str in t_map.items():
                                if _col_matches_label(c_name, label_str):
                                    matched_field = field_key
                                    break

                            if matched_field:
                                if matched_field == "date":
                                    tx_date = parse_date(val_str)
                                elif matched_field == "raw_type":
                                    raw_type = val_str
                                elif matched_field == "security":
                                    m_isin = re.search(r"\(([A-Z0-9]{12})\)", val_str)
                                    if m_isin:
                                        isin = m_isin.group(1)
                                        sec_name = val_str[:m_isin.start()].strip()
                                    else:
                                        sec_name = val_str
                                elif matched_field == "isin":
                                    if re.match(r"^[A-Z0-9]{12}$", val_str):
                                        isin = val_str
                                elif matched_field == "quantity" and num is not None:
                                    qty = abs(num)
                                elif matched_field == "price" and num is not None:
                                    price = abs(num)
                                elif matched_field == "amount" and num is not None:
                                    amount = abs(num)
                                continue

                            # 2. Fallback synonyms
                            if any(k in c_name for k in ["date", "notice date", "trade date"]):
                                tx_date = parse_date(val_str)
                            elif any(k in c_name for k in ["type", "action", "txn"]):
                                raw_type = val_str
                            elif any(k in c_name for k in ["particulars", "instrument", "narration", "description"]):
                                m_isin = re.search(r"\(([A-Z0-9]{12})\)", val_str)
                                if m_isin:
                                    isin = m_isin.group(1)
                                    sec_name = val_str[:m_isin.start()].strip()
                                else:
                                    sec_name = val_str
                            elif any(k in c_name for k in ["qty", "executed qty", "quantity", "units"]) or re.search(r"quantit\s*y|executed\s*qty", c_name):
                                qty = abs(num) if num is not None else None
                            elif any(k in c_name for k in ["price", "rate", "cmp", "cost price"]):
                                price = abs(num) if num is not None else None
                            elif any(k in c_name for k in ["amount", "net total", "gross amount", "value", "net amount"]):
                                amount = abs(num) if num is not None else 0.0

                        if not tx_date or not raw_type:
                            continue

                        norm_raw = re.sub(r"\s+", " ", raw_type.upper().strip())
                        norm_raw_underscore = norm_raw.replace(" ", "_")
                        std_type = type_map_upper.get(norm_raw) or type_map_upper.get(norm_raw_underscore)

                        if not std_type:
                            STANDARD_TYPE_MAP = {
                                "INFLOW": "inflow", "ADDITION": "inflow", "ADDITIONS": "inflow",
                                "FUNDS IN": "inflow", "FUNDS INTRODUCED": "inflow", "DRAWDOWN": "inflow",
                                "SUBSCRIPTION": "inflow", "CAPITAL IN": "inflow", "DEPOSIT": "inflow",
                                "OUTFLOW": "outflow", "WITHDRAWAL": "outflow", "WITHDRAWALS": "outflow",
                                "FUNDS OUT": "outflow", "FUNDS WITHDRAWN": "outflow", "DISTRIBUTION": "outflow",
                                "REDEMPTION": "outflow", "CAPITAL OUT": "outflow",
                                "BUY": "buy", "PURCHASE": "buy", "B": "buy",
                                "SELL": "sell", "SALE": "sell", "S": "sell",
                                "DIVIDEND": "dividend", "DIV": "dividend", "INTEREST": "dividend",
                                "FEE": "fee", "FEES": "fee", "CHARGES": "fee", "ADVISORY FEE": "fee",
                                "MANAGEMENT FEE": "fee", "PERFORMANCE FEE": "fee", "MGMT FEE": "fee",
                                "PERF FEE": "fee", "CUSTODY FEE": "fee",
                                "AUDIT FEE": "fee", "BROKERAGE": "fee"
                            }
                            std_type = STANDARD_TYPE_MAP.get(norm_raw, "other")

                        if std_type in ["fee", "inflow", "outflow"]:
                            sec_name = None
                            isin = None
                            qty = None
                            price = None
                        elif std_type == "dividend" and sec_name:
                            sec_name = re.sub(r"\s*(?:dividend\s+credit|dividend\s+payout|div\s+credit|dividend)\s*$", "", sec_name, flags=re.IGNORECASE).strip()

                        if sec_name and not isin:
                            isin = resolve_isin_from_holdings(sec_name, holdings)

                        transactions.append(Transaction(
                            date=tx_date,
                            type=std_type,
                            raw_type=raw_type,
                            security=sec_name,
                            isin=isin,
                            quantity=qty,
                            price=price,
                            amount=amount
                        ))

        return transactions

    def _parse_monospace_text(self, file_path: str, template: Dict[str, Any]) -> List[StatementRecord]:
        """Parses monospace textflow PDF using declarative template rules."""
        with pdfplumber.open(file_path) as pdf:
            full_text = "\n".join(p.extract_text() or "" for p in pdf.pages)

        meta = extract_metadata_from_text(full_text, template)

        # 1. Summary extraction from template.summary.mapping
        summary_cfg = template.get("summary", {})
        has_gain = summary_cfg.get("has_gain", True)
        default_s_map = {
            "opening": "Opening Value",
            "inflow": "Funds Introduced",
            "outflow": "Funds Withdrawn",
            "gain": "Net Gain/(Loss)",
            "fee": "Fees & Charges",
            "cash": "Cash & Equivalents",
            "closing": "Closing Value"
        }
        s_map = summary_cfg.get("mapping") or default_s_map

        summary_dict: Dict[str, Any] = {
            "opening": 0.0, "inflow": 0.0, "outflow": 0.0,
            "gain": None if not has_gain else 0.0,
            "fee": 0.0, "cash": 0.0, "closing": 0.0
        }

        for line in full_text.split("\n"):
            line_str = line.strip()
            for field_key, label_str in s_map.items():
                if label_str and label_str.lower() in line_str.lower():
                    val_part = line_str.split(":")[-1] if ":" in line_str else line_str[line_str.lower().find(label_str.lower()) + len(label_str):]
                    num = clean_number(val_part)
                    if num is not None:
                        if field_key == "gain":
                            summary_dict["gain"] = num
                        else:
                            summary_dict[field_key] = abs(num)

        # 2. Holdings extraction from template.holdings
        holdings_cfg = template.get("holdings", {})
        h_section_hdr = holdings_cfg.get("section_header") or "HOLDINGS AS ON"
        h_end_markers = holdings_cfg.get("end_markers") or ["TRANSACTIONS", "Total"]
        h_total_labels = [lbl.lower().strip() for lbl in holdings_cfg.get("total_row_labels", [])]
        h_cash_labels = [lbl.lower().strip() for lbl in holdings_cfg.get("cash_row_labels", [])]
        fallback_used = False
        if not h_total_labels:
            h_total_labels = ["total"]
            fallback_used = True
        if not h_cash_labels:
            h_cash_labels = ["cash & equivalents"]
            fallback_used = True
        if fallback_used:
            soft_list = summary_dict.setdefault("soft_issues", [])
            if "template_missing_row_labels" not in soft_list:
                soft_list.append("template_missing_row_labels")
        stop_markers = h_end_markers + h_total_labels

        default_h_regex = r"^\s*(\d+)\s+(.+?)\s+([A-Z0-9]{12})\s+([\-0-9,.()]+)\s+([\-0-9,.()]+)\s+([\-0-9,.()]+)\s+([\-0-9,.()]+)\s+([\-0-9,.()]+)"
        h_regex = holdings_cfg.get("regex") or default_h_regex

        holdings = []
        in_holdings = False
        for line in full_text.split("\n"):
            line_str = line.strip()
            if h_section_hdr.lower() in line_str.lower():
                in_holdings = True
                continue
            if in_holdings and any(line_str.lower().startswith(em.lower()) or em.lower() in line_str.lower() for em in stop_markers):
                in_holdings = False
                continue
            if in_holdings:
                m = re.match(h_regex, line_str)
                if m:
                    groups = m.groups()
                    if len(groups) == 8:
                        _, name, isin, qty_str, avg_str, price_str, val_str, wt_str = groups
                    elif len(groups) == 7:
                        name, isin, qty_str, avg_str, price_str, val_str, wt_str = groups
                    else:
                        continue
                    qty = clean_number(qty_str)
                    avg_cost = abs(clean_number(avg_str))
                    price = abs(clean_number(price_str))
                    mval = clean_number(val_str)
                    wt = clean_number(wt_str)
                    total_cost = round(avg_cost * qty, 2)
                    holdings.append(Holding(
                        name=name.strip(),
                        isin=isin.strip(),
                        quantity=qty,
                        market_value=mval,
                        weight_pct=wt,
                        price=price,
                        avg_cost=avg_cost,
                        total_cost=total_cost
                    ))

        # 3. Transactions extraction from template.transactions
        tx_cfg = template.get("transactions", {})
        t_section_hdr = tx_cfg.get("section_header", "TRANSACTIONS")
        t_end_markers = tx_cfg.get("end_markers", ["End of report", "Page "])
        default_type_map = {
            "PURCHASE": "buy", "SALE": "sell", "DIVIDEND": "dividend", "CHARGES": "fee",
            "FUNDS IN": "inflow", "FUNDS OUT": "outflow", "INFLOW": "inflow", "OUTFLOW": "outflow"
        }
        raw_type_map = tx_cfg.get("type_map") or default_type_map
        type_map = {re.sub(r"\s+", " ", k.upper().strip()): v for k, v in raw_type_map.items()}

        # Build longest-match alternation from template type_map keys
        sorted_keys = sorted(type_map.keys(), key=len, reverse=True)
        escaped_keys = [re.sub(r"\\\s+", r"\\s+", re.escape(k)) for k in sorted_keys]
        type_tokens_pat = r"(?:" + "|".join(escaped_keys) + r")" if escaped_keys else r"(?:BUY|SELL|PURCHASE|SALE|DIVIDEND|FEE|INFLOW|OUTFLOW)"

        tmpl_tx_regex = tx_cfg.get("regex", "")
        if tmpl_tx_regex and "{TYPE_TOKENS}" in tmpl_tx_regex:
            primary_tx_regex = tmpl_tx_regex.replace("{TYPE_TOKENS}", type_tokens_pat)
        else:
            primary_tx_regex = r"^([0-9\/\-]{10})\s+(" + type_tokens_pat + r")\s+(.+?)\s+([0-9,.\-]+)\s+([0-9,.\-]+)\s+([\-0-9,.]+)\s*$"

        fallback_tx_regex = r"^([0-9\/\-]{10})\s+([A-Za-z0-9_]+(?:\s+[A-Za-z0-9_]+)?)\s+(.+?)\s+([0-9,.\-]+)\s+([0-9,.\-]+)\s+([\-0-9,.]+)\s*$"

        transactions = []
        in_tx = False
        for line in full_text.split("\n"):
            line_str = line.strip()
            if t_section_hdr.lower() in line_str.lower():
                in_tx = True
                continue
            if in_tx and (any(em.lower() in line_str.lower() for em in t_end_markers) or line_str.lower().startswith(("end of report", "page ", "disclaimer"))):
                in_tx = False
                continue
            if in_tx:
                m = re.match(primary_tx_regex, line_str, re.IGNORECASE)
                if not m:
                    m = re.match(fallback_tx_regex, line_str)
                if m:
                    dt_str, raw_t, part, q_str, pr_str, amt_str = m.groups()
                    raw_norm = re.sub(r"\s+", " ", raw_t.strip().upper())
                    std_type = type_map.get(raw_norm, "other")
                    sec_name = part.strip()
                    isin = None
                    qty = None
                    price = None
                    amt = abs(clean_number(amt_str))

                    if std_type in ["fee", "inflow", "outflow"]:
                        sec_name = None
                    elif std_type == "dividend":
                        sec_name = re.sub(r"dividend\s+credit.*", "", sec_name, flags=re.IGNORECASE).strip()
                        isin = resolve_isin_from_holdings(sec_name, holdings)
                    else:
                        qty = abs(clean_number(q_str)) if q_str != "-" else None
                        price = abs(clean_number(pr_str)) if pr_str != "-" else None
                        isin = resolve_isin_from_holdings(sec_name, holdings)

                    transactions.append(Transaction(
                        date=parse_date(dt_str),
                        type=std_type,
                        raw_type=raw_t,
                        security=sec_name,
                        isin=isin,
                        quantity=qty,
                        price=price,
                        amount=amt
                    ))

        return [StatementRecord(
            statement=meta,
            summary=Summary(**summary_dict),
            holdings=holdings,
            transactions=transactions
        )]

    def _parse_ruled_lines(self, file_path: str, template: Dict[str, Any]) -> List[StatementRecord]:
        """Parses horizontal-rule ledger statements using declarative template rules."""
        with pdfplumber.open(file_path) as pdf:
            first_page = pdf.pages[0]
            w = first_page.width
            full_text = "\n".join(p.extract_text() or "" for p in pdf.pages)

            left_crop = first_page.crop((20, 60, w / 2, 210)).extract_text() or ""
            right_crop = first_page.crop((w / 2, 60, w - 20, 210)).extract_text() or ""

        meta = extract_metadata_from_text(full_text, template, left_crop, right_crop)

        # 1. Summary extraction from template.summary
        summary_cfg = template.get("summary", {})
        has_gain = summary_cfg.get("has_gain", True)
        s_section_hdr = summary_cfg.get("section_header", "LEDGER SUMMARY")
        cols_order = summary_cfg.get("columns_order") or list(summary_cfg.get("mapping", {}).keys()) or [
            "opening", "inflow", "outflow", "gain", "fee", "cash", "closing"
        ]

        summary_dict: Dict[str, Any] = {
            "opening": 0.0, "inflow": 0.0, "outflow": 0.0,
            "gain": None if not has_gain else 0.0,
            "fee": 0.0, "cash": 0.0, "closing": 0.0
        }
        lines = full_text.split("\n")
        for i, line in enumerate(lines):
            if s_section_hdr.lower() in line.lower() and i + 2 < len(lines):
                for data_line in lines[i + 1:i + 6]:
                    nums = [clean_number(tok) for tok in data_line.split() if re.search(r"[0-9]", tok)]
                    if len(nums) >= len(cols_order):
                        for idx, field_key in enumerate(cols_order):
                            val = nums[idx]
                            if val is None:
                                continue
                            if field_key == "gain":
                                summary_dict["gain"] = val
                            else:
                                summary_dict[field_key] = abs(val)
                        break

        # 2. Holdings extraction from template.holdings
        holdings_cfg = template.get("holdings", {})
        h_section_hdr = holdings_cfg.get("section_header") or "ANNEXURE A - HOLDINGS"
        h_end_markers = holdings_cfg.get("end_markers") or ["Total Securities", "LEDGER ENTRIES"]
        h_total_labels = [lbl.lower().strip() for lbl in holdings_cfg.get("total_row_labels", [])]
        h_cash_labels = [lbl.lower().strip() for lbl in holdings_cfg.get("cash_row_labels", [])]
        fallback_used = False
        if not h_total_labels:
            h_total_labels = ["total securities"]
            fallback_used = True
        if not h_cash_labels:
            h_cash_labels = ["cash balance"]
            fallback_used = True
        if fallback_used:
            soft_list = summary_dict.setdefault("soft_issues", [])
            if "template_missing_row_labels" not in soft_list:
                soft_list.append("template_missing_row_labels")
        default_h_regex = r"^(.+?)\s+([A-Z0-9]{12})\s+([\-0-9,.()]+)\s+([\-0-9,.()]+)\s+([\-0-9,.()]+)\s+([\-0-9,.()]+)%?"
        h_regex = holdings_cfg.get("regex") or default_h_regex

        holdings = []
        in_holdings = False
        for line in lines:
            line_str = line.strip()
            if h_section_hdr.lower() in line_str.lower():
                in_holdings = True
                continue
            if in_holdings and any(em.lower() in line_str.lower() for em in h_end_markers):
                if any(lbl in line_str.lower() for lbl in h_total_labels):
                    nums = [clean_number(tok) for tok in line_str.split() if re.search(r"[0-9]", tok)]
                    if len(nums) >= 3:
                        summary_dict["stated_equity"] = abs(nums[-2])
                in_holdings = False
                continue
            if in_holdings:
                m = re.match(h_regex, line_str)
                if m:
                    name, isin, qty_str, cost_str, val_str, wt_str = m.groups()
                    qty = clean_number(qty_str)
                    tot_cost = abs(clean_number(cost_str))
                    mval = clean_number(val_str)
                    wt = clean_number(wt_str)
                    avg_cost = round(tot_cost / abs(qty), 4) if qty != 0 else 0.0
                    price = round(abs(mval) / abs(qty), 4) if qty != 0 else 0.0
                    holdings.append(Holding(
                        name=name.strip(),
                        isin=isin.strip(),
                        quantity=qty,
                        market_value=mval,
                        weight_pct=wt,
                        price=price,
                        avg_cost=avg_cost,
                        total_cost=tot_cost
                    ))

        # 3. Transactions extraction from template.transactions
        tx_cfg = template.get("transactions", {})
        t_section_hdr = tx_cfg.get("section_header", "LEDGER ENTRIES")
        t_end_markers = tx_cfg.get("end_markers", ["Total", "Page ", "Disclaimer"])
        default_t_regex = r"^([0-9]{4}-[0-9]{2}-[0-9]{2})\s+(.+?)\s+([A-Z0-9]+\d+)\s+([0-9,.\-]+)\s+([0-9,.\-]+)"
        t_regex = tx_cfg.get("regex") or default_t_regex
        default_type_map = {
            "Purchase": "buy",
            "Sale": "sell",
            "Dividend received": "dividend",
            "Charges debited": "fee",
            "Funds withdrawn": "outflow",
            "Funds received": "inflow"
        }
        raw_type_map = tx_cfg.get("type_map") or default_type_map
        # Sort keys by length descending so longest prefix matches first
        sorted_type_items = sorted(raw_type_map.items(), key=lambda kv: len(kv[0]), reverse=True)

        transactions = []
        in_tx = False
        for line in lines:
            line_str = line.strip()
            if t_section_hdr.lower() in line_str.lower():
                in_tx = True
                continue
            if in_tx and (
                line_str.lower().startswith(("total", "page"))
                or any(em.lower() in line_str.lower() for em in t_end_markers)
            ):
                in_tx = False
                continue
            if in_tx:
                m = re.match(t_regex, line_str)
                if m:
                    dt_str, narr, ref, debit_str, credit_str = m.groups()
                    debit = abs(clean_number(debit_str)) if debit_str != "-" else 0.0
                    credit = abs(clean_number(credit_str)) if credit_str != "-" else 0.0

                    std_type = "other"
                    raw_type = narr.split("-")[0].strip() if "-" in narr else narr.strip()
                    sec_name = None
                    isin = None
                    qty = None
                    price = None
                    amt = debit if debit > 0 else credit

                    for label_key, mapped_enum in sorted_type_items:
                        if narr.lower().startswith(label_key.lower()) or label_key.lower() in narr.lower():
                            std_type = mapped_enum
                            raw_type = label_key if label_key in narr else raw_type
                            break

                    if std_type == "dividend":
                        m_sec = re.search(r"-\s*(.+)$", narr)
                        if m_sec:
                            sec_name = m_sec.group(1).strip()
                            isin = resolve_isin_from_holdings(sec_name, holdings)
                    elif std_type in ("buy", "sell"):
                        m_trade = re.match(r"[^\-]+\-\s*(.+?)\s*-\s*([0-9,.]+)\s*units\s*@\s*([0-9,.]+)", narr, re.IGNORECASE)
                        if m_trade:
                            sec_name, q_str, pr_str = m_trade.groups()
                            sec_name = sec_name.strip()
                            qty = abs(clean_number(q_str))
                            price = abs(clean_number(pr_str))
                            isin = resolve_isin_from_holdings(sec_name, holdings)

                    transactions.append(Transaction(
                        date=parse_date(dt_str),
                        type=std_type,
                        raw_type=raw_type,
                        security=sec_name,
                        isin=isin,
                        quantity=qty,
                        price=price,
                        amount=amt
                    ))

        return [StatementRecord(
            statement=meta,
            summary=Summary(**summary_dict),
            holdings=holdings,
            transactions=transactions
        )]

    def _parse_xlsx(self, file_path: str, template: Dict[str, Any]) -> List[StatementRecord]:
        """Parses multi-sheet Excel statements using template sheet and column mappings."""
        wb = openpyxl.load_workbook(file_path, data_only=True)

        summary_cfg = template.get("summary", {})
        holdings_cfg = template.get("holdings", {})
        tx_cfg = template.get("transactions", {})

        sum_sheet_name = summary_cfg.get("sheet", "Summary")
        ws_sum = wb[sum_sheet_name] if sum_sheet_name in wb.sheetnames else wb.active
        first_val = ws_sum.cell(1, 1).value
        provider = str(first_val).strip() if first_val else template.get("fields", {}).get("provider", "Unknown Provider")
        meta_dict: Dict[str, Any] = {"provider": provider, "currency": None}
        tmpl_fields = template.get("fields", {})
        if tmpl_fields.get("currency"):
            meta_dict["currency"] = normalize_currency(tmpl_fields.get("currency"))

        has_gain = summary_cfg.get("has_gain", True)
        s_map = summary_cfg.get("mapping", {})
        summary_dict: Dict[str, Any] = {
            "opening": 0.0, "inflow": 0.0, "outflow": 0.0,
            "gain": None if not has_gain else 0.0,
            "fee": 0.0, "cash": 0.0, "closing": 0.0
        }

        for row in ws_sum.iter_rows(values_only=True):
            if not row or not row[0]:
                continue
            k = str(row[0]).strip().lower()
            v = row[1] if len(row) > 1 else None

            if "client id" in k:
                meta_dict["client_id"] = str(v).strip()
            elif "client name" in k:
                meta_dict["client_name"] = str(v).strip()
            elif "pan" in k:
                meta_dict["pan"] = str(v).strip()
            elif "strategy" in k:
                meta_dict["strategy"] = str(v).strip()
            elif "currency" in k:
                meta_dict["currency"] = normalize_currency(str(v).strip() if v is not None else "")
            elif "period start" in k:
                meta_dict["period_start"] = parse_date(str(v))
            elif "period end" in k:
                meta_dict["period_end"] = parse_date(str(v))
            else:
                matched_s = None
                for field_key, label_str in s_map.items():
                    if _col_matches_label(k, label_str):
                        matched_s = field_key
                        break
                if matched_s:
                    if matched_s == "gain":
                        summary_dict["gain"] = float(v or 0)
                    else:
                        summary_dict[matched_s] = abs(float(v or 0))
                elif "opening" in k:
                    summary_dict["opening"] = abs(float(v or 0))
                elif "contribution" in k or "inflow" in k:
                    summary_dict["inflow"] = abs(float(v or 0))
                elif "withdrawal" in k or "outflow" in k:
                    summary_dict["outflow"] = abs(float(v or 0))
                elif "gain" in k:
                    summary_dict["gain"] = float(v or 0)
                elif "fee" in k:
                    summary_dict["fee"] = abs(float(v or 0))
                elif "cash" in k:
                    summary_dict["cash"] = abs(float(v or 0))
                elif "closing" in k:
                    summary_dict["closing"] = abs(float(v or 0))

        if not meta_dict.get("currency"):
            curr_pat = tmpl_fields.get("currency_pattern")
            for row in ws_sum.iter_rows(values_only=True):
                for cell_val in (row or []):
                    if cell_val is not None:
                        c_str = str(cell_val).strip()
                        if curr_pat:
                            cm = re.search(curr_pat, c_str, re.IGNORECASE)
                            if cm:
                                meta_dict["currency"] = normalize_currency(cm.group(1) if cm.groups() else cm.group(0))
                                break
                        m = re.search(r"(?:Base\s*Currency|Portfolio\s*Currency|Currency)[ \t]*[:\-\s][ \t]*([A-Za-z]{3}|Rs\.?|₹|\$)", c_str, re.IGNORECASE)
                        if m:
                            meta_dict["currency"] = normalize_currency(m.group(1))
                            break
                        if re.search(r"\(INR\)|\(Rs\.?\)", c_str, re.IGNORECASE):
                            meta_dict["currency"] = "INR"
                            break
                        elif re.search(r"\(USD\)|\(\$\)", c_str, re.IGNORECASE):
                            meta_dict["currency"] = "USD"
                            break
                if meta_dict.get("currency"):
                    break

        meta = StatementMeta(**meta_dict)
        summary = Summary(**summary_dict)

        # 2. Holdings Sheet
        holdings = []
        h_sheet_name = holdings_cfg.get("sheet", "Holdings")
        ws_h = wb[h_sheet_name]
        rows_h = list(ws_h.iter_rows(values_only=True))
        h_map = holdings_cfg.get("mapping", {})
        hdr_idx = None
        for i, row in enumerate(rows_h):
            if row and any(any(k in str(c).lower() for k in ["security", "scrip", "isin"]) for c in row if c):
                hdr_idx = i
                break

        if hdr_idx is not None:
            cols = [str(c).lower().strip() if c is not None else "" for c in rows_h[hdr_idx]]
            for row in rows_h[hdr_idx + 1:]:
                if not row or not row[0]:
                    continue
                row_label = str(row[0]).strip().lower()
                h_total_labels = [lbl.lower().strip() for lbl in holdings_cfg.get("total_row_labels", [])]
                h_cash_labels = [lbl.lower().strip() for lbl in holdings_cfg.get("cash_row_labels", [])]
                fallback_used = False
                if not h_total_labels:
                    h_total_labels = ["total", "grand total", "total equity"]
                    fallback_used = True
                if not h_cash_labels:
                    h_cash_labels = ["cash", "cash balance", "cash & margin", "liquid cash reserve"]
                    fallback_used = True
                if fallback_used:
                    soft_list = getattr(summary, "soft_issues", [])
                    if "template_missing_row_labels" not in soft_list:
                        soft_list.append("template_missing_row_labels")
                    summary.soft_issues = soft_list
                skip_labels = set(h_total_labels + h_cash_labels + ["total", "grand total", "total equity", "cash"])
                if any(lbl in row_label for lbl in skip_labels):
                    continue

                h_data: Dict[str, Any] = {"name": "", "isin": None, "quantity": 0.0, "avg_cost": 0.0, "price": 0.0, "market_value": 0.0, "weight_pct": 0.0}
                for c_name, cell_val in zip(cols, row):
                    if cell_val is None or not c_name:
                        continue
                    matched_f = None
                    for fk, lbl in h_map.items():
                        if _col_matches_label(c_name, lbl):
                            matched_f = fk
                            break
                    if not matched_f:
                        if "security" in c_name or "scrip" in c_name:
                            matched_f = "name"
                        elif "isin" in c_name:
                            matched_f = "isin"
                        elif "qty" in c_name or "quantity" in c_name:
                            matched_f = "quantity"
                        elif "avg cost" in c_name:
                            matched_f = "avg_cost"
                        elif "price" in c_name:
                            matched_f = "price"
                        elif "value" in c_name:
                            matched_f = "market_value"
                        elif "weight" in c_name:
                            matched_f = "weight_pct"

                    if matched_f in ("name", "isin"):
                        h_data[matched_f] = str(cell_val).strip()
                    elif matched_f in h_data:
                        numeric = float(cell_val or 0)
                        h_data[matched_f] = numeric if matched_f in ("quantity", "market_value", "weight_pct") else abs(numeric)

                total_cost = round(h_data["avg_cost"] * h_data["quantity"], 2)
                holdings.append(Holding(
                    name=h_data["name"],
                    isin=h_data["isin"],
                    quantity=h_data["quantity"],
                    market_value=h_data["market_value"],
                    weight_pct=h_data["weight_pct"],
                    price=h_data["price"],
                    avg_cost=h_data["avg_cost"],
                    total_cost=total_cost
                ))

        # 3. Transactions Sheet
        transactions = []
        t_sheet_name = tx_cfg.get("sheet", "Transactions")
        ws_t = wb[t_sheet_name]
        rows_t = list(ws_t.iter_rows(values_only=True))
        t_map = tx_cfg.get("mapping", {})
        hdr_idx_t = None
        for i, row in enumerate(rows_t):
            if row and any("date" in str(c).lower() for c in row if c):
                hdr_idx_t = i
                break

        type_map = {k.upper(): v for k, v in tx_cfg.get("type_map", {
            "INFLOW": "inflow", "OUTFLOW": "outflow", "BUY": "buy", "SELL": "sell", "DIVIDEND": "dividend", "FEE": "fee"
        }).items()}

        if hdr_idx_t is not None:
            cols_t = [str(c).lower().strip() if c is not None else "" for c in rows_t[hdr_idx_t]]
            for row in rows_t[hdr_idx_t + 1:]:
                if not row or not row[0]:
                    continue
                t_data: Dict[str, Any] = {"date": "", "raw_type": "", "security": None, "isin": None, "quantity": None, "price": None, "amount": 0.0}
                for c_name, cell_val in zip(cols_t, row):
                    if cell_val is None or not c_name:
                        continue
                    matched_f = None
                    for fk, lbl in t_map.items():
                        if _col_matches_label(c_name, lbl):
                            matched_f = fk
                            break
                    if not matched_f:
                        if "date" in c_name:
                            matched_f = "date"
                        elif "type" in c_name:
                            matched_f = "raw_type"
                        elif "security" in c_name:
                            matched_f = "security"
                        elif "isin" in c_name:
                            matched_f = "isin"
                        elif "qty" in c_name or "quantity" in c_name:
                            matched_f = "quantity"
                        elif "price" in c_name:
                            matched_f = "price"
                        elif "amount" in c_name:
                            matched_f = "amount"

                    if matched_f == "date":
                        t_data["date"] = parse_date(str(cell_val))
                    elif matched_f in ("raw_type", "security", "isin"):
                        t_data[matched_f] = str(cell_val).strip()
                    elif matched_f in ("quantity", "price", "amount"):
                        t_data[matched_f] = abs(float(cell_val))

                std_t = type_map.get(t_data["raw_type"].upper(), "other")
                transactions.append(Transaction(
                    date=t_data["date"],
                    type=std_t,
                    raw_type=t_data["raw_type"],
                    security=t_data["security"],
                    isin=t_data["isin"],
                    quantity=t_data["quantity"],
                    price=t_data["price"],
                    amount=t_data["amount"]
                ))

        return [StatementRecord(
            statement=meta,
            summary=summary,
            holdings=holdings,
            transactions=transactions
        )]

    def _parse_csv_sectioned(self, file_path: str, template: Dict[str, Any]) -> List[StatementRecord]:
        """Parses sectioned CSV statements using template section and column mappings."""
        with open(file_path, "r", encoding="utf-8") as f:
            reader = list(csv.reader(f))

        provider = reader[0][0].strip() if reader and reader[0] and reader[0][0] else template.get("fields", {}).get("provider", "Unknown Provider")
        meta_dict: Dict[str, Any] = {"provider": provider, "currency": None}
        tmpl_fields = template.get("fields", {})
        if tmpl_fields.get("currency"):
            meta_dict["currency"] = normalize_currency(tmpl_fields.get("currency"))

        summary_cfg = template.get("summary", {})
        holdings_cfg = template.get("holdings", {})
        tx_cfg = template.get("transactions", {})

        s_map = summary_cfg.get("mapping", {})
        h_map = holdings_cfg.get("mapping", {})
        t_map = tx_cfg.get("mapping", {})

        sum_sections = [s.upper() for s in summary_cfg.get("sections", ["SUMMARY"])]
        hold_sections = [s.upper() for s in holdings_cfg.get("sections", ["HOLDINGS"])]
        tx_sections = [s.upper() for s in tx_cfg.get("sections", ["TRANSACTIONS"])]

        summary_dict: Dict[str, Any] = {
            "opening": 0.0, "inflow": 0.0, "outflow": 0.0,
            "gain": 0.0, "fee": 0.0, "cash": 0.0, "closing": 0.0
        }
        holdings = []
        transactions = []

        current_section = "HEADER"
        h_cols: List[str] = []
        t_cols: List[str] = []

        type_map = {k.upper(): v for k, v in tx_cfg.get("type_map", {
            "INFLOW": "inflow", "OUTFLOW": "outflow", "BUY": "buy", "SELL": "sell", "DIVIDEND": "dividend", "FEE": "fee"
        }).items()}

        for row in reader:
            if not row or not any(row):
                continue
            first = row[0].strip()

            if first.upper() in sum_sections:
                current_section = "SUMMARY"
                continue
            elif first.upper() in hold_sections:
                current_section = "HOLDINGS"
                continue
            elif first.upper() in tx_sections:
                current_section = "TRANSACTIONS"
                continue

            if current_section == "HEADER":
                k = first.lower()
                v = row[1].strip() if len(row) > 1 else ""
                if "client id" in k:
                    meta_dict["client_id"] = v
                elif "client name" in k:
                    meta_dict["client_name"] = v
                elif "pan" in k:
                    meta_dict["pan"] = v
                elif "strategy" in k:
                    meta_dict["strategy"] = v
                elif "currency" in k:
                    meta_dict["currency"] = normalize_currency(v or first)
                elif "period" in k:
                    m = re.search(r"([0-9\-]+)\s+to\s+([0-9\-]+)", v)
                    if m:
                        meta_dict["period_start"] = parse_date(m.group(1))
                        meta_dict["period_end"] = parse_date(m.group(2))

            elif current_section == "SUMMARY":
                k = first.lower()
                raw_val = row[1] if len(row) > 1 else "0"
                matched_s = None
                for fk, lbl in s_map.items():
                    if _col_matches_label(k, lbl):
                        matched_s = fk
                        break
                if matched_s:
                    if matched_s == "gain":
                        summary_dict["gain"] = clean_number(raw_val)
                    else:
                        summary_dict[matched_s] = abs(clean_number(raw_val))
                else:
                    v = abs(clean_number(raw_val))
                    if "opening" in k:
                        summary_dict["opening"] = v
                    elif "inflow" in k:
                        summary_dict["inflow"] = v
                    elif "outflow" in k:
                        summary_dict["outflow"] = v
                    elif "gain" in k:
                        summary_dict["gain"] = clean_number(raw_val)
                    elif "fee" in k:
                        summary_dict["fee"] = v
                    elif "cash" in k:
                        summary_dict["cash"] = v
                    elif "closing" in k:
                        summary_dict["closing"] = v

            elif current_section == "HOLDINGS":
                if not h_cols and any(k in first.lower() for k in ["security", "scrip"]):
                    h_cols = [c.lower().strip() for c in row]
                    continue
                h_total_labels = [lbl.lower().strip() for lbl in holdings_cfg.get("total_row_labels", [])]
                h_cash_labels = [lbl.lower().strip() for lbl in holdings_cfg.get("cash_row_labels", [])]
                fallback_used = False
                if not h_total_labels:
                    h_total_labels = ["total", "grand total", "total equity"]
                    fallback_used = True
                if not h_cash_labels:
                    h_cash_labels = ["cash", "cash balance", "cash & margin", "liquid cash reserve"]
                    fallback_used = True
                if fallback_used:
                    soft_list = summary_dict.setdefault("soft_issues", [])
                    if "template_missing_row_labels" not in soft_list:
                        soft_list.append("template_missing_row_labels")
                skip_labels = set(h_total_labels + h_cash_labels + ["total", "grand total", "total equity", "cash"])
                if any(lbl in first.lower().strip() for lbl in skip_labels):
                    continue

                h_data: Dict[str, Any] = {"name": first, "isin": None, "quantity": 0.0, "avg_cost": 0.0, "price": 0.0, "market_value": 0.0, "weight_pct": 0.0}
                for c_name, cell_val in zip(h_cols, row):
                    if not cell_val or not c_name:
                        continue
                    matched_f = None
                    for fk, lbl in h_map.items():
                        if _col_matches_label(c_name, lbl):
                            matched_f = fk
                            break
                    if not matched_f:
                        if "security" in c_name:
                            matched_f = "name"
                        elif "isin" in c_name:
                            matched_f = "isin"
                        elif "qty" in c_name:
                            matched_f = "quantity"
                        elif "avg cost" in c_name:
                            matched_f = "avg_cost"
                        elif "price" in c_name:
                            matched_f = "price"
                        elif "value" in c_name:
                            matched_f = "market_value"
                        elif "weight" in c_name:
                            matched_f = "weight_pct"

                    if matched_f in ("name", "isin"):
                        h_data[matched_f] = cell_val.strip()
                    elif matched_f in h_data:
                        numeric = clean_number(cell_val)
                        h_data[matched_f] = numeric if matched_f in ("quantity", "market_value", "weight_pct") else abs(numeric)

                tot_c = round(h_data["avg_cost"] * h_data["quantity"], 2)
                holdings.append(Holding(
                    name=h_data["name"],
                    isin=h_data["isin"],
                    quantity=h_data["quantity"],
                    market_value=h_data["market_value"],
                    weight_pct=h_data["weight_pct"],
                    price=h_data["price"],
                    avg_cost=h_data["avg_cost"],
                    total_cost=tot_c
                ))

            elif current_section == "TRANSACTIONS":
                if not t_cols and "date" in first.lower():
                    t_cols = [c.lower().strip() for c in row]
                    continue

                t_data: Dict[str, Any] = {"date": "", "raw_type": "", "security": None, "quantity": None, "price": None, "amount": 0.0}
                for c_name, cell_val in zip(t_cols, row):
                    if not cell_val or not c_name:
                        continue
                    matched_f = None
                    for fk, lbl in t_map.items():
                        if _col_matches_label(c_name, lbl):
                            matched_f = fk
                            break
                    if not matched_f:
                        if "date" in c_name:
                            matched_f = "date"
                        elif "type" in c_name:
                            matched_f = "raw_type"
                        elif "security" in c_name:
                            matched_f = "security"
                        elif "qty" in c_name:
                            matched_f = "quantity"
                        elif "price" in c_name:
                            matched_f = "price"
                        elif "amount" in c_name:
                            matched_f = "amount"

                    if matched_f == "date":
                        t_data["date"] = parse_date(cell_val)
                    elif matched_f in ("raw_type", "security"):
                        t_data[matched_f] = cell_val.strip()
                    elif matched_f in ("quantity", "price", "amount"):
                        t_data[matched_f] = abs(clean_number(cell_val))

                std_t = type_map.get(t_data["raw_type"].upper(), "other")
                isin = resolve_isin_from_holdings(t_data["security"], holdings)

                transactions.append(Transaction(
                    date=t_data["date"],
                    type=std_t,
                    raw_type=t_data["raw_type"],
                    security=t_data["security"],
                    isin=isin,
                    quantity=t_data["quantity"],
                    price=t_data["price"],
                    amount=t_data["amount"]
                ))

        if not meta_dict.get("currency"):
            curr_pat = tmpl_fields.get("currency_pattern")
            for row in reader[:35]:
                row_str = " ".join(str(cell) for cell in row if cell)
                if curr_pat:
                    cm = re.search(curr_pat, row_str, re.IGNORECASE)
                    if cm:
                        meta_dict["currency"] = normalize_currency(cm.group(1) if cm.groups() else cm.group(0))
                        break
                m = re.search(r"(?:Base\s*Currency|Portfolio\s*Currency|Currency)[ \t]*[:\-\s,][ \t]*([A-Za-z]{3}|Rs\.?|₹|\$)", row_str, re.IGNORECASE)
                if m:
                    meta_dict["currency"] = normalize_currency(m.group(1))
                    break
                if "(INR)" in row_str or "(Rs.)" in row_str:
                    meta_dict["currency"] = "INR"
                    break
                elif "(USD)" in row_str or "($)" in row_str:
                    meta_dict["currency"] = "USD"
                    break

        return [StatementRecord(
            statement=StatementMeta(**meta_dict),
            summary=Summary(**summary_dict),
            holdings=holdings,
            transactions=transactions
        )]

"""
utils.py
--------
Utility functions for text, number, date, and ISIN handling.
Handles accounting sign conventions, Indian Lakhs/Crores, diverse date patterns,
and ISO 6166 Mod-10 Luhn check digit validation.
"""

import re
import hashlib
from datetime import datetime
from typing import Optional, Any
from dateutil import parser as dateutil_parser


def compute_sha256(file_path: str) -> str:
    """Computes SHA-256 hash of a file's raw content."""
    hasher = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


def clean_number(val: Any) -> Optional[float]:
    """
    Cleans raw strings containing currency, commas, negative signs, Dr/Cr, or accounting brackets.
    Examples:
      '1,00,000.00'   -> 100000.0
      '-INR 12,500.0' -> -12500.0
      '(Rs. 18,250)'  -> -18250.0
      '1,234.00-'     -> -1234.0
      '−1,234.00'     -> -1234.0 (unicode minus)
      '1,234.00 Dr'   -> -1234.0
      '1.5 Lakhs'     -> 150000.0
      '21.52%'        -> 21.52
      '-' or ''       -> None
    """
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)

    s = str(val).strip()
    if not s or s in ("-", "--", "N/A", "NA", "null", "None"):
        return None

    # Replace unicode minuses and dashes
    s = s.replace("\u2212", "-").replace("—", "-").replace("–", "-")

    # Detect accounting brackets: (12,500.00) or (Rs. 12,500.00) -> negative
    is_negative = False
    if s.startswith("(") and s.endswith(")"):
        is_negative = True
        s = s[1:-1].strip()

    # Detect trailing minus: e.g. "1,234.00-"
    if s.endswith("-"):
        is_negative = True
        s = s[:-1].strip()

    # Detect leading minus sign
    if s.startswith("-"):
        is_negative = True
        s = s[1:].strip()

    # Detect Dr / Cr suffixes (Debit indicates negative balance / expense in Indian accounting)
    multiplier = 1.0
    if s.upper().endswith(" DR"):
        is_negative = True
        s = s[:-3].strip()
    elif s.upper().endswith(" CR"):
        s = s[:-3].strip()
    # Support Indian financial number naming: Lakh (10^5) and Crore (10^7)
    elif re.search(r"(?i)\s+lakhs?$", s):
        s = re.sub(r"(?i)\s+lakhs?$", "", s).strip()
        multiplier = 100000.0
    elif re.search(r"(?i)\s+crores?$", s):
        s = re.sub(r"(?i)\s+crores?$", "", s).strip()
        multiplier = 10000000.0

    # Strip currency words, symbols, and percentage signs
    s = re.sub(r"(?i)\b(?:inr|usd)\b|rs\.?", "", s)
    s = re.sub(r"[₹$,%]", "", s)
    s = s.strip()

    # Handle remaining minus if any was embedded
    if s.startswith("-"):
        is_negative = True
        s = s[1:].strip()
    if s.endswith("-"):
        is_negative = True
        s = s[:-1].strip()

    # Remove all formatting commas
    s = s.replace(",", "").strip()

    try:
        num = float(s) * multiplier
        if is_negative:
            num = -abs(num)
        return num
    except ValueError:
        return None


def parse_date(date_str: str, dayfirst: bool = True) -> Optional[str]:
    """
    Normalizes diverse date formats to standard ISO format 'YYYY-MM-DD'.
    Supports Indian/UK dayfirst and US formats (when dayfirst=False).
    """
    if not date_str:
        return None

    s = str(date_str).strip()
    # Explicit ISO check first: YYYY-MM-DD
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        return s

    if not dayfirst:
        us_patterns = [
            ("%m/%d/%Y", r"^\d{1,2}/\d{1,2}/\d{4}$"),
            ("%m-%d-%Y", r"^\d{1,2}-\d{1,2}-\d{4}$"),
            ("%b %d, %Y", r"^[A-Za-z]{3}\s+\d{1,2},\s+\d{4}$"),
            ("%B %d, %Y", r"^[A-Za-z]+\s+\d{1,2},\s+\d{4}$"),
        ]
        for fmt, regex in us_patterns:
            if re.match(regex, s, re.IGNORECASE):
                try:
                    dt = datetime.strptime(s, fmt)
                    return dt.strftime("%Y-%m-%d")
                except ValueError:
                    continue
        try:
            dt = dateutil_parser.parse(s, dayfirst=False)
            return dt.strftime("%Y-%m-%d")
        except Exception:
            pass

    # Common Indian/UK formats (day first)
    day_first_patterns = [
        ("%d-%b-%Y", r"^\d{1,2}-[A-Za-z]{3}-\d{4}$"),
        ("%d/%m/%Y", r"^\d{1,2}/\d{1,2}/\d{4}$"),
        ("%d.%m.%Y", r"^\d{1,2}\.\d{1,2}\.\d{4}$"),
        ("%d-%m-%Y", r"^\d{1,2}-\d{1,2}-\d{4}$"),
        ("%d %b %Y", r"^\d{1,2}\s+[A-Za-z]{3}\s+\d{4}$"),
        ("%d %B %Y", r"^\d{1,2}\s+[A-Za-z]+\s+\d{4}$"),
        ("%d-%B-%Y", r"^\d{1,2}-[A-Za-z]+-\d{4}$"),
    ]

    for fmt, regex in day_first_patterns:
        if re.match(regex, s, re.IGNORECASE):
            try:
                dt = datetime.strptime(s, fmt)
                return dt.strftime("%Y-%m-%d")
            except ValueError:
                continue

    # Fallback to dateutil parser
    try:
        dt = dateutil_parser.parse(s, dayfirst=True)
        return dt.strftime("%Y-%m-%d")
    except Exception:
        pass

    # Regex search inside sentence/cell
    match = re.search(r"(\d{1,2})[-/\.](\d{1,2}|[A-Za-z]{3})[-/\.](\d{4})", s)
    if match:
        raw = match.group(0)
        try:
            dt = dateutil_parser.parse(raw, dayfirst=True)
            return dt.strftime("%Y-%m-%d")
        except Exception:
            pass

    return None


def is_valid_isin(isin: Optional[str]) -> bool:
    """
    Validates a 12-character ISIN using the standard ISO 6166 Mod-10 Luhn check digit algorithm.
    Returns True for valid ISINs, False for invalid checksums or malformed strings.
    """
    if not isin or not isinstance(isin, str):
        return False
    
    isin = isin.strip().upper()
    # Step 1: Every standard ISIN is strictly 12 characters
    if len(isin) != 12:
        return False
    
    # Step 2: First 2 chars must be alphabetical country code (e.g., 'IN' for India, 'US' for USA)
    if not (isin[0].isalpha() and isin[1].isalpha()):
        return False

    # Step 3: Convert letters to digits: A=10, B=11, ..., Z=35 (numbers stay 0-9)
    digits = ""
    for char in isin:
        if char.isdigit():
            digits += char
        elif char.isalpha():
            digits += str(ord(char) - ord("A") + 10)
        else:
            return False

    # Step 4: Mod-10 Luhn check: Double every second digit starting from the rightmost digit
    total = 0
    reverse_digits = digits[::-1]
    for i, d in enumerate(reverse_digits):
        n = int(d)
        if i % 2 == 1:
            n *= 2
            # If doubling produces a two-digit number (e.g. 14), sum the two digits: 1 + 4 = 5
            if n > 9:
                n = (n // 10) + (n % 10)
        total += n

    # Step 5: Checksum is valid if the grand total is an exact multiple of 10
    return (total % 10) == 0


def normalise_header(h: str) -> str:
    if not h:
        return ""
    s = str(h).lower()
    s = re.sub(r"\((?:rs\.?|inr|qty|amount)\)", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s*%\s*", "", s)
    s = s.rstrip(":")
    s = re.sub(r"\s+", "", s)
    return s


def match_header_with_fuzzy(target_label: str, candidate_headers: list) -> tuple:
    from difflib import SequenceMatcher
    if not target_label or not candidate_headers:
        return None, None
    norm_target = normalise_header(target_label)
    exact_matches = [c for c in candidate_headers if normalise_header(c) == norm_target]
    if len(exact_matches) == 1:
        return exact_matches[0], None
    if len(exact_matches) > 1:
        return None, "ambiguous_header"
    fuzzy_matches = [c for c in candidate_headers if SequenceMatcher(None, normalise_header(c), norm_target).ratio() >= 0.9]
    if len(fuzzy_matches) == 1:
        return fuzzy_matches[0], None
    if len(fuzzy_matches) > 1:
        return None, "ambiguous_header"
    return None, None


SUPPORTED_CURRENCIES = {
    "INR", "USD", "EUR", "GBP", "AED", "SGD", "JPY", "AUD", "CAD", "CHF", "HKD"
}


def normalize_currency(val: Optional[str]) -> Optional[str]:
    """
    Normalizes raw currency strings or symbols to standard ISO 4217 code.
    Returns None if currency is missing, empty, unrecognized, or unsupported.
    """
    if not val:
        return None
    s = str(val).strip().upper()
    if not s or s in ("NONE", "NULL", "N/A", "UNKNOWN", "-"):
        return None

    # Direct ISO code match
    if s in SUPPORTED_CURRENCIES:
        return s

    # Common aliases and symbols
    if s in ("RS", "RS.", "RUPEE", "RUPEES", "₹", "INR.", "IN RUPEES", "INDIAN RUPEE", "INDIAN RUPEES"):
        return "INR"
    if s in ("$", "US$", "USD.", "DOLLAR", "DOLLARS", "US DOLLAR", "US DOLLARS"):
        return "USD"
    if s in ("€", "EUR.", "EURO", "EUROS"):
        return "EUR"
    if s in ("£", "GBP.", "POUND", "POUNDS", "STERLING", "BRITISH POUND"):
        return "GBP"
    if s in ("AED.", "DIRHAM", "DIRHAMS", "UAE DIRHAM"):
        return "AED"
    if s in ("SGD.", "S$", "SINGAPORE DOLLAR"):
        return "SGD"

    # Search for ISO code as whole word in string
    m = re.search(r"\b(INR|USD|EUR|GBP|AED|SGD|JPY|AUD|CAD|CHF|HKD)\b", s)
    if m:
        return m.group(1)

    # Search for symbol / abbreviation inside string
    if "RS." in s or "RUPEE" in s or "₹" in s:
        return "INR"
    if "$" in s or "DOLLAR" in s:
        return "USD"
    if "€" in s or "EURO" in s:
        return "EUR"
    if "£" in s or "POUND" in s:
        return "GBP"

    return None


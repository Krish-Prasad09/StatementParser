"""
models.py
---------
Core data models for the PMS/AIF Statement Parser.
Uses Pydantic for clean type validation and easy serialization to JSON.
"""

from typing import List, Optional, Dict, Any, Literal
from pydantic import BaseModel, Field


# ==============================================================================
# 1. PARSED FINANCIAL DOMAIN MODELS
# These models represent the structured financial output extracted from a statement.
# ==============================================================================

class StatementMeta(BaseModel):
    """Metadata describing the statement header and client identity."""
    provider: str                       # AMC / Portfolio Manager name (e.g., 'Alchemy Capital Management')
    strategy: Optional[str] = None      # Investment strategy / Scheme (e.g., 'High Growth PMS')
    client_id: str                      # Unique client account/folio code (e.g., 'ALCH10293')
    client_name: str                    # Investor / Entity legal name (e.g., 'Rahul Sharma')
    pan: Optional[str] = None           # Indian Permanent Account Number (10-char alphanumeric)
    period_start: Optional[str] = None  # YYYY-MM-DD (start of accounting window; None for snapshot balance sheets)
    period_end: str                     # YYYY-MM-DD (as-on valuation date or period close)
    currency: Optional[str] = None      # ISO currency code ('INR', 'USD', etc.; None if missing to prevent blind default)


class Summary(BaseModel):
    """Overall corpus and performance summary."""
    # extra="allow" permits custom AMC-specific rollforward attributes without failing validation
    model_config = {"extra": "allow"}
    opening: float = 0.0                # Corpus valuation at start of period
    inflow: float = 0.0                 # Fresh capital additions / subscriptions
    outflow: float = 0.0                # Capital withdrawals / redemptions
    gain: Optional[float] = None        # null when statement has no gain column (e.g. Banyan)
    fee: float = 0.0                    # Total expenses/charges deducted during period
    closing: float = 0.0                # Closing valuation / Net Asset Value (NAV) of portfolio
    mgmt_fee: Optional[float] = None    # Periodic management fee component
    perf_fee: Optional[float] = None    # Performance fee / profit share (carry)
    units: Optional[float] = None       # Total units held (primarily for AIF Cat III funds)
    nav_per_unit: Optional[float] = None# NAV per unit on valuation date


class Holding(BaseModel):
    """A single security or fund holding in the portfolio."""
    model_config = {"extra": "allow"}
    name: str                           # Company / Security display name (e.g., 'Reliance Industries Ltd')
    isin: Optional[str] = None          # 12-character ISO 6166 security identifier (e.g., 'INE002A01018')
    quantity: float                     # Number of shares / units owned
    market_value: float                 # Current holding valuation (quantity * price)
    weight_pct: Optional[float] = None  # Allocation percentage of total portfolio
    price: Optional[float] = None       # Market price per unit on statement date
    avg_cost: Optional[float] = None    # Acquisition cost per unit
    total_cost: Optional[float] = None  # Total purchase cost basis (quantity * avg_cost)
    side: Optional[str] = None          # 'long' (equity ownership) or 'short' (derivative/borrowed liability)
    instrument: Optional[str] = None    # Asset class: 'Equity', 'Debt', 'Mutual Fund', 'Cash & Equivalent'


class Transaction(BaseModel):
    """A single buy, sell, corporate action, fee, or capital flow."""
    date: str                           # YYYY-MM-DD
    type: str                           # buy | sell | dividend | fee | inflow | outflow | other
    raw_type: str                       # Label as printed in the original document
    security: Optional[str] = None      # Name of traded asset
    isin: Optional[str] = None          # ISIN of traded asset
    quantity: Optional[float] = None    # Units traded
    price: Optional[float] = None       # Execution price per unit
    amount: float                       # Always positive float; direction is dictated by type


class StatementRecord(BaseModel):
    """A complete extracted statement record."""
    statement: StatementMeta            # Document header identity and date ranges
    summary: Summary                    # Account-level rollforward balances
    holdings: List[Holding] = Field(default_factory=list)      # Portfolio position breakdown
    transactions: List[Transaction] = Field(default_factory=list) # Audit trail of trades and cash flows


class ExtractionResult(BaseModel):
    """The full result of ingesting and parsing a file."""
    file: str                           # Source file path on disk
    format_id: Optional[str] = None     # Matched template ID (e.g., 'ask_growth_pms')
    status: str                         # validated | needs_review | duplicate | not_a_statement
    issues: List[str] = Field(default_factory=list) # Diagnostic codes flagged by validator
    statements: List[StatementRecord] = Field(default_factory=list) # Extracted statement records (1+ for multi-statement PDFs)
    notes: Optional[str] = None         # Human-readable status notes
    diagnostic_message: Optional[str] = None # Detailed error or trace information if failed
    llm_calls_made: int = 0             # Invariant proof: 0 for known, 1 for new learning
    sha256_hash: Optional[str] = None   # Content fingerprint to guarantee idempotent deduplication


# ==============================================================================
# 2. DECLARATIVE TEMPLATE SCHEMAS
# These models define the deterministic "recipes" generated by the AI Learner
# and used by the Parser to extract statements without repeating LLM calls.
# ==============================================================================

class TemplateFingerprint(BaseModel):
    """Structural anchor headers for format matching and layout drift detection."""
    model_config = {"extra": "allow"}
    table_headers: List[str] = Field(default_factory=list)      # Flattened column header signatures
    summary_headers: List[str] = Field(default_factory=list)    # Key labels in rollforward summary block
    holdings_headers: List[str] = Field(default_factory=list)   # Column titles in holdings table
    transactions_headers: List[str] = Field(default_factory=list) # Column titles in trade ledger
    section_titles: List[str] = Field(default_factory=list)     # Major section heading text


class TemplateFields(BaseModel):
    """Metadata extraction patterns for client_id, client_name, pan, strategy, and provider."""
    model_config = {"extra": "allow"}
    provider: Optional[str] = None              # Static provider name if permanently known
    provider_pattern: Optional[str] = None      # Regex to isolate AMC name from page header
    client_id_pattern: Optional[str] = None     # Regex with capture group for account / folio code
    client_name_pattern: Optional[str] = None   # Regex with capture group for investor name
    pan_pattern: Optional[str] = None           # Regex with capture group for 10-char PAN
    strategy_pattern: Optional[str] = None      # Regex with capture group for investment mandate/scheme
    currency_pattern: Optional[str] = None      # Regex to capture denomination (e.g., INR, USD)
    currency: Optional[str] = None              # Static currency override if uniform across all statements


class TemplatePeriod(BaseModel):
    """Rules to extract period start/end and date formats."""
    model_config = {"extra": "allow"}
    pattern: Optional[str] = None       # Regex capturing 1, 2, or 4 date elements
    format: Optional[str] = None        # Date format string (e.g., '%d-%b-%Y')
    infer_start: Optional[str] = None   # Strategy for single dates (e.g., 'first_of_month')


class TemplateSummary(BaseModel):
    """Summary label-to-field mapping and flags."""
    model_config = {"extra": "allow"}
    has_gain: bool = True               # True if document reports P&L explicitly
    derive_fee_sum: bool = False        # True if fees must be summed from itemized components
    section_header: Optional[str] = None# Section anchor title (e.g., 'Performance Summary')
    columns_order: Optional[List[str]] = None # Order of values in horizontal summary grids
    mapping: Dict[str, str] = Field(default_factory=dict) # Maps canonical fields -> document labels
    sheet: Optional[str] = None         # Specific Excel sheet name containing summary data
    sections: List[str] = Field(default_factory=list)     # Multi-section headers


class TemplateHoldings(BaseModel):
    """Holdings column mapping, row regex, and structural flags."""
    model_config = {"extra": "allow"}
    mapping: Dict[str, str] = Field(default_factory=dict) # Standard fields (name, isin, qty) -> table headers
    regex: Optional[str] = None         # Fallback regex for monospace plain text parsing
    section_header: Optional[str] = None# Anchor text indicating start of holdings grid
    end_markers: List[str] = Field(default_factory=list)  # Text marking table boundary (e.g., 'Total Portfolio')
    total_row_labels: List[str] = Field(default_factory=list) # Row labels to exclude as summary totals
    cash_row_labels: List[str] = Field(default_factory=list)  # Labels denoting bank balance / liquid cash
    nav_row_labels: List[str] = Field(default_factory=list)   # Labels identifying NAV line
    gross_long_labels: List[str] = Field(default_factory=list)# Labels identifying long equity sum
    gross_short_labels: List[str] = Field(default_factory=list)# Labels identifying short/derivative obligations
    supports_short: bool = False        # True for long-short hedge funds / AIF Cat III
    is_unlisted: bool = False           # True for private equity / venture capital holdings
    derive_price: bool = False          # Calculate price = market_value / quantity if missing
    derive_avg_cost: bool = False       # Calculate avg_cost = total_cost / quantity if missing
    sheet: Optional[str] = None         # Specific Excel sheet name for holdings
    header_row: Optional[int] = None    # 1-based row index of table header in spreadsheet
    sections: List[str] = Field(default_factory=list) # Sub-category headers (e.g., 'Large Cap', 'Mid Cap')


class TemplateTransactions(BaseModel):
    """Transactions column mapping, row regex, and raw_type to enum mapping."""
    model_config = {"extra": "allow"}
    mapping: Dict[str, str] = Field(default_factory=dict) # Standard fields (date, type, amount) -> table headers
    regex: Optional[str] = None         # Fallback regex for monospace plain text trade logs
    section_header: Optional[str] = None# Header title marking trade activity table
    end_markers: List[str] = Field(default_factory=list)  # Stop words marking end of transactions
    type_map: Dict[str, str] = Field(default_factory=dict)# Maps raw AMC text ('PUR', 'SL') -> canonical enum ('buy', 'sell')
    sheet: Optional[str] = None         # Excel sheet name for trade activity
    header_row: Optional[int] = None    # Spreadsheet row index containing trade columns
    sections: List[str] = Field(default_factory=list)     # Section names


class FormatTemplate(BaseModel):
    """
    Declarative layout template stored in templates/<format_id>.json.
    Contains rules to extract data deterministically with zero LLM calls.
    """
    format_id: str                      # Unique format key (e.g., 'alchemy_capital_monthly')
    version: int = 1                    # Schema version (increments on layout drift: _v2)
    layout_kind: Literal["ruled_table", "monospace_text", "ruled_lines_horizontal_only", "xlsx", "csv_sectioned"]
    provider_key: str                   # Normalized AMC slug for provider clustering
    sebi_regn: Optional[str] = None     # SEBI Registration number (e.g., 'INP000001234')
    approved: bool = True               # True if passed self-check validation test drive
    fingerprint: TemplateFingerprint    # Anchor signatures for fast similarity scoring
    fields: TemplateFields              # Regex patterns for client and account identifiers
    period: TemplatePeriod = Field(default_factory=TemplatePeriod) # Accounting date range extraction rules
    summary: TemplateSummary            # Corpus rollforward mapping configuration
    holdings: TemplateHoldings          # Security position table extraction configuration
    transactions: TemplateTransactions  # Trade ledger extraction configuration
    learned_from: Optional[Dict[str, str]] = None # Audit trail of sample file used during one-shot AI training


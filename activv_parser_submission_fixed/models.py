"""
models.py
---------
Core data models for the PMS/AIF Statement Parser.
Uses Pydantic for clean type validation and easy serialization to JSON.
"""

from typing import List, Optional, Dict, Any, Literal
from pydantic import BaseModel, Field


class StatementMeta(BaseModel):
    """Metadata describing the statement header and client identity."""
    provider: str
    strategy: Optional[str] = None
    client_id: str
    client_name: str
    pan: Optional[str] = None
    period_start: Optional[str] = None  # YYYY-MM-DD
    period_end: str                     # YYYY-MM-DD
    currency: Optional[str] = None


class Summary(BaseModel):
    """Overall corpus and performance summary."""
    model_config = {"extra": "allow"}
    opening: float = 0.0
    inflow: float = 0.0
    outflow: float = 0.0
    gain: Optional[float] = None        # null when statement has no gain column (e.g. Banyan)
    fee: float = 0.0
    cash: float = 0.0
    closing: float = 0.0
    mgmt_fee: Optional[float] = None
    perf_fee: Optional[float] = None
    units: Optional[float] = None
    nav_per_unit: Optional[float] = None


class Holding(BaseModel):
    """A single security or fund holding in the portfolio."""
    model_config = {"extra": "allow"}
    name: str
    isin: Optional[str] = None
    quantity: float
    market_value: float
    weight_pct: Optional[float] = None
    price: Optional[float] = None
    avg_cost: Optional[float] = None
    total_cost: Optional[float] = None
    side: Optional[str] = None
    instrument: Optional[str] = None


class Transaction(BaseModel):
    """A single buy, sell, corporate action, fee, or capital flow."""
    date: str                           # YYYY-MM-DD
    type: str                           # buy | sell | dividend | fee | inflow | outflow | other
    raw_type: str                       # Label as printed in the original document
    security: Optional[str] = None
    isin: Optional[str] = None
    quantity: Optional[float] = None
    price: Optional[float] = None
    amount: float                       # Always positive float; direction is dictated by type


class StatementRecord(BaseModel):
    """A complete extracted statement record."""
    statement: StatementMeta
    summary: Summary
    holdings: List[Holding] = Field(default_factory=list)
    transactions: List[Transaction] = Field(default_factory=list)


class ExtractionResult(BaseModel):
    """The full result of ingesting and parsing a file."""
    file: str
    format_id: Optional[str] = None
    status: str                         # validated | needs_review | duplicate | not_a_statement
    issues: List[str] = Field(default_factory=list)
    statements: List[StatementRecord] = Field(default_factory=list)
    notes: Optional[str] = None
    diagnostic_message: Optional[str] = None
    llm_calls_made: int = 0             # Invariant proof: 0 for known, 1 for new learning
    sha256_hash: Optional[str] = None


class TemplateFingerprint(BaseModel):
    """Structural anchor headers for format matching and layout drift detection."""
    model_config = {"extra": "allow"}
    table_headers: List[str] = Field(default_factory=list)
    summary_headers: List[str] = Field(default_factory=list)
    holdings_headers: List[str] = Field(default_factory=list)
    transactions_headers: List[str] = Field(default_factory=list)
    section_titles: List[str] = Field(default_factory=list)


class TemplateFields(BaseModel):
    """Metadata extraction patterns for client_id, client_name, pan, strategy, and provider."""
    model_config = {"extra": "allow"}
    provider: Optional[str] = None
    provider_pattern: Optional[str] = None
    client_id_pattern: Optional[str] = None
    client_name_pattern: Optional[str] = None
    pan_pattern: Optional[str] = None
    strategy_pattern: Optional[str] = None
    currency_pattern: Optional[str] = None
    currency: Optional[str] = None


class TemplatePeriod(BaseModel):
    """Rules to extract period start/end and date formats."""
    model_config = {"extra": "allow"}
    pattern: Optional[str] = None
    format: Optional[str] = None
    infer_start: Optional[str] = None


class TemplateSummary(BaseModel):
    """Summary label-to-field mapping and flags."""
    model_config = {"extra": "allow"}
    has_gain: bool = True
    derive_fee_sum: bool = False
    section_header: Optional[str] = None
    columns_order: Optional[List[str]] = None
    mapping: Dict[str, str] = Field(default_factory=dict)
    sheet: Optional[str] = None
    sections: List[str] = Field(default_factory=list)


class TemplateHoldings(BaseModel):
    """Holdings column mapping, row regex, and structural flags."""
    model_config = {"extra": "allow"}
    mapping: Dict[str, str] = Field(default_factory=dict)
    regex: Optional[str] = None
    section_header: Optional[str] = None
    end_markers: List[str] = Field(default_factory=list)
    total_row_labels: List[str] = Field(default_factory=list)
    cash_row_labels: List[str] = Field(default_factory=list)
    nav_row_labels: List[str] = Field(default_factory=list)
    gross_long_labels: List[str] = Field(default_factory=list)
    gross_short_labels: List[str] = Field(default_factory=list)
    supports_short: bool = False
    is_unlisted: bool = False
    derive_price: bool = False
    derive_avg_cost: bool = False
    sheet: Optional[str] = None
    header_row: Optional[int] = None
    sections: List[str] = Field(default_factory=list)


class TemplateTransactions(BaseModel):
    """Transactions column mapping, row regex, and raw_type to enum mapping."""
    model_config = {"extra": "allow"}
    mapping: Dict[str, str] = Field(default_factory=dict)
    regex: Optional[str] = None
    section_header: Optional[str] = None
    end_markers: List[str] = Field(default_factory=list)
    type_map: Dict[str, str] = Field(default_factory=dict)
    sheet: Optional[str] = None
    header_row: Optional[int] = None
    sections: List[str] = Field(default_factory=list)


class FormatTemplate(BaseModel):
    """
    Declarative layout template stored in templates/<format_id>.json.
    Contains rules to extract data deterministically with zero LLM calls.
    """
    format_id: str
    version: int = 1
    layout_kind: Literal["ruled_table", "monospace_text", "ruled_lines_horizontal_only", "xlsx", "csv_sectioned"]
    provider_key: str
    sebi_regn: Optional[str] = None
    approved: bool = True
    fingerprint: TemplateFingerprint
    fields: TemplateFields
    period: TemplatePeriod = Field(default_factory=TemplatePeriod)
    summary: TemplateSummary
    holdings: TemplateHoldings
    transactions: TemplateTransactions
    learned_from: Optional[Dict[str, str]] = None


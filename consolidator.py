"""
consolidator.py
---------------
Multi-provider portfolio consolidation engine.
Aggregates statements across multiple PMS/AIF providers for a single client.
Rules:
- Filters for latest validated statement per account/provider for the reporting period.
- Supports multiple accounts under the same wealth manager without overwriting.
- Segregates currencies (INR vs USD) to prevent invalid cross-currency addition.
- Joins listed securities by ISIN (sums quantities and market values).
- Keeps unlisted AIF holdings in a dedicated separate bucket (since they have no ISIN).
- Computes overall corpus, total cash, and asset allocation breakdown (% Equity, % AIF, % Cash).
- Explicitly tracks and reports excluded statements with reasons (unvalidated, currency mismatch, etc.).
"""

from typing import List, Dict, Any, Optional, Tuple
from collections import defaultdict
from pydantic import BaseModel, Field

from models import StatementRecord, ExtractionResult


class ConsolidatedHolding(BaseModel):
    """A consolidated security holding joined across multiple provider accounts."""
    isin: str
    name: str
    quantity: float
    market_value: float
    total_cost: float
    weight_pct: float
    providers_held_in: List[str]


class ConsolidatedPortfolio(BaseModel):
    """Client-wide consolidated wealth portfolio."""
    client_name: str
    pan: str
    period: str
    currency: str
    total_corpus: float
    total_cash: float
    total_equity_value: float
    total_aif_value: float
    asset_allocation: Dict[str, float]  # e.g. {"Listed Equity": 61.15, "AIF Alternatives": 35.48, "Cash": 3.36}
    participating_providers: List[str]
    listed_holdings: List[ConsolidatedHolding]
    aif_holdings: List[Dict[str, Any]]
    raw_statements_count: int
    excluded_statements: List[Dict[str, Any]] = []


class PortfolioConsolidator:
    """Aggregates multi-account holdings and summaries for high-net-worth clients."""

    def consolidate_client(
        self,
        results: List[ExtractionResult],
        target_pan: str,
        target_period_end: Optional[str] = None,
        target_currency: str = "INR"
    ) -> Optional[ConsolidatedPortfolio]:
        """
        Consolidates portfolio for a specific PAN.
        Takes latest validated statement per account for the target period and currency.
        Tracks all excluded statements with specific rejection reasons.
        """
        if not target_pan:
            return None

        excluded: List[Dict[str, Any]] = []
        candidate_records: List[StatementRecord] = []

        for r in results:
            if r.status != "validated":
                excluded.append({
                    "source_file": r.file,
                    "reason": "unvalidated_status",
                    "status": r.status,
                    "issues": r.issues
                })
                continue

            for stmt in r.statements:
                if stmt.statement.pan != target_pan:
                    excluded.append({
                        "source_file": r.file,
                        "provider": stmt.statement.provider,
                        "reason": "pan_mismatch",
                        "statement_pan": stmt.statement.pan,
                        "target_pan": target_pan
                    })
                    continue

                if not stmt.statement.currency:
                    excluded.append({
                        "source_file": r.file,
                        "provider": stmt.statement.provider,
                        "reason": "unverified_currency",
                        "statement_currency": stmt.statement.currency
                    })
                    continue

                if stmt.statement.currency != target_currency:
                    excluded.append({
                        "source_file": r.file,
                        "provider": stmt.statement.provider,
                        "reason": "currency_mismatch",
                        "statement_currency": stmt.statement.currency,
                        "target_currency": target_currency
                    })
                    continue

                if target_period_end is not None and stmt.statement.period_end != target_period_end:
                    excluded.append({
                        "source_file": r.file,
                        "provider": stmt.statement.provider,
                        "reason": "period_mismatch",
                        "statement_period": stmt.statement.period_end,
                        "target_period": target_period_end
                    })
                    continue

                candidate_records.append(stmt)

        if not candidate_records:
            return None

        # 2. Select latest statement per account key (provider + client_id + strategy)
        # Prevents double-counting if duplicate/resend statements exist for the exact same strategy/account
        by_account: Dict[Tuple[str, str, str], StatementRecord] = {}
        for rec in candidate_records:
            strat_key = (rec.statement.strategy or "").strip().lower()
            acc_key = (rec.statement.provider.strip().lower(), (rec.statement.client_id or "default").strip().upper(), strat_key)
            if acc_key not in by_account:
                by_account[acc_key] = rec
            else:
                existing = by_account[acc_key]
                if rec.statement.period_end > existing.statement.period_end:
                    excluded.append({
                        "provider": existing.statement.provider,
                        "client_id": existing.statement.client_id,
                        "strategy": existing.statement.strategy,
                        "reason": "superseded_by_newer_statement",
                        "older_period": existing.statement.period_end,
                        "newer_period": rec.statement.period_end
                    })
                    by_account[acc_key] = rec
                else:
                    excluded.append({
                        "provider": rec.statement.provider,
                        "client_id": rec.statement.client_id,
                        "strategy": rec.statement.strategy,
                        "reason": "superseded_by_newer_statement",
                        "older_period": rec.statement.period_end,
                        "newer_period": existing.statement.period_end
                    })

        selected_records = list(by_account.values())
        client_name = selected_records[0].statement.client_name
        period = target_period_end or selected_records[0].statement.period_end

        # 3. Sum total closing corpus and cash
        total_corpus = sum(rec.summary.closing for rec in selected_records)
        total_cash = sum(rec.summary.cash for rec in selected_records)

        # 4. Join Listed Holdings by ISIN
        isin_map = defaultdict(lambda: {
            "name": "",
            "quantity": 0.0,
            "market_value": 0.0,
            "total_cost": 0.0,
            "providers": set()
        })

        aif_holdings = []
        total_equity_value = 0.0
        total_aif_value = 0.0

        for rec in selected_records:
            prov = rec.statement.provider
            for h in rec.holdings:
                # If holding has a valid ISIN and is not an unlisted private debt/equity instrument
                inst = getattr(h, "instrument", None)
                is_unlisted_inst = inst not in (None, "", "Equity", "Stock", "Listed Equity")
                if h.isin and not is_unlisted_inst:
                    entry = isin_map[h.isin]
                    if not entry["name"] or len(h.name) > len(entry["name"]):
                        entry["name"] = h.name
                    entry["quantity"] += h.quantity
                    entry["market_value"] += h.market_value
                    entry["total_cost"] += (h.total_cost or 0.0)
                    entry["providers"].add(prov)
                    total_equity_value += h.market_value
                else:
                    # Unlisted AIF holding bucket
                    total_aif_value += h.market_value
                    aif_holdings.append({
                        "name": h.name,
                        "provider": prov,
                        "quantity": h.quantity,
                        "market_value": h.market_value,
                        "price": h.price,
                        "instrument": getattr(h, "instrument", None)
                    })

        # Build list of consolidated listed holdings
        consolidated_listed: List[ConsolidatedHolding] = []
        for isin, data in sorted(isin_map.items()):
            wt = (data["market_value"] / total_corpus * 100.0) if total_corpus > 0 else 0.0
            consolidated_listed.append(ConsolidatedHolding(
                isin=isin,
                name=data["name"],
                quantity=data["quantity"],
                market_value=data["market_value"],
                total_cost=data["total_cost"],
                weight_pct=round(wt, 2),
                providers_held_in=sorted(list(data["providers"]))
            ))

        # 5. Asset Allocation breakdown
        alloc = {
            "Listed Equity": round((total_equity_value / total_corpus * 100.0), 2) if total_corpus > 0 else 0.0,
            "AIF Alternatives": round((total_aif_value / total_corpus * 100.0), 2) if total_corpus > 0 else 0.0,
            "Cash & Equivalents": round((total_cash / total_corpus * 100.0), 2) if total_corpus > 0 else 0.0
        }

        participating = sorted(list(set(rec.statement.provider for rec in selected_records)))

        return ConsolidatedPortfolio(
            client_name=client_name,
            pan=target_pan,
            period=period,
            currency=target_currency,
            total_corpus=total_corpus,
            total_cash=total_cash,
            total_equity_value=total_equity_value,
            total_aif_value=total_aif_value,
            asset_allocation=alloc,
            participating_providers=participating,
            listed_holdings=consolidated_listed,
            aif_holdings=aif_holdings,
            raw_statements_count=len(selected_records),
            excluded_statements=excluded
        )

    def consolidate_all_currencies(
        self,
        results: List[ExtractionResult],
        target_pan: str,
        target_period_end: Optional[str] = None
    ) -> Dict[str, ConsolidatedPortfolio]:
        """
        Consolidates portfolio partitioned strictly by currency (e.g. INR vs USD).
        Guarantees that disparate currencies are never added together.
        """
        currencies = set()
        for r in results:
            if r.status == "validated":
                for stmt in r.statements:
                    if stmt.statement.pan == target_pan:
                        if stmt.statement.currency:
                            currencies.add(stmt.statement.currency)

        portfolios: Dict[str, ConsolidatedPortfolio] = {}
        for curr in sorted(currencies):
            port = self.consolidate_client(
                results=results,
                target_pan=target_pan,
                target_period_end=target_period_end,
                target_currency=curr
            )
            if port:
                portfolios[curr] = port
        return portfolios
